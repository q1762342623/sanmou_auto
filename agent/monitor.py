"""**外部 Agent 监控**（只读，不决策）：战斗本体仍然由纯规则调度器驱动。

定位
----
- 决策：100% 由 [`sanmou_auto.atomic_loop`](../atomic_loop.py) 的规则决定（确定性、可复现）；
- 监控：本模块在**另一个进程**里定时读取调度状态与指令流水，做体检 + 让 DeepSeek
  写一段中文诊断与建议，落盘给人看。**它不碰设备、不改状态、不影响战斗**。

体检项（纯规则，先于 LLM 执行，保证 LLM 挂了也有告警）：
1. 状态是否停滞（`lastTickAt` 超过 2 个巡检周期没更新）；
2. 战报收集失败率（`reports.failed / count`）；
3. 待撤回部队是否长期滞留（`marchedEver` 连续多次巡检不减少）；
4. 指令失败累计（`commands.*.fail`）；
5. 据点缓存是否过期（`campsAt` 超过 120 分钟）。

输出：
- `screenshots/flow/monitor.log`           人读巡检日志（每周期一段）
- `screenshots/flow/monitor_reports.jsonl` 每周期一行 JSON（体检 + LLM 诊断）

用法：
    python -m sanmou_auto.agent.monitor --hours 13            # 每 15 分钟巡检一次
    python -m sanmou_auto.agent.monitor --minutes 30 --interval 300
    python -m sanmou_auto.agent.monitor --once                # 只跑一次
    python -m sanmou_auto.agent.monitor --no-llm              # 只做规则体检
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .. import vision
from ..commands.base import read_json
from .deepseek import DeepSeekClient, DeepSeekError

STATE_PATH = Path(vision.shot_path("atomic_state.json"))
COMMANDS_PATH = Path(vision.shot_path("atomic_commands.jsonl"))
META_PATH = Path(vision.shot_path("hotspot_13h.meta.json"))
LOG_PATH = Path(vision.shot_path("monitor.log"))
REPORT_PATH = Path(vision.shot_path("monitor_reports.jsonl"))

SYSTEM_PROMPT = """你是《三国谋定天下》自动拉线的**外部监控 Agent**，只做观察与建议，不参与决策。

