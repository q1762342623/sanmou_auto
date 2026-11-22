"""功能「**自动拉线与战报收集**」：自动拉线循环 + 在等待时间自动收录战报。

它把「战报自动收录」(`skills/战报自动收录`) 插到「自动拉线」的空闲时间里执行：

- **等战斗结束**的预判等待 / 轮间隔 → 分片等待，到点就插一轮战报收集；
- 没等到等待也没关系：每轮开始、邮件读完、据点面板关闭、战斗结束、
  回家补兵结束这些「世界地图安全点」会检查是否该收集，超时就补一次；
- 收集间隔只有下限（`MIN_INTERVAL_SECONDS`），**不设上限**：单次采集
  跑 7~10 分钟、距上次超过 10 分钟都属正常，不再告警；
- 每次收集结束**无论有没有滑到列表底部都会上传一次**
  （`capture.requireListEnd=false` + 服务器按 BLOB 增量去重）。

每次收集 = `scripts/round.py --no-wait`（采集 → 提取 → 增量上传，不等 /api/live），
之后无论成败都 `scripts/screen.py back-to-map` 把游戏恢复成世界地图，自动拉线继续。

用法:
    python -m sanmou_auto.flow_line_report                     # 跑 1 小时
    python -m sanmou_auto.flow_line_report --minutes 10        # 跑 10 分钟
    python -m sanmou_auto.flow_line_report --max-rounds 1      # 冒烟一轮
    python -m sanmou_auto.flow_line_report --report-interval 240
    python -m sanmou_auto.flow_line_report --report-skip-upload  # 只抓不上传
    python -m sanmou_auto.flow_line_report --no-report           # 等同自动拉线
"""
from __future__ import annotations

import argparse

from . import cli
from .flow_loop import MobilizeLoop
from .mapnav import MapNav
from .mobilize import STRENGTH_FULL
from .report_collector import (MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS,
                               SUBMIT_INTERVAL_SECONDS, ReportCollector)
from .timing import fmt_seconds


def main(argv: list[str] | None = None) -> int:
    cli.setup_console()
    ap = argparse.ArgumentParser(
        description="自动拉线与战报收集：自动拉线循环 + 在等待时间自动收录战报"
                    "（每次收集后都会上传，间隔不设上限）")
    # ---- 自动拉线参数（与 flow_loop 一致）----
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
                    help="出征后不等待战斗结束（会显著减少可插入收集的等待时间）")
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
    ap.add_argument("--no-city-rest", action="store_true",
                    help="关闭「低兵力进城歇兵 / 征满召回」")
    ap.add_argument("--city-rest-state", default="",
                    help="歇兵状态文件（默认 screenshots/flow/city_rest.json）")
    # ---- 战报收集参数 ----
    ap.add_argument("--no-report", action="store_true",
                    help="关闭战报收集（等同自动拉线）")
    ap.add_argument("--report-interval", type=float, default=300.0,
                    help="空闲点两次收集的最小间隔秒数（默认 300，"
                         f"下限 {MIN_INTERVAL_SECONDS:.0f}s；从上次收集结束算起）")
    ap.add_argument("--report-submit-interval", type=float,
                    default=SUBMIT_INTERVAL_SECONDS,
                    help="距上次「提交成功」超过该秒数必须收集（默认 600 = 10 分钟）")
    ap.add_argument("--report-max-seconds", type=int, default=1200,
                    help="单次采集最长秒数（默认 1200；翻到底会提前结束）")
    ap.add_argument("--report-skip-upload", action="store_true",
                    help="战报只采集/提取，不上传服务器")
    ap.add_argument("--report-state", default="",
                    help="收集状态文件（默认 screenshots/flow/report_collect_state.json）")
    ap.add_argument("--no-report-first", action="store_true",
                    help="启动时即使没有历史记录也不立刻收集，等满一个间隔")
    args = ap.parse_args(argv)

    duration = args.seconds if args.seconds is not None else args.minutes * 60.0
    troops = tuple(int(t) for t in args.troops.split(",") if t.strip())

    loop = MobilizeLoop()
    if not args.no_report:
        if args.report_interval < MIN_INTERVAL_SECONDS:
            print(f"! 收集间隔 {args.report_interval:.0f}s 太小，"
                  f"已抬到 {MIN_INTERVAL_SECONDS:.0f}s")
    collector = ReportCollector(
        interval=args.report_interval,
        submit_interval=args.report_submit_interval,
        enabled=not args.no_report,
        log=print,
        prepare=lambda: MapNav(loop.adb).ensure_map(),
        skip_upload=args.report_skip_upload,
        max_seconds=args.report_max_seconds,
        state_path=args.report_state or None,
        collect_on_start=not args.no_report_first,
    )

    out = loop.run(
        duration=duration, limit=args.limit, troops=troops,
        dry_run=args.dry_run, battle=not args.no_battle,
        wait_return=not args.no_wait, battle_timeout=args.battle_timeout,
        min_strength=args.min_strength, replenish=not args.no_replenish,
        enemy_fallback=not args.no_enemy_fallback,
        gap=args.gap, max_rounds=args.max_rounds,
        stop_on_no_strength=not args.no_strength_stop,
        max_fails=args.max_fails, home=not args.no_home,
        city_rest=not args.no_city_rest,
        city_rest_state=args.city_rest_state or None,
        collector=collector)

    summary = out.get("summary") or {}
    status = summary.get("report_collect") or collector.status()
    print("\n===== 战报收集汇总 =====")
    if not status.get("enabled"):
        print("  已关闭（--no-report）")
    else:
        gap = status.get("maxGapSeconds")
        print(f"  收集 {status.get('count')} 次（失败 {status.get('failures')}）")
        print(f"  目标间隔 {status.get('intervalSeconds'):.0f}s（空闲点最小间隔）；"
              f"实际最大间隔 "
              f"{fmt_seconds(gap) if gap is not None else '-'}"
              f"（平均 {fmt_seconds(status.get('avgGapSeconds')) if status.get('avgGapSeconds') is not None else '-'}）")
        submit = status.get("submitIntervalSeconds")
        if submit:
            print(f"  提交间隔 {float(submit):.0f}s（超过必须收集）；"
                  f"最近提交 {status.get('lastSubmitAt') or '-'}"
                  f"（{fmt_seconds(status.get('secondsSinceSubmit')) if status.get('secondsSinceSubmit') is not None else '-'} 前）")
        pending = status.get("pendingUploads") or []
        if pending:
            print(f"  待补交 {len(pending)} 条：{', '.join(pending[:3])}"
                  + (" …" if len(pending) > 3 else ""))
        if status.get("lastStartAt"):
            print(f"  最近一次 {status['lastStartAt']}  "
                  f"captureId={status.get('lastCaptureId') or '-'}  "
                  f"ok={status.get('lastOk')}")
        if status.get("lastError"):
            print(f"  最近失败原因：{status['lastError']}")
        print(f"  状态文件: {status.get('statePath')}")
        print(f"  明细日志: {status.get('logPath')}")
    print(f"  自动拉线明细: {out.get('path')}")
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
