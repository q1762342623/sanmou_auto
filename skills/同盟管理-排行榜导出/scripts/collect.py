"""排行榜采集编排器：抓 → 校验人数 → 不达标自动重抓 → 生成 HTML。

为什么要它
----------
各榜单条数是固定的（繁荣/武勋 300 人、首占榜每等级 100 人、阵营内排行上千人）。
tcpdump 起抓时机、页面切换、缓存命中都可能让某次只抓到一半，所以这里做**硬性校验**：
每个榜单抓完立刻核对行数，不达标就把**该榜单单独再抓一次**（最多 3 次），
全部达标（或次数用尽）后再生成 HTML。

所有榜单共用同一个批次号（`--tag`），这样 HTML 能把几次抓包拼成一页。

用法
----
    python scripts/collect.py                 # 全流程（约 4~6 分钟）
    python scripts/collect.py --tries 4       # 单榜最多重抓 4 次
    python scripts/collect.py --skip-html     # 只采集、不生成页面
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from report import OUT_DIR, build, dedupe, load_rows            # noqa: E402

PROBE = os.path.join(HERE, "probe.py")

# (榜单名, 额外参数, 校验键, 目标行数, 少样本豁免上限)
# 目标行数就是榜单真实人数：繁荣榜 300、武勋榜 300、首占榜（单等级）100。
# 行数据按 sproto 数组解析（每页 50 行 × 页数），所以是精确值，不做「≥290」这种宽松口径。
# 豁免说明：赛季初期武勋榜可能真的只有 0~几十人，少于 SOFT_MIN 视为正常，不重抓。
TARGETS = [
    ("个人繁荣榜", [], "prosperity", 300, None),
    ("个人武勋榜", [], "merit", 300, 50),
    ("个人首占榜", ["--firstocc9"], "first_occ", 100, None),
]
CAMP_MIN_PEOPLE = 100       # 每个阵营至少这么多人，否则认为没抓到


def judge(got: int, need: int, soft_min: int | None) -> tuple[bool, str]:
    """返回 (是否达标, 说明)。目标 = 完整人数（300 / 100）；少样本（< soft_min）算正常。"""
    if got >= need:
        return True, "完整"
    if soft_min is not None and got < soft_min:
        return True, f"少样本（< {soft_min}），赛季初期属正常，不重抓"
    return False, f"不足 {need}"


def run_probe(extra: list[str]) -> None:
    cmd = [sys.executable, PROBE, *extra]
    print("   $", " ".join(["python", "scripts/probe.py", *extra]))
    result = subprocess.run(cmd, cwd=ROOT, check=False)
    if result.returncode != 0:
        print(f"   [warn] rank_probe 退出码 {result.returncode}")


def measure(tag: str) -> dict:
    """统计这一批次里各榜单的可解析行数与阵营人数。"""
    try:
        buckets, meta = load_rows(tag)
    except SystemExit:
        return {"prosperity": 0, "merit": 0, "first_occ": 0, "camps": {}}
    camps = {name: len(order or [])
             for name, order in (meta.get("campOrders") or {}).items()}
    unions = {name: set(order or [])
              for name, order in (meta.get("campOrders") or {}).items()}
    return {
        "prosperity": len(dedupe(buckets.get("prosperity", []))),
        "merit": len(dedupe(buckets.get("merit", []))),
        "first_occ": len(dedupe(buckets.get("first_occ", []))),
        "camps": camps,
        "campUnions": unions,
    }


def camps_ok(stat: dict) -> bool:
    camps = stat.get("camps") or {}
    if len(camps) < 2:
        return False
    if any(people < CAMP_MIN_PEOPLE for people in camps.values()):
        return False
    names = list((stat.get("campUnions") or {}).values())
    # 两个阵营的同盟集合不应有交集（有交集说明数据串阵营了）
    return not (set.intersection(*[set(n) for n in names])) if len(names) >= 2 else False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="排行榜采集编排（含人数硬校验）")
    parser.add_argument("--tries", type=int, default=3, help="单榜最多抓几次")
    parser.add_argument("--tag", default="", help="批次号（默认当前时间）")
    parser.add_argument("--skip-html", action="store_true")
    args = parser.parse_args(argv)

    tag = args.tag or time.strftime("%Y%m%d_%H%M%S")
    print(f"=== 批次 {tag}，单榜最多抓 {args.tries} 次\n")

    results: dict[str, tuple[int, bool, str]] = {}
    for board, extra, key, need, soft_min in TARGETS:
        rule = f"目标 {need} 行（榜单完整人数）"
        if soft_min is not None:
            rule += f"（不足 {soft_min} 行视为赛季初期正常，不重抓）"
        print(f"\n== {board}（{rule}）")
        got = 0
        ok = False
        note = ""
        for attempt in range(1, args.tries + 1):
            print(f"  第 {attempt} 次抓取")
            run_probe(["--only", board, "--tag", tag, *extra])
            got = measure(tag)[key]
            ok, note = judge(got, need, soft_min)
            print(f"  → 实际 {got} 行（{note}）")
            if ok:
                break
            print("  [retry] 不足，重新抓这个榜单")
        results[board] = (got, ok, note)

    print("\n== 阵营内排行（两个阵营都要有数据且同盟不重合）")
    camp_stat: dict = {}
    for attempt in range(1, args.tries + 1):
        print(f"  第 {attempt} 次抓取")
        run_probe(["--only", "__none__", "--extra", "--tag", tag])
        camp_stat = measure(tag)
        camps = camp_stat.get("camps") or {}
        print(f"  → {camps}")
        if camps_ok(camp_stat):
            break
        print("  [retry] 阵营数据不合格，重新抓")

    print("\n=== 结果")
    for board, (got, ok, note) in results.items():
        print(f"  {board}: {got} 行 {'OK' if ok else '不足!'}（{note}）")
    camps = camp_stat.get("camps") or {}
    print(f"  阵营: {camps} {'OK' if camps_ok(camp_stat) else '不足!'}")
    bad = [b for b, (_g, ok, _n) in results.items() if not ok]
    if not camps_ok(camp_stat):
        bad.append("阵营内排行")

    if args.skip_html:
        print("\n--skip-html：不生成页面")
        return 0 if not bad else 1

    out = os.path.join(OUT_DIR, f"rank_report_{tag}.html")
    build(tag, out)
    build(tag, os.path.join(OUT_DIR, "rank_report_latest.html"))
    print(f"\nHTML -> {out}")
    print(f"最新 -> {os.path.join(OUT_DIR, 'rank_report_latest.html')}")
    if bad:
        print(f"\n[注意] 以下数据不足，页面已按现有数据生成：{'、'.join(bad)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
