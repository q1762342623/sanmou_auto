"""**SKILL 调度器**：把 7 条原子指令编排成「收集 → 调动 → 补兵 → 行军 → 撤回 → 收集 …」。

调度规则（按用户口径）
----------------------
1. **首次启动**：据点收集 → 主城坐标收集 → 战报收集；
2. **每条非收集指令执行前，必须先跑一次完整战报收集**——所以循环体固定是
   「跑一次战报收集 → 收集完成后再看有没有可执行指令」；
   没有可执行指令时，继续跑战报收集（用收集填满等待，不空耗）；
3. 主链：`调动 → 补兵 → 行军 → 撤回`，靠收集指令把每一步隔开；
4. **跨轮次**：部队编号只要「做过一次行军且没被打回」就一直留在待撤回清单里，
   下一轮可以被带着继续行军，也可以单独被撤回；
5. **每 90 分钟**重新跑一次据点收集，刷新我方据点坐标。

状态文件：`screenshots/flow/atomic_state.json`（重启后接着跑）
指令日志：`screenshots/flow/atomic_commands.jsonl`（每条指令一行，便于事后统计）

用法：
    python -m sanmou_auto.atomic_loop --minutes 30
    python -m sanmou_auto.atomic_loop --hours 13
    python -m sanmou_auto.atomic_loop --seconds 300 --dry-run
    python -m sanmou_auto.atomic_loop --specs        # 只打印 7 条指令契约
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from . import vision
from .adb import Adb
from .commands import COMMANDS, ORDER, spec_table
from .commands.base import dump_camps, now_hhmmss, read_json, seconds_until, write_json
from .hotspot import HotspotTracker
from .mapnav import MapNav
from .report_collector import (MIN_INTERVAL_SECONDS, SUBMIT_INTERVAL_SECONDS,
                               ReportCollector)
from .troop_return import TroopReturn

CAMP_REFRESH_SECONDS = 90 * 60          # 用户要求：每 1.5 小时刷新据点
RETURN_AFTER_BATTLE = 300               # 行军到位 5 分钟后才允许撤回
POLL_SECONDS = 30.0                     # 收集失败/无可执行指令时的最小空档


def _now() -> str:
    return time.strftime("%H:%M:%S")


class AtomicLoop:
    """原子指令调度器。"""

    def __init__(self, adb: Adb | None = None, log=print,
                 collector: ReportCollector | None = None,
                 tracker: HotspotTracker | None = None,
                 state_path: str | Path | None = None,
                 dry_run: bool = False,
                 camps_refresh: float = CAMP_REFRESH_SECONDS,
                 return_after_battle: float = RETURN_AFTER_BATTLE,
                 max_moves: int = 3,
                 resume: bool = False):
        """`resume=False`（默认）= 每次启动都是**独立新任务**。

        新任务会：归档上一次的调度状态与补兵记录、清空历史热点、
        强制重扫据点与重读主城，绝不继承上次的部队/轮次/热点。
        `resume=True` 才接着上次未跑完的状态继续（异常重启时用）。
        """
        self.adb = adb or Adb()
        self.log = log
        self.collector = collector
        self.resume = bool(resume)
        self.run_id = time.strftime("%Y%m%d_%H%M%S")
        if tracker is not None:
            self.tracker = tracker
        else:
            # 新任务不继承历史热点：等本次第一次战报收集产出 hotspot
            self.tracker = HotspotTracker(log=log, reset=not self.resume)
        self.state_path = Path(state_path or vision.shot_path("atomic_state.json"))
        self.command_log = Path(vision.shot_path("atomic_commands.jsonl"))
        self.dry_run = dry_run
        self.camps_refresh = float(camps_refresh)
        self.return_after_battle = float(return_after_battle)
        self.max_moves = int(max_moves)

        if self.resume:
            self.state: dict = read_json(self.state_path)
            if self.state:
                self.log(f"== 续跑上次任务：{self.state.get('runId') or '未知 runId'}"
                         f"（第 {self.state.get('round') or 0} 轮）")
        else:
            self._archive_previous()
            self.state = {}
        self.state.setdefault("runId", self.run_id)
        self.state.setdefault("startedAt", time.time())
        self.state.setdefault("campsAt", 0.0)
        self.state.setdefault("home", None)
        self.state.setdefault("hotspot", None)
        self.state.setdefault("sent", [])
        self.state.setdefault("sentArriveAt", "")
        self.state.setdefault("needReplenish", [])
        self.state.setdefault("replenishing", [])
        self.state.setdefault("marching", [])
        self.state.setdefault("marchedEver", [])
        self.state.setdefault("moves", 0)
        self.state.setdefault("round", 0)
        self.state.setdefault("reports", {"count": 0, "records": 0, "blobs": 0,
                                          "pages": 0, "uploadOk": 0, "failed": 0})
        self.state.setdefault("commands", {})
        self.camps: list = []
        self._avatar: TroopReturn | None = None
        self._nav = MapNav(self.adb, log=log)

    # ------------------------------------------------------------------ 状态
    def _archive_previous(self) -> None:
        """把上一次任务的调度状态/补兵记录归档，保证本次是干净的新任务。"""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        targets = [self.state_path]
        try:
            from .city_rest import _state_path as city_rest_path
            targets.append(city_rest_path())
        except Exception:          # noqa: BLE001 - 补兵状态缺失不影响启动
            pass
        for path in targets:
            path = Path(path)
            try:
                if not path.is_file():
                    continue
                archived = path.with_name(
                    f"{path.stem}.prev_{stamp}{path.suffix}")
                path.replace(archived)
                self.log(f"  · 上次任务状态已归档：{archived.name}"
                         "（本次按独立新任务执行）")
            except OSError as error:
                self.log(f"  ! 归档 {path.name} 失败：{error}")

    def _save(self) -> None:
        write_json(self.state_path, self.state)

    def _log_command(self, row: dict) -> None:
        try:
            self.command_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.command_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _count(self, cmd: str, ok: bool) -> None:
        row = self.state["commands"].setdefault(cmd, {"ok": 0, "fail": 0})
        row["ok" if ok else "fail"] = int(row.get("ok" if ok else "fail", 0)) + 1

    @property
    def avatars(self) -> TroopReturn:
        if self._avatar is None:
            self._avatar = TroopReturn(self.adb, log=self.log, nav=self._nav)
        return self._avatar

    def _busy_troops(self, indexes=None) -> list[int]:
        """当前「在途/排队」的部队（读右侧头像；读不到就当在途，保守）。"""
        try:
            rows = self.avatars.read_avatars_clean()
        except Exception as error:      # noqa: BLE001
            self.log(f"  ! 读右侧头像失败：{error!r}")
            return [int(i) for i in (indexes or [])]
        busy = [row["index"] for row in rows
                if row.get("moving") or row.get("queued")]
        if indexes is None:
            return busy
        wanted = {int(i) for i in indexes}
        return [i for i in busy if i in wanted]

    # ------------------------------------------------------------------ 启动
    def _command(self, key: str, **kwargs) -> dict:
        module = COMMANDS[key]
        spec = module.SPEC
        shown = []
        for name, value in kwargs.items():
            if name == "log":
                continue
            if name == "camps" and isinstance(value, (list, tuple)):
                shown.append(f"camps={len(value)}个")
            elif isinstance(value, (list, tuple, str, int, float)) or value is None:
                shown.append(f"{name}={value}")
            else:
                shown.append(f"{name}={type(value).__name__}")
        self.log(f"\n---- [{spec.title}] {_now()} 输入: " + ("，".join(shown) or "无"))
        started = time.time()
        if self.dry_run and key not in ("camps", "home", "reports"):
            self.log(f"  · dry-run：跳过指令 {key}")
            return {"ok": True, "skipped": True, "reason": "dry-run"}
        try:
            out = module.run(adb=self.adb, log=self.log, **kwargs)
        except Exception as error:      # noqa: BLE001 - 单条指令异常不打断长跑
            out = {"ok": False, "reason": repr(error)}
            self.log(f"  × [{spec.title}] 异常：{error!r}")
        out.setdefault("ok", False)
        self._count(key, bool(out.get("ok")))
        self._save()
        self._log_command({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "runId": self.run_id,
            "command": key,
            "ok": bool(out.get("ok")), "reason": out.get("reason"),
            "seconds": round(time.time() - started, 2),
            "outputs": {k: v for k, v in out.items()
                        if k not in ("camps", "campDicts", "raw", "troops")},
        })
        return out

    def startup(self) -> None:
        self.log(f"===== 启动：新任务 {self.run_id}"
                 "（据点收集 → 主城坐标收集 → 进入收集驱动的调度循环）")
        # 新任务 campsAt=0 → 必定重扫；续跑时缓存还在 90 分钟内才复用
        out = self._command("camps", refresh=self._camps_stale())
        self.camps = out.get("camps") or []
        if self.camps:
            self.state["campsAt"] = time.time()
        home = self._command("home") if not self.state.get("home") else {
            "ok": True, "home": self.state.get("home")}
        if home.get("home"):
            self.state["home"] = home["home"]
        self._save()

    # ------------------------------------------------------------------ 收集
    def _run_reports(self) -> dict:
        out = COMMANDS["reports"].run(self.collector, tracker=self.tracker,
                                      trigger=f"第{int(self.state.get('round') or 0) + 1}轮",
                                      log=self.log)
        stats = self.state["reports"]
        stats["count"] = int(stats.get("count") or 0) + 1
        if out.get("ok"):
            stats["records"] = int(stats.get("records") or 0) + int(out.get("records") or 0)
            stats["blobs"] = int(stats.get("blobs") or 0) + int(out.get("blobs") or 0)
            stats["pages"] = int(stats.get("pages") or 0) + int(out.get("pages") or 0)
            if out.get("uploadOk"):
                stats["uploadOk"] = int(stats.get("uploadOk") or 0) + 1
        else:
            stats["failed"] = int(stats.get("failed") or 0) + 1
        if out.get("hotspot"):
            self.state["hotspot"] = list(out["hotspot"])
        self.state["lastReportAt"] = time.time()
        self._save()
        return out

    # ------------------------------------------------------------------ 决策
    def _camps_stale(self) -> bool:
        return (time.time() - float(self.state.get("campsAt") or 0)) > self.camps_refresh

    def _return_ready(self) -> bool:
        if not self.state.get("marchedEver"):
            return False
        left = seconds_until(self.state.get("attackArriveAt") or "", now_hhmmss())
        if left is None:
            return True
        return left <= -self.return_after_battle

    def _alive_marching(self) -> list[int]:
        marching = [int(i) for i in (self.state.get("marching") or [])]
        if not marching:
            return []
        busy = set(self._busy_troops(marching))
        return [i for i in marching if i in busy]

    def _close_round(self) -> None:
        st = self.state
        if st.get("sent") or st.get("marching"):
            self.log(f"  · 本轮结束（第 {st.get('round')} 轮）：清空调动/行军状态")
        st["sent"] = []
        st["sentArriveAt"] = ""
        st["marching"] = []
        st["needReplenish"] = []
        st["moves"] = 0
        st["attackCoord"] = None
        st["attackArriveAt"] = ""
        st["camp"] = ""
        st["campCoord"] = None
        st["closedAt"] = time.time()
        self._save()

    def decide(self) -> str | None:
        """**战报收集完成后**调用：返回下一条要执行的指令 key（None = 继续收集）。"""
        st = self.state
        if self._camps_stale():
            return "camps"
        if not st.get("home"):
            return "home"
        if self._return_ready():
            return "return"
        if st.get("needReplenish"):
            return "replenish"
        if not st.get("sent"):
            return "mobilize" if st.get("hotspot") else None
        if not st.get("marching"):
            left = seconds_until(st.get("sentArriveAt") or "", now_hhmmss())
            if left is None or left <= 0:
                return "attack"
            self.log(f"  · 等调动到达（还剩 {left:.0f}s），继续收集战报")
            return None
        alive = self._alive_marching()
        if alive:
            left = seconds_until(st.get("attackArriveAt") or "", now_hhmmss())
            if (left is not None and left <= -self.return_after_battle
                    and int(st.get("moves") or 0) < self.max_moves
                    and st.get("hotspot")
                    and list(st["hotspot"]) != list(st.get("attackCoord") or [])):
                self.log(f"  · 驻留已满 {self.return_after_battle:.0f}s 未被打回 → 换热点再行军")
                return "attack-again"
            return None
        self.log("  · 参战部队已全部不在途中（被打回据点/主城）")
        self._close_round()
        return None

    # ------------------------------------------------------------------ 执行
    def execute(self, cmd: str) -> dict:
        if cmd == "camps":
            out = self._command("camps", refresh=True)
            self.camps = out.get("camps") or self.camps
            if out.get("ok"):
                self.state["campsAt"] = time.time()
            self._save()
            return out
        if cmd == "home":
            out = self._command("home")
            if out.get("home"):
                self.state["home"] = out["home"]
            self._save()
            return out
        if cmd == "mobilize":
            return self._do_mobilize()
        if cmd == "replenish":
            return self._do_replenish()
        if cmd in ("attack", "attack-again"):
            return self._do_attack(cmd)
        if cmd == "return":
            return self._do_return()
        return {"ok": False, "reason": f"unknown-command:{cmd}"}

    def _do_mobilize(self) -> dict:
        st = self.state
        out = self._command("mobilize", war_coord=st.get("hotspot"),
                            camps=self.camps, troops=self.troops)
        if out.get("skipped"):
            return out
        if out.get("ok"):
            sent = sorted({int(i) for i in (out.get("sent") or [])}
                          | {int(i) for i in (out.get("alreadyThere") or [])})
            st["sent"] = sent
            st["sentArriveAt"] = out.get("lastArrival") or ""
            st["camp"] = out.get("camp") or ""
            st["campCoord"] = out.get("campCoord")
            st["needReplenish"] = [int(i) for i in (out.get("needReplenish") or [])]
            st["round"] = int(st.get("round") or 0) + 1
            st["marching"] = []
            st["moves"] = 0
            st["attackCoord"] = None
            st["attackArriveAt"] = ""
            self.log(f"  ✓ 第 {st['round']} 轮：调动到 {st['camp']}，"
                     f"部队{sent}，最后一支 {st['sentArriveAt'] or '未知'} 到达")
        self._save()
        return out

    def _do_replenish(self) -> dict:
        st = self.state
        home = st.get("home") or {}
        out = self._command("replenish", troops=st.get("needReplenish") or [],
                            home_coord=home.get("coord"), camps=self.camps)
        if out.get("skipped"):
            return out
        sent = [int(i) for i in (out.get("sent") or [])]
        if sent:
            st["replenishing"] = sorted(set(st.get("replenishing") or []) | set(sent))
            st["replenishCity"] = out.get("city")
        st["needReplenish"] = [i for i in (st.get("needReplenish") or [])
                               if i not in sent and out.get("ok")]
        self._save()
        return out

    def _do_attack(self, cmd: str) -> dict:
        st = self.state
        troops = self._alive_marching() if cmd == "attack-again" else list(
            st.get("sent") or [])
        if not troops:
            self.log("  · 没有可出征的部队")
            if cmd == "attack-again":
                return {"ok": True, "skipped": True}
            return {"ok": False, "reason": "no-troop"}
        out = self._command("attack", war_coord=st.get("hotspot"), troops=tuple(troops))
        if out.get("skipped"):
            return out
        if out.get("ok"):
            marched = [int(i) for i in (out.get("marching") or [])]
            st["marching"] = sorted(set(st.get("marching") or []) | set(marched))
            st["marchedEver"] = sorted(set(st.get("marchedEver") or []) | set(marched))
            st["attackArriveAt"] = out.get("lastArrival") or st.get("attackArriveAt")
            st["attackCoord"] = out.get("target") or st.get("hotspot")
            st["action"] = out.get("action")
            if cmd == "attack-again":
                st["moves"] = int(st.get("moves") or 0) + 1
        self._save()
        return out

    def _do_return(self) -> dict:
        st = self.state
        out = self._command("return", troops=st.get("marchedEver") or [],
                            arriveAt=st.get("attackArriveAt") or "",
                            replenish=st.get("replenishing") or [])
        if out.get("skipped"):
            return out
        returned = [int(i) for i in (out.get("returned") or [])]
        if returned:
            keep = set(returned)
            st["marchedEver"] = [i for i in (st.get("marchedEver") or [])
                                 if i not in keep]
            st["marching"] = [i for i in (st.get("marching") or []) if i not in keep]
            st["sent"] = [i for i in (st.get("sent") or []) if i not in keep]
            st["replenishing"] = [i for i in (st.get("replenishing") or [])
                                  if i not in keep]
            st["lastReturnAt"] = time.time()
            self.log(f"  ✓ 已撤回主城 {returned}；"
                     f"仍在战场 {st['marchedEver'] or '无'}")
            if not st["marchedEver"]:
                self._close_round()
        self._save()
        return out

    # ------------------------------------------------------------------ 循环
    def run(self, duration: float = 3600.0, troops=(1, 2, 3, 4),
            max_fails: int = 8, max_ticks: int = 0) -> dict:
        """跑调度循环（决策 100% 由 `decide()` 的规则给出，外部监控只读）。"""
        self.troops = tuple(int(t) for t in troops if 1 <= int(t) <= 4) or (1, 2, 3, 4)
        self.log(f"===== ENGINE=atomic runId={self.run_id} "
                 f"时长={int(duration)}s（逐拍按 decide() 规则执行，"
                 f"没有固定轮次/固定顺序）")
        started = time.monotonic()
        deadline = started + max(0.0, duration)
        self.adb.ensure_online()
        self._nav.ensure_map()
        self.startup()

        fails = 0
        ticks = 0
        stop_reason = "duration"
        try:
            while True:
                if time.monotonic() >= deadline:
                    stop_reason = "duration"
                    break
                if max_ticks and ticks >= max_ticks:
                    stop_reason = "max-ticks"
                    break
                ticks += 1
                self.state["ticks"] = ticks
                self.state["lastTickAt"] = time.time()
                self._save()
                self.log(f"\n===== 调度第 {ticks} 拍 {_now()} "
                         f"（第 {int(self.state.get('round') or 0)} 轮）")
                report = self._run_reports()
                if not report.get("enabled"):
                    if not self.state.get("hotspot"):
                        self.log("  ! 战报收集未启用且没有热点（新任务不继承历史热点）"
                                 "→ 无法确定战场，结束")
                        stop_reason = "no-hotspot"
                        break
                    self.log("  · 战报收集未启用，用本次任务已拿到的热点推进")
                elif not report.get("ok"):
                    fails += 1
                    self.log(f"  ! 战报收集失败（连续 {fails} 次）")
                    if fails >= max_fails:
                        stop_reason = "too-many-report-failures"
                        break
                else:
                    fails = 0

                cmd = self.decide()
                if cmd is None:
                    continue                # 用收集填满等待（不 sleep、不空耗）
                self.log(f"  → 决策：执行指令 {cmd}")
                out = self.execute(cmd)
                if self.dry_run:
                    self.log("  · dry-run：决策链已走通，停止")
                    stop_reason = "dry-run"
                    break
                if not out.get("ok") and not out.get("skipped"):
                    self.log(f"  × 指令 {cmd} 失败：{out.get('reason')}")
                    time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            stop_reason = "interrupted"
            self.log("== 收到 Ctrl+C，保存状态后退出")

        wall = round(time.monotonic() - started, 1)
        summary = {
            "feature": "三谋拉线与战报自动收集（原子指令调度）",
            "runId": self.run_id,
            "resume": self.resume,
            "ok": stop_reason in ("duration", "max-ticks", "interrupted", "dry-run"),
            "stop_reason": stop_reason,
            "wallSeconds": wall,
            "ticks": ticks,
            "rounds": int(self.state.get("round") or 0),
            "hotspot": self.state.get("hotspot"),
            "home": self.state.get("home"),
            "camp": self.state.get("camp"),
            "sent": self.state.get("sent"),
            "marching": self.state.get("marching"),
            "marchedEver": self.state.get("marchedEver"),
            "replenishing": self.state.get("replenishing"),
            "reports": self.state.get("reports"),
            "commands": self.state.get("commands"),
            "statePath": str(self.state_path),
            "commandLog": str(self.command_log),
        }
        self._save()
        out_path = vision.shot_path(f"atomic_{time.strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(out_path, "w", encoding="utf-8") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass
        summary["path"] = out_path
        self.log(f"\n===== 调度结束：{stop_reason}，{ticks} 拍，"
                 f"第 {summary['rounds']} 轮，汇总 {out_path}")
        return summary


# ---------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    from . import cli

    cli.setup_console()
    ap = argparse.ArgumentParser(
        description="三谋拉线与战报自动收集（原子指令 + SKILL 调度）")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--max-ticks", type=int, default=0, help="最多调度多少拍（0=不限）")
    ap.add_argument("--troops", default="1,2,3,4")
    ap.add_argument("--camps-refresh-minutes", type=float, default=90.0,
                    help="据点收集刷新间隔（默认 90 分钟）")
    ap.add_argument("--return-after", type=float, default=RETURN_AFTER_BATTLE,
                    help="行军到位多少秒后才允许撤回（默认 300）")
    ap.add_argument("--max-moves", type=int, default=3, help="驻留未被打回最多换几次热点")
    ap.add_argument("--report-interval", type=float, default=300.0)
    ap.add_argument("--report-submit-interval", type=float, default=SUBMIT_INTERVAL_SECONDS)
    ap.add_argument("--report-max-seconds", type=int, default=900)
    ap.add_argument("--report-skip-upload", action="store_true")
    ap.add_argument("--report-state", default="")
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--max-fails", type=int, default=8)
    ap.add_argument("--refresh-camps", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只收集/只决策，不动手")
    ap.add_argument("--resume", action="store_true",
                    help="续跑上次未完成的任务（默认每次启动都是独立新任务）")
    ap.add_argument("--state", default="", help="状态文件路径（默认 screenshots/flow/atomic_state.json）")
    ap.add_argument("--specs", action="store_true", help="只打印 7 条指令契约")
    args = ap.parse_args(argv)

    if args.specs:
        print(spec_table())
        return 0

    if args.seconds is not None:
        duration = float(args.seconds)
    elif args.hours is not None:
        duration = float(args.hours) * 3600.0
    else:
        duration = float(args.minutes) * 60.0

    troops = tuple(int(t) for t in args.troops.split(",") if t.strip())
    adb = Adb()
    collector = ReportCollector(
        interval=max(MIN_INTERVAL_SECONDS, args.report_interval),
        submit_interval=args.report_submit_interval,
        enabled=not args.no_report,
        log=print,
        prepare=lambda: MapNav(adb).ensure_map(),
        skip_upload=args.report_skip_upload,
        max_seconds=args.report_max_seconds,
        state_path=args.report_state or None,
        collect_on_start=False,
    )
    loop = AtomicLoop(adb=adb, collector=collector,
                      state_path=args.state or None,
                      dry_run=args.dry_run,
                      camps_refresh=args.camps_refresh_minutes * 60.0,
                      return_after_battle=args.return_after,
                      max_moves=args.max_moves,
                      resume=args.resume)
    if args.refresh_camps:
        loop.state["campsAt"] = 0.0
    summary = loop.run(duration=duration, troops=troops,
                       max_fails=args.max_fails, max_ticks=args.max_ticks)
    print("\n===== 三谋拉线与战报自动收集（原子指令调度） =====")
    print(f"  停止原因：{summary['stop_reason']}  拍数：{summary['ticks']}"
          f"  轮次：{summary['rounds']}")
    print(f"  当前热点：{summary['hotspot']}  据点：{summary['camp']}")
    reports = summary.get("reports") or {}
    print(f"  有效战报：{reports.get('records')} 条 / {reports.get('blobs')} BLOB / "
          f"{reports.get('pages')} 页 / 上传 {reports.get('uploadOk')} 次"
          f"（收集 {reports.get('count')} 次，失败 {reports.get('failed')} 次）")
    print(f"  指令统计：{json.dumps(summary.get('commands') or {}, ensure_ascii=False)}")
    print(f"  明细：{summary.get('path')}")
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
