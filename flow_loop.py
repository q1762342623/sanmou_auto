"""功能「**自动拉线**」：循环执行
「邮件分析战场 → 调动据点 → 寻找敌方进攻 → 回家补兵」，并统计每一轮每个节点耗时。

停止条件（满足任一即停）：
1. 到达运行时长（默认 1 小时，`--minutes` / `--seconds`）；
2. **无法再补满兵力**：4 支部队全部「补兵后仍 < 满编」被跳过；
3. 到达 `--max-rounds` 轮；
4. 连续 `--max-fails` 轮抛异常（防止死循环空转）；
5. Ctrl+C（仍会写出已完成的轮次与汇总）。

每轮输出（示例）：
    [第 3 轮 07:52:10] 用时 13m34s
        节点: 邮件分析战场 1m12s | 调动据点 4m51s | 寻找敌方进攻 11m02s | 回家补兵 31s
        阶段耗时(Top): 等待战斗结束 10m12s | 部队3 出征 57s | 定位最近可调动据点 12s
        部队: 1=出征[兵力33000] 2=兵力不足(12000) 3=跳过(行军 00:04:12) 4=出征[兵力33000]

结果写入 `screenshots/flow/auto_line_<时间戳>.json`（汇总）
与同名 `.jsonl`（逐轮明细，边跑边写，可随时查看）。

用法:
    python -m sanmou_auto.flow_loop                     # 跑 1 小时
    python -m sanmou_auto.flow_loop --minutes 10        # 跑 10 分钟
    python -m sanmou_auto.flow_loop --max-rounds 1      # 冒烟一轮
    python -m sanmou_auto.flow_loop --no-wait           # 不等战斗结束
    python -m sanmou_auto.flow_loop --no-home           # 不回家补兵
"""
from __future__ import annotations

import argparse
import json
import time

from . import cli, vision
from .adb import Adb
from .flow_mobilize import MobilizeFlow
from .mobilize import STRENGTH_FULL
from .timing import STAGES, STAGE_OTHER, StepTimer, fmt_seconds

# 全部 4 队都补不满兵力时的 skipped 标记（见 mobilize.ensure_strength）
LOW_STRENGTH = "low-strength"

KIND_ORDER = ("分析", "执行", "等待", "准备", "收尾")


def _now() -> str:
    return time.strftime("%H:%M:%S")


def strength_exhausted(res: dict, troops: tuple[int, ...]) -> bool:
    """4 队是否全部「补兵后仍不满」——判定无法再补满兵力的唯一依据。"""
    entries = {t.get("index"): t for t in res.get("troops", [])
               if isinstance(t, dict)}
    if set(troops) - set(entries):
        return False
    return all(entries[i].get("skipped") == LOW_STRENGTH for i in troops)


def _troop_line(res: dict) -> str:
    parts = []
    for t in res.get("troops", []):
        if not isinstance(t, dict):
            continue
        idx = t.get("index")
        if t.get("skipped") == LOW_STRENGTH:
            note = f"兵力不足({t.get('strength')})"
        elif t.get("arrived"):
            note = "已到达"
        elif t.get("dispatched"):
            note = "出征"
            if t.get("status"):
                note += f"[{t['status']}]"
        else:
            note = t.get("status") or "未出征"
        if t.get("strength") is not None:
            note += f"<{t['strength']}>"
        parts.append(f"{idx}={note}")
    return " ".join(parts)


def _round_reason(res: dict) -> str:
    reason = res.get("reason") or ""
    bt = res.get("battle")
    if isinstance(bt, dict):
        br = bt.get("reason")
        if br and br not in reason:
            reason = f"{reason}+{br}" if reason else str(br)
    hr = res.get("home_replenish")
    if isinstance(hr, dict) and not hr.get("ok"):
        hr_reason = hr.get("reason") or "home-failed"
        if hr_reason not in reason:
            reason = f"{reason}+{hr_reason}" if reason else hr_reason
    return reason or "-"


def _stage_line(by_stage: dict[str, float], total: float | None = None) -> str:
    keys = list(STAGES) + [k for k in by_stage if k not in STAGES]
    parts = [f"{k} {fmt_seconds(by_stage[k])}" for k in keys if k in by_stage]
    if not parts:
        return "-"
    text = " | ".join(parts)
    if total:
        acc = sum(by_stage.values())
        share = acc / total * 100 if total else 0
        text += f"（节点合计 {fmt_seconds(acc)}，占本轮 {share:.0f}%）"
    return text