你会收到一份 JSON 快照（规则调度器的运行状态 + 最近指令流水 + 规则体检告警）。
请用中文输出 JSON：
{
  "headline": "一句话结论（<=30字）",
  "observations": ["观察1", "观察2", "观察3"],
  "advice": ["如果继续这样，建议操作员注意什么"],
  "risk": "low|mid|high"
}
要求：只根据快照里的事实说话，不要臆测数值；没有异常就直说「运行正常」。"""

ALERT_LIMITS = {
    "stale_factor": 2,        # 超过 2 个巡检周期没更新 → 停滞告警
    "report_fail_ratio": 0.3,  # 战报失败率超过 30% 告警
    "camps_stale_minutes": 120,
}


class BattleMonitor:
    def __init__(self, client: DeepSeekClient | None = None, log=print,
                 interval: float = 900.0, use_llm: bool = True):
        self.client = client or DeepSeekClient(log=log)
        self.log = log
        self.interval = max(30.0, float(interval))
        self.use_llm = use_llm
        self.cycles = 0
        self.llm_ok = 0
        self.llm_fail = 0
        self._last_marched: list[int] = []
        self._marched_hold = 0

    # ------------------------------------------------------------------ 工具
    def _append(self, path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text.rstrip() + "\n")
                handle.flush()
        except OSError:
            pass

    def _tail_commands(self, limit: int = 8) -> list[dict]:
        try:
            lines = COMMANDS_PATH.read_text(encoding="utf-8",
                                            errors="replace").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines[-limit:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    # ------------------------------------------------------------------ 体检
    def snapshot(self) -> dict:
        state = read_json(STATE_PATH)
        now = time.time()
        try:
            state_mtime = STATE_PATH.stat().st_mtime
        except OSError:
            state_mtime = 0.0
        meta = read_json(META_PATH)
        reports = state.get("reports") or {}

        alerts: list[str] = []
        if not state:
            alerts.append("没有调度状态文件（atomic_state.json 不存在，任务可能还没启动）")
        else:
            last_tick = float(state.get("lastTickAt") or state_mtime or 0)
            silent = (now - last_tick) / 60.0 if last_tick else None
            if silent is not None and silent > self.interval * ALERT_LIMITS["stale_factor"] / 60.0:
                alerts.append(f"状态已 {silent:.0f} 分钟没有更新（可能卡死或进程已退出）")

        count = int(reports.get("count") or 0)
        failed = int(reports.get("failed") or 0)
        if count >= 3 and failed / max(1, count) > ALERT_LIMITS["report_fail_ratio"]:
            alerts.append(f"战报收集失败率偏高：{failed}/{count}"
                          f"（{failed / max(1, count) * 100:.0f}%）")

        marched = [int(i) for i in (state.get("marchedEver") or [])]
        if marched and marched == self._last_marched:
            self._marched_hold += 1
        else:
            self._marched_hold = 0
        self._last_marched = list(marched)
        if self._marched_hold >= 3:
            alerts.append(f"部队{marched} 已连续 {self._marched_hold} 次巡检未撤回"
                          "（可能一直在打，或撤回条件不满足）")

        for name, stats in (state.get("commands") or {}).items():
            fail = int((stats or {}).get("fail") or 0)
            if fail:
                alerts.append(f"指令 {name} 累计失败 {fail} 次")

        camps_age = (now - float(state.get("campsAt") or 0)) / 60.0
        if state and camps_age > ALERT_LIMITS["camps_stale_minutes"]:
            alerts.append(f"据点缓存已 {camps_age:.0f} 分钟未刷新"
                          f"（上限 {ALERT_LIMITS['camps_stale_minutes']} 分钟）")

        return {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stateFile": str(STATE_PATH),
            "stateAgeSeconds": round(now - state_mtime, 1) if state_mtime else None,
            "runId": state.get("runId"),
            "ticks": state.get("ticks"),
            "round": state.get("round"),
            "hotspot": state.get("hotspot"),
            "camp": state.get("camp"),
            "sent": state.get("sent") or [],
            "marching": state.get("marching") or [],
            "marchedEver": marched,
            "replenishing": state.get("replenishing") or [],
            "needReplenish": state.get("needReplenish") or [],
            "reports": reports,
            "commands": state.get("commands") or {},
            "campsAgeMinutes": round(camps_age, 1) if state else None,
            "deadlineLeftMinutes": (
                round((float(meta.get("plannedEndAt") or 0) - now) / 60.0, 1)
                if meta.get("plannedEndAt") else None),
            "recentCommands": [
                {"at": row.get("at"), "command": row.get("command"),
                 "ok": row.get("ok"), "reason": row.get("reason"),
                 "seconds": row.get("seconds")}
                for row in self._tail_commands()],
            "alerts": alerts,
        }

    # ------------------------------------------------------------------ 诊断
    def diagnose(self, snap: dict) -> dict:
        if not self.use_llm:
            return {"source": "rule", "headline": "规则体检完成",
                    "observations": snap.get("alerts") or ["运行正常"],
                    "advice": [], "risk": "high" if snap.get("alerts") else "low"}
        user = ("监控快照（JSON）：\n" + json.dumps(snap, ensure_ascii=False)
                + "\n\n请按要求输出 JSON。")
        try:
            data = self.client.json_chat(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": user}],
                temperature=0.2, max_tokens=500, json_mode=True)
            self.llm_ok += 1
            data["source"] = "deepseek"
            return data
        except DeepSeekError as error:
            self.llm_fail += 1
            self.log(f"  ! DeepSeek 诊断失败：{error}")
            return {"source": "rule-fallback", "headline": "LLM 不可用，仅规则体检",
                    "observations": snap.get("alerts") or ["运行正常"],
                    "advice": [], "risk": "high" if snap.get("alerts") else "low"}

    # ------------------------------------------------------------------ 巡检
    def cycle(self) -> dict:
        self.cycles += 1
        snap = self.snapshot()
        diag = self.diagnose(snap)
        row = {"cycle": self.cycles, "snapshot": snap, "diagnosis": diag,
               "tokens": self.client.stats() if self.use_llm else None}
        self._append(REPORT_PATH, json.dumps(row, ensure_ascii=False))

        head = f"[{time.strftime('%H:%M')}] 巡检#{self.cycles} " \
               f"run={snap.get('runId') or '?'} 拍{snap.get('ticks')} " \
               f"第{snap.get('round')}轮 调动{len(snap.get('sent') or [])}队/" \
               f"行军{len(snap.get('marching') or [])}队/" \
               f"待撤{len(snap.get('marchedEver') or [])}队 " \
               f"战报{(snap.get('reports') or {}).get('records')}条 " \
               f"→ {diag.get('headline')}（风险{diag.get('risk')}, 来源{diag.get('source')}）"
        self._append(LOG_PATH, head)
        self.log(head)
        for item in diag.get("observations") or []:
            self._append(LOG_PATH, f"    · {item}")
        for item in diag.get("advice") or []:
            self._append(LOG_PATH, f"    → {item}")
        alerts = snap.get("alerts") or []
        if alerts:
            for alert in alerts:
                self._append(LOG_PATH, f"    ! {alert}")
        return row

    def run(self, seconds: float = 0.0, once: bool = False) -> dict:
        started = time.monotonic()
        rows: list[dict] = []
        self.log(f"== 外部监控启动：每 {self.interval / 60:.0f} 分钟巡检一次"
                 f"（LLM={'on' if self.use_llm else 'off'}）")
        while True:
            rows.append(self.cycle())
            if once:
                break
            if seconds and time.monotonic() - started + self.interval > seconds:
                break
            time.sleep(self.interval)
        return {"cycles": len(rows), "llmOk": self.llm_ok,
                "llmFail": self.llm_fail,
                "log": str(LOG_PATH), "reports": str(REPORT_PATH),
                "last": rows[-1] if rows else None}


def main(argv: list[str] | None = None) -> int:
    from .. import cli as cli_mod

    cli_mod.setup_console()
    ap = argparse.ArgumentParser(
        description="外部 Agent 监控（只读）：规则决策 + LLM 诊断")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--interval", type=float, default=900.0,
                    help="巡检间隔秒数（默认 900 = 15 分钟）")
    ap.add_argument("--once", action="store_true", help="只巡检一次")
    ap.add_argument("--no-llm", action="store_true", help="只做规则体检，不调 DeepSeek")
    ap.add_argument("--model", default="", help="覆盖 DeepSeek 模型名")
    ap.add_argument("--api-key", default="")
    args = ap.parse_args(argv)

    if args.seconds is not None:
        duration = float(args.seconds)
    elif args.hours is not None:
        duration = float(args.hours) * 3600.0
    elif args.minutes is not None:
        duration = float(args.minutes) * 60.0
    else:
        duration = 0.0

    client = DeepSeekClient(api_key=args.api_key or None,
                            model=args.model or "deepseek-chat")
    monitor = BattleMonitor(client=client, interval=args.interval,
                            use_llm=not args.no_llm)
    out = monitor.run(seconds=duration, once=args.once)
    print(json.dumps({"cycles": out["cycles"], "llmOk": out["llmOk"],
                      "llmFail": out["llmFail"], "log": out["log"],
                      "reports": out["reports"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