def _pool_steps(rounds: list[dict]) -> list[dict]:
    """把所有轮次的步骤按「名称」汇总：次数 / 总耗时 / 平均 / 最短 / 最长。"""
    agg: dict[tuple[str, str], dict] = {}
    for r in rounds:
        for s in r.get("steps", []):
            key = (s["name"], s["kind"])
            a = agg.setdefault(key, {
                "name": s["name"], "kind": s["kind"],
                "stage": s.get("stage") or STAGE_OTHER, "count": 0,
                "total": 0.0, "min": None, "max": 0.0,
            })
            a["count"] += 1
            a["total"] = round(a["total"] + s["seconds"], 3)
            a["min"] = s["seconds"] if a["min"] is None else min(a["min"], s["seconds"])
            a["max"] = max(a["max"], s["seconds"])
    out = []
    for a in agg.values():
        out.append({
            "name": a["name"], "kind": a["kind"], "stage": a["stage"],
            "count": a["count"], "total": a["total"],
            "avg": round(a["total"] / a["count"], 3),
            "min": a["min"], "max": a["max"],
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


def _pool_kinds(rounds: list[dict]) -> dict[str, dict]:
    """按大类（分析/执行/等待…）汇总：总耗时、每轮平均、每轮最长。"""
    agg: dict[str, dict] = {}
    for r in rounds:
        kinds = r.get("by_kind") or {}
        for kind, sec in kinds.items():
            a = agg.setdefault(kind, {"kind": kind, "total": 0.0,
                                      "max": 0.0, "rounds": 0})
            a["total"] = round(a["total"] + sec, 3)
            a["max"] = max(a["max"], sec)
            a["rounds"] += 1
    for a in agg.values():
        a["avg"] = round(a["total"] / max(1, a["rounds"]), 3)
    return agg


def _pool_stages(rounds: list[dict]) -> dict[str, dict]:
    """按「自动拉线」四大节点汇总：总耗时 / 每轮平均 / 每轮最长 / 占比。"""
    agg: dict[str, dict] = {}
    for r in rounds:
        stages = r.get("by_stage") or {}
        for stage, sec in stages.items():
            a = agg.setdefault(stage, {"stage": stage, "total": 0.0,
                                       "max": 0.0, "rounds": 0})
            a["total"] = round(a["total"] + sec, 3)
            a["max"] = max(a["max"], sec)
            a["rounds"] += 1
    total_all = sum(a["total"] for a in agg.values()) or 1e-6
    for a in agg.values():
        a["avg"] = round(a["total"] / max(1, a["rounds"]), 3)
        a["share"] = round(a["total"] / total_all, 4)
    return agg


def _print_round(i: int, started: str, wall: float,
                 by_kind: dict[str, float], by_stage: dict[str, float],
                 res: dict) -> None:
    kinds = " / ".join(
        f"{k} {fmt_seconds(by_kind[k])}"
        for k in KIND_ORDER if k in by_kind)
    extra = " ".join(f"{k} {fmt_seconds(v)}"
                     for k, v in by_kind.items() if k not in KIND_ORDER)
    status = "完成" if res.get("ok") else "未完成"
    print(f"\n[第 {i} 轮 {started}] 用时 {fmt_seconds(wall)} | "
          f"{status} reason={_round_reason(res)}")
    print("    节点: " + _stage_line(by_stage, wall))
    print("    分类: " + kinds + (f" {extra}" if extra else ""))
    steps = sorted(res.get("timing", []),
                   key=lambda s: s.get("seconds", 0), reverse=True)
    if steps:
        print("    阶段耗时(Top): " + " | ".join(
            f"{s['name']} {fmt_seconds(s['seconds'])}" for s in steps[:6]))
    line = _troop_line(res)
    if line:
        print("    部队: " + line)


def _print_summary(rounds: list[dict], wall_total: float, out_path: str) -> None:
    print("\n===== 汇总（自动拉线）=====")
    print(f"轮次 {len(rounds)}  总用时 {fmt_seconds(wall_total)}  "
          f"（平均 {fmt_seconds(wall_total / max(1, len(rounds)))}/轮）")
    stages = _pool_stages(rounds)
    if stages:
        print("-- 按节点 --")
        for stage in list(STAGES) + [s for s in stages if s not in STAGES]:
            a = stages.get(stage)
            if not a:
                continue
            print(f"  {stage}: 总 {fmt_seconds(a['total'])}  "
                  f"每轮平均 {fmt_seconds(a['avg'])}  "
                  f"每轮最长 {fmt_seconds(a['max'])}  "
                  f"占比 {a['share'] * 100:.0f}%")
    kinds = _pool_kinds(rounds)
    if kinds:
        print("-- 按大类 --")
        for kind in KIND_ORDER + tuple(k for k in kinds if k not in KIND_ORDER):
            a = kinds.get(kind)
            if not a:
                continue
            share = a["total"] / max(1e-6, sum(x["total"] for x in kinds.values()))
            print(f"  {kind}: 总 {fmt_seconds(a['total'])}  "
                  f"每轮平均 {fmt_seconds(a['avg'])}  "
                  f"每轮最长 {fmt_seconds(a['max'])}  "
                  f"占比 {share * 100:.0f}%")
    steps = _pool_steps(rounds)
    if steps:
        print("-- 按阶段（Top 15）--")
        for s in steps[:15]:
            print(f"  [{s['stage']}·{s['kind']}] {s['name']}: 次数 {s['count']}  "
                  f"总 {fmt_seconds(s['total'])}  平均 {fmt_seconds(s['avg'])}  "
                  f"最短 {fmt_seconds(s['min'])}  最长 {fmt_seconds(s['max'])}")
    print(f"明细: {out_path}")


class MobilizeLoop:
    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.log = log
        self.flow = MobilizeFlow(self.adb, log=log)
        self.rounds: list[dict] = []

    def run(self, duration: float = 3600.0,
            limit: int = 10, troops: tuple[int, ...] = (1, 2, 3, 4),
            battle: bool = True, wait_return: bool = True,
            battle_timeout: float = 900.0,
            min_strength: int = STRENGTH_FULL, replenish: bool = True,
            enemy_fallback: bool = True, dry_run: bool = False,
            gap: float = 5.0, max_rounds: int = 0,
            stop_on_no_strength: bool = True,
            max_fails: int = 5, home: bool = True) -> dict:
        self.adb.ensure_online()
        tag = time.strftime("%Y%m%d_%H%M%S")
        out_path = vision.shot_path(f"auto_line_{tag}.json")
        jsonl_path = out_path[:-5] + ".jsonl"
        started_all = time.monotonic()
        deadline = started_all + max(0.0, duration)
        stop_reason = "duration"
        fails = 0
        i = 0

        self.log(f"== 自动拉线循环开始：计划 {fmt_seconds(duration)}"
                 f"（最多 {max_rounds or '不限'} 轮，间隔 {gap:.0f}s）")
        self.log("== 每轮节点：邮件分析战场 → 调动据点 → 寻找敌方进攻 → 回家补兵")
        self.log(f"== 明细将写入 {jsonl_path}")

        try:
            while True:
                now = time.monotonic()
                if max_rounds and i >= max_rounds:
                    stop_reason = "max-rounds"
                    break
                if now >= deadline:
                    stop_reason = "duration"
                    break
                i += 1
                round_started = _now()
                t0 = time.monotonic()
                tm = StepTimer()
                try:
                    res = self.flow.run(
                        limit=limit, troops=troops, dry_run=dry_run,
                        battle=battle, wait_return=wait_return,
                        battle_timeout=battle_timeout,
                        min_strength=min_strength, replenish=replenish,
                        enemy_fallback=enemy_fallback, timer=tm, home=home)
                    error = ""
                    fails = 0
                except Exception as e:                  # 单轮异常不终止长跑
                    res = {"ok": False, "reason": f"exception: {e}",
                           "timing": tm.as_list(),
                           "timing_by_kind": tm.totals_by_kind(),
                           "timing_by_stage": tm.totals_by_stage(),
                           "elapsed": tm.elapsed}
                    error = repr(e)
                    fails += 1
                    self.log(f"× 第 {i} 轮异常：{error}（连续 {fails}/{max_fails}）")

                wall = round(time.monotonic() - t0, 3)
                by_kind = res.get("timing_by_kind") or tm.totals_by_kind()
                by_stage = res.get("timing_by_stage") or tm.totals_by_stage()
                record = {
                    "round": i,
                    "started": round_started,
                    "wall": wall,
                    "ok": bool(res.get("ok")),
                    "reason": _round_reason(res),
                    "error": error,
                    "battlefield": res.get("battlefield"),
                    "camp": res.get("camp"),
                    "last_arrival": res.get("last_arrival"),
                    "troops": res.get("troops"),
                    "home_ok": res.get("home_ok"),
                    "home_replenish": res.get("home_replenish"),
                    "by_kind": by_kind,
                    "by_stage": by_stage,
                    "steps": res.get("timing") or tm.as_list(),
                    "record": res.get("record"),
                }
                self.rounds.append(record)
                _print_round(i, round_started, wall, by_kind, by_stage, res)
                with open(jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

                if stop_on_no_strength and strength_exhausted(res, troops):
                    stop_reason = "no-strength"
                    self.log("== 4 支部队都已补不满兵力，停止循环")
                    break
                if fails >= max(1, max_fails):
                    stop_reason = "too-many-failures"
                    self.log(f"== 连续 {fails} 轮异常，停止循环")
                    break

                rest = deadline - time.monotonic()
                if rest <= 0:
                    stop_reason = "duration"
                    break
                time.sleep(min(max(0.0, gap), rest))
        except KeyboardInterrupt:
            stop_reason = "interrupted"
            self.log("== 收到 Ctrl+C，写出已完成轮次")

        wall_total = round(time.monotonic() - started_all, 3)
        summary = {
            "feature": "自动拉线",
            "ok": stop_reason in ("duration", "no-strength", "max-rounds",
                                  "interrupted"),
            "stop_reason": stop_reason,
            "rounds": len(self.rounds),
            "wall_total": wall_total,
            "duration_plan": duration,
            "by_stage": _pool_stages(self.rounds),
            "by_kind": _pool_kinds(self.rounds),
            "steps": _pool_steps(self.rounds),
            "detail": self.rounds,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        _print_summary(self.rounds, wall_total, out_path)
        self.log(f"== 停止原因: {stop_reason}")
        return {"stop_reason": stop_reason, "rounds": len(self.rounds),
                "wall_total": wall_total, "path": out_path,
                "summary": summary}


def main(argv: list[str] | None = None) -> int:
    cli.setup_console()
    ap = argparse.ArgumentParser(
        description="自动拉线：循环 邮件分析战场 → 调动据点 → 寻找敌方进攻 → 回家补兵，"
                    "统计每轮各节点耗时")
    ap.add_argument("--minutes", type=float, default=60.0,
                    help="运行时长（分钟，默认 60）")
    ap.add_argument("--seconds", type=float, default=None,
                    help="运行时长（秒，优先于 --minutes）")
    ap.add_argument("--max-rounds", type=int, default=0,
                    help="最多执行轮数（0 = 不限，默认 0）")
    ap.add_argument("--gap", type=float, default=5.0,
                    help="轮间隔秒数（默认 5）")
    ap.add_argument("--max-fails", type=int, default=5,
                    help="连续异常多少轮后停止（默认 5）")
    ap.add_argument("--limit", type=int, default=10, help="读取同盟邮件条数")
    ap.add_argument("--troops", default="1,2,3,4",
                    help="要调动的部队序号（1-4），逗号分隔")
    ap.add_argument("--dry-run", action="store_true",
                    help="只到「点选最近可调动据点」，不实际出征")
    ap.add_argument("--no-battle", action="store_true",
                    help="只调动，不执行正面战斗")
    ap.add_argument("--no-wait", action="store_true",
                    help="出征后不等待战斗结束")
    ap.add_argument("--battle-timeout", type=float, default=900.0,
                    help="等待战斗结束/返回的秒数（默认 900）")
    ap.add_argument("--min-strength", type=int, default=STRENGTH_FULL,
                    help=f"补兵后仍低于该兵力就跳过该队（默认 {STRENGTH_FULL}）")
    ap.add_argument("--no-replenish", action="store_true",
                    help="兵力不满也不点补兵")
    ap.add_argument("--no-enemy-fallback", action="store_true",
                    help="邮件执行不了时不去找红色敌对目标")
    ap.add_argument("--no-home", action="store_true",
                    help="不执行「回家补兵」节点")
    ap.add_argument("--no-strength-stop", action="store_true",
                    help="4 队都补不满兵力也不停止（继续跑满时长）")
    args = ap.parse_args(argv)

    duration = args.seconds if args.seconds is not None else args.minutes * 60.0
    troops = tuple(int(t) for t in args.troops.split(",") if t.strip())
    loop = MobilizeLoop()
    out = loop.run(
        duration=duration, limit=args.limit, troops=troops,
        dry_run=args.dry_run, battle=not args.no_battle,
        wait_return=not args.no_wait, battle_timeout=args.battle_timeout,
        min_strength=args.min_strength, replenish=not args.no_replenish,
        enemy_fallback=not args.no_enemy_fallback,
        gap=args.gap, max_rounds=args.max_rounds,
        stop_on_no_strength=not args.no_strength_stop,
        max_fails=args.max_fails, home=not args.no_home)
    return 0 if out.get("summary", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
