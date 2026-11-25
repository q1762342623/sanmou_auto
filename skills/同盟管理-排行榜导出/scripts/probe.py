"""点「排行」抓包：逐个榜单截图 + 解析 tcpdump 里的榜单数据。

为什么单独写一个探针，而不是复用战报收集
----------------------------------------
战报收集只关心「战报卡片」那一类对象；排行榜的响应是**另一组 RPC**，
字段结构也不同（名次 / 名称 / 数值 / 州 / 同盟…）。本工具做三件事：

1. `--capture`（默认）：OCR 找「排行」按钮 → 进排行页 → 逐个页签 / 榜单点击，
   每一步都截图并记录**设备时间窗**（tcpdump 的帧时间戳就是设备 epoch）；
2. 抓包全程开着（root + 设备内 tcpdump），结束 pull 回本地；
3. `--analyze`：解出每帧的 RPC 前缀（2 字节 opcode）、字符串、整数、数组行，
   按时间窗**把帧归到具体榜单**，再用截图 OCR 文本反查「这段文字出现在哪一帧」，
   直接证明榜单数据来自哪个请求。

用法
----
    python scripts/probe.py                     # 全流程（约 60~90s）
    python scripts/probe.py --scroll 2          # 每个榜单多滑 2 次（触发分页）
    python scripts/probe.py --scope 1           # 额外切一次「全服」下拉第 1 项
    python scripts/probe.py --analyze <pcap>    # 只解析已有 pcap
    python scripts/probe.py --no-ocr            # 不做 OCR 反查（更快）
    python scripts/probe.py --no-capture        # 只解析最近一次抓的 pcap

产物（默认 `screenshots/flow/rank_probe/`）：
    rank_<ts>.pcap          原始抓包
    step_<i>_<榜单>.jpg     每个榜单的截图
    rank_frames_<ts>.jsonl  每帧解码结果（opcode / 文本 / 整数 / 数组行）
    rank_report_<ts>.json   按榜单归档的汇总 + OCR 反查命中
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcap_codec import ROOT            # noqa: E402
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sanmou_auto import config as cfg  # noqa: E402
from sanmou_auto import vision         # noqa: E402
from sanmou_auto.adb import Adb        # noqa: E402
from sanmou_auto.mapnav import MapNav  # noqa: E402
from pcap_codec import (               # noqa: E402
    ENGINE, CJK_RE, PRINTABLE_RE, cards, decode_frame, discover_key,
    iter_frames, pc, utf8_runs, walk_object,
)

OUT_DIR = os.path.join(ROOT, "screenshots", "flow", "rank_probe")
COORD_MIN, COORD_MAX = 1, 2000

# --------------------------------------------------------------------------- #
# UI 坐标（竖屏实测；「排行」按钮是艺术字，OCR 读不出来，只能固定坐标）
# --------------------------------------------------------------------------- #
GRID_TOGGLE = cfg._p((1413, 835), (833, 1522))
# 展开网格后第三行第三个（战法 / 霸业 / 排行 / 背包）
RANK_BTN = cfg._p((1413, 835), (521, 1452))
RANK_BACK = cfg._p((150, 1500), (150, 1505))
MODAL_CLOSE = cfg._p((1500, 95), (810, 315))
SCOPE_DROPDOWN = cfg._p((1456, 838), (725, 1279))
# 竖屏列表可视区约 y=140..1120，下面 y≈1130 起是固定的「未入榜」行，
# 从 1150 起手的手势落在固定行上 → 列表根本不滚（实测 deep 截图一直停在第 1 名）。
LIST_SWIPE_UP = cfg._p((1400, 700, 1400, 250), (450, 1000, 450, 420))
LIST_SWIPE_DOWN = cfg._p((1400, 250, 1400, 700), (450, 420, 450, 1000))

# 排行页底部三个页签（个人 / 同盟 / 阵营）
TAB_POS = {
    "个人": cfg._p((155, 838), (155, 1425)),
    "同盟": cfg._p((450, 838), (450, 1425)),
    "阵营": cfg._p((730, 838), (730, 1425)),
}

# 个人页签的榜单横条：右侧还藏两项，需要横拖约一屏才露出来
BOARD_ROW_SWIPE = cfg._p((1500, 838, 60, 838), (830, 1344, 60, 1344))
# 赛季积分榜没用，不再采集；只补「阵营内排行」（要逐阵营各抓一段）
EXTRA_BOARDS: list[tuple[str, str, tuple[int, int]]] = [
    ("个人", "阵营内排行", cfg._p((760, 838), (723, 1344))),
]
CAMP_NAMES = ("讨逆军", "太平道")

# 右上角下拉（首占榜=等级，阵营内排行=阵营）——同一个位置，点开后是浮动列表
DROPDOWN_OPEN = cfg._p((1456, 838), (728, 1279))
# 等级列表展开后会盖住 select 本身，需先向上滑一小段让「9级」整行露出
LEVEL_LIST_SWIPE = cfg._p((1500, 838, 1500, 700), (770, 1190, 770, 1090))
LEVEL9_FALLBACK = cfg._p((760, 838), (760, 1063))
FIRSTOCC_LEVEL = "9级"

# 同页签的「兄弟榜」：点它再点回目标榜，能强制服务端重新下发
# （否则点「已选中」的按钮不会发请求 —— 实测繁荣榜段只有 8KB）
SIBLING = {
    "个人繁荣榜": "个人武勋榜",
    "个人武勋榜": "个人繁荣榜",
    "个人首占榜": "个人繁荣榜",
    "同盟": "",
    "霸业积分榜": "",
    "阵营积分榜": "霸业名额榜",
    "霸业名额榜": "阵营积分榜",
}

# 各榜单固定条数（实测）：首占榜**每个等级只有 100 人**（2 帧 × 约 50 行），
# 不要把 12级 和 9级 两批相加当成一个榜 200 人。
EXPECTED_ROWS = {
    "个人繁荣榜": 300,
    "个人武勋榜": 300,
    "个人首占榜": 100,
    "霸业积分榜": 7,
}

# （页签, 榜单名, 榜单按钮坐标）——按钮坐标竖屏实测
BOARDS: list[tuple[str, str, tuple[int, int]]] = [
    ("同盟", "霸业积分榜", cfg._p((160, 838), (145, 1338))),
    ("同盟", "城池积分", cfg._p((760, 838), (500, 1275))),
    ("个人", "个人繁荣榜", cfg._p((160, 838), (160, 1344))),
    ("个人", "个人武勋榜", cfg._p((470, 838), (437, 1344))),
    ("个人", "个人首占榜", cfg._p((760, 838), (700, 1344))),
    ("阵营", "阵营积分榜", cfg._p((160, 838), (150, 1340))),
    ("阵营", "霸业名额榜", cfg._p((470, 838), (430, 1340))),
]


# --------------------------------------------------------------------------- #
# OCR 驱动点击
# --------------------------------------------------------------------------- #
def _ocr_spans(img: Image.Image, scale: float = 2.0):
    from sanmou_auto import ocrengine
    return ocrengine.ocr(img, scale=scale)


def find_span(adb: Adb, text: str, *, exact: bool = False, bottom_only: bool = False):
    """在当前画面里 OCR 找一段文字，返回 (span, 截图)。"""
    img = adb.screencap()
    spans = _ocr_spans(img)
    cands = [s for s in spans if (s.text == text if exact else text in s.text)]
    if bottom_only:
        cands = [s for s in cands if s.cy > img.height * 0.55]
    if not cands:
        return None, img
    # 同一文字多处出现时取最靠下的（页签/榜单按钮都在底部）
    cands.sort(key=lambda s: s.cy)
    return cands[-1], img


def tap_text(adb: Adb, text: str, *, exact: bool = False, bottom_only: bool = False,
             fallback: tuple[int, int] | None = None, wait: float = 1.0) -> bool:
    span, img = find_span(adb, text, exact=exact, bottom_only=bottom_only)
    if span is not None:
        print(f"   tap「{span.text}」({span.cx:.0f},{span.cy:.0f})")
        adb.tap(span.cx, span.cy)
        time.sleep(wait)
        return True
    if fallback is not None:
        print(f"   OCR 没看到「{text}」，用兜底坐标 {fallback}")
        adb.tap(*fallback)
        time.sleep(wait)
        return True
    print(f"   × 找不到「{text}」")
    return False


def select_firstocc_level(adb: Adb, level: str = FIRSTOCC_LEVEL) -> bool:
    """首占榜右上角等级下拉：展开 → 上滑 → 点 9 级。"""
    adb.tap(*DROPDOWN_OPEN)
    time.sleep(1.3)
    adb.swipe(*LEVEL_LIST_SWIPE, 400)
    time.sleep(0.9)
    span, _img = find_span(adb, level)
    if span is None:
        print(f"   [warn] 下拉看不到「{level}」，用兜底坐标 {LEVEL9_FALLBACK}")
        adb.tap(*LEVEL9_FALLBACK)
    else:
        print(f"   选「{span.text}」({span.cx:.0f},{span.cy:.0f})")
        adb.tap(span.cx, span.cy)
    time.sleep(2.4)
    # 校验：select 上应显示所选等级
    span, _img = find_span(adb, level)
    return span is not None


def select_dropdown_item(adb: Adb, name: str) -> bool:
    """阵营内排行右上角阵营下拉：展开 → OCR 找到阵营名 → 点击。"""
    adb.tap(*DROPDOWN_OPEN)
    time.sleep(1.3)
    span, _img = find_span(adb, name)
    if span is None:
        print(f"   [warn] 下拉里没有「{name}」")
        return False
    print(f"   选「{span.text}」({span.cx:.0f},{span.cy:.0f})")
    adb.tap(span.cx, span.cy)
    time.sleep(2.4)
    return True


def deep_scroll(adb: Adb, shots: list[str], prefix: str, limit: int) -> int:
    """一直往下拖，直到画面连续 2 次不再变化（榜单懒加载/分页到底）。"""
    if limit <= 0:
        return 0
    from sanmou_auto.fastwait import signature

    last = None
    same = 0
    moves = 0
    for i in range(limit):
        adb.swipe(*LIST_SWIPE_UP, 420)
        time.sleep(1.4)
        img = adb.screencap()
        moves += 1
        sig = signature(img)
        if sig == last:
            same += 1
            print(f"      第 {i + 1} 次拖动画面无变化（连续 {same} 次）")
            if same >= 2:
                break
        else:
            same = 0
        last = sig
        if i % 5 == 0:
            path = f"{prefix}_deep{i + 1:02d}.jpg"
            vision.save_jpg(img, path, 900, 78)
            shots.append(path)
    return moves


def grid_expanded(adb: Adb) -> bool:
    """网格展开后才有「背包」这一格；用它判断当前是不是展开态。

    不能用「排行」判断：那个按钮是艺术字，RapidOCR 认不出来
    （实测整屏 OCR 只漏掉「排行」，其余格子都能读）。
    """
    span, _img = find_span(adb, "背包", exact=True, bottom_only=True)
    return span is not None


def ensure_rank_open(adb: Adb, nav: MapNav) -> None:
    """回到地图 → 展开功能网格 → 点「排行」（固定坐标）并确认页签出现。"""
    nav.ensure_map()
    adb.tap(*cfg.MAP_BLANK_POINT)
    time.sleep(0.5)
    for attempt in range(3):
        if not grid_expanded(adb):
            print(f"   [grid] 展开功能网格 {GRID_TOGGLE}")
            adb.tap(*GRID_TOGGLE)
            time.sleep(1.3)
        adb.tap(*RANK_BTN)
        time.sleep(1.9)
        # 排行榜底部页签是常规字体，OCR 能稳定读到「阵营」
        span, _img = find_span(adb, "阵营", exact=True, bottom_only=True)
        if span is not None:
            print("   已进入排行榜")
            return
        print(f"   [rank] 第 {attempt + 1} 次没进排行榜，重试")
    raise RuntimeError("打不开排行榜")


# --------------------------------------------------------------------------- #
# 抓包
# --------------------------------------------------------------------------- #
def dev_epoch(adb: Adb) -> float:
    """设备 epoch（pcap 时间戳用的就是设备时钟）。"""
    out = (adb.shell("date", "+%s") or "").strip()
    try:
        return float(out.splitlines()[-1])
    except (ValueError, IndexError):
        return time.time()


def discover_ports(adb: Adb) -> list[int]:
    ports: list[int] = []
    for command in (("ss", "-tnp"), ("netstat", "-tnp")):
        out = adb.shell(*command) or ""
        for line in out.splitlines():
            if "nslg" not in line:
                continue
            tokens = [t for t in line.split() if ":" in t]
            if len(tokens) < 2:
                continue
            peer = tokens[1].replace("[", "").replace("]", "")
            _, _, port = peer.rpartition(":")
            if port.isdigit() and 10000 <= int(port) <= 30000:
                ports.append(int(port))
        if ports:
            break
    return sorted(set(ports)) or list(pc.DEFAULT_GAME_PORTS)


def start_tcpdump(adb: Adb, tag: str) -> dict:
    ports = discover_ports(adb)
    filt = " or ".join(f"tcp port {p}" for p in ports)
    remote = f"/data/local/tmp/rank_{tag}.pcap"
    cmd = f"/system/bin/tcpdump -i any -s 0 -U -w {remote} '{filt}' > /dev/null 2>&1 & echo $!"
    pid = (adb.shell(cmd) or "").strip().splitlines()[-1:]
    pid = pid[0].strip() if pid else ""
    if not pid.isdigit():
        raise RuntimeError("tcpdump 启动失败（需要 adb root 与设备内 tcpdump）")
    print(f"tcpdump pid={pid} 端口={ports}")
    return {"pid": pid, "remote": remote, "ports": ports, "filter": filt}


def stop_tcpdump(adb: Adb, handle: dict, local: str) -> str:
    # 多点这一秒很关键：排行榜的响应常在点击后 2~3s 才到，
    # 窗口太短会漏掉（实测「阵营积分榜」只抓到 30 字节，数据跑到下一段里）。
    time.sleep(1.2)
    adb.shell("kill", "-2", handle["pid"])
    time.sleep(1.5)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    try:
        adb._run("pull", handle["remote"], local)
    finally:
        adb.shell("rm", "-f", handle["remote"])
    print("pcap ->", local, f"({os.path.getsize(local)} B)")
    return local


def capture(adb: Adb, args) -> list[dict]:
    """每个榜单单独一段抓包。

    为什么不能一次性抓、再按时间归并：解码链会把整条 TCP 流合并成一个 chunk，
    所有帧共用 chunk 的**第一个包**时间戳（实测 66 帧全是同一时刻），
    时间窗完全失效。改成「一段包 = 一个榜单」，归属就只取决于文件名。
    """
    # 允许外部指定批次号：多榜单分开抓时共用同一个 tag，HTML 才能拼成一页
    tag0 = (getattr(args, "tag", "") or "").strip() or time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(OUT_DIR, exist_ok=True)
    nav = MapNav(adb)
    sources: list[dict] = []

    if getattr(args, "cold", False):
        # 榜单数据有客户端缓存：同一会话内第二次进「阵营」不会再发请求，
        # 想抓到全部榜单的首次下发就必须冷启动一次。
        print("== 冷启动游戏（清榜单缓存）")
        adb.force_stop()
        time.sleep(1.5)
        adb.launch()
        for i in range(40):
            time.sleep(3)
            try:
                if nav.on_map():
                    print(f"   已回到地图（{3 * (i + 1)}s）")
                    break
            except Exception:      # noqa: BLE001
                pass
        else:
            print("   [warn] 60s 内没确认回到地图，继续尝试")

    def window(label: str, slug: str, actions, min_bytes: int = 1500) -> None:
        shots: list[str] = []
        pcap = ""
        for attempt in range(2):
            handle = start_tcpdump(adb, f"{tag0}_{slug}")
            try:
                shots = actions()
            finally:
                pcap = stop_tcpdump(
                    adb, handle, os.path.join(OUT_DIR, f"rank_{tag0}_{slug}.pcap"))
            size = os.path.getsize(pcap)
            if size >= min_bytes or attempt:
                break
            # 响应可能在点击后 3~4s 才到；抓到太少就再点一遍（服务端会重发）
            print(f"   [retry] {label} 只抓到 {size}B，再取一次")
            time.sleep(0.8)
        sources.append({"label": label, "pcap": pcap, "shots": shots,
                        "bytes": os.path.getsize(pcap)})
        print(f"   -> {label} 完成（{os.path.getsize(pcap)}B）")

    print("\n== [0] 打开排行榜")
    def _open() -> list[str]:
        ensure_rank_open(adb, nav)
        path = os.path.join(OUT_DIR, "step_00_打开排行.jpg")
        vision.save_jpg(adb.screencap(), path, 900, 78)
        return [path]
    window("打开排行", "open", _open)

    only = {name.strip() for name in (getattr(args, "only", "") or "").split(",")
            if name.strip()}
    for i, (tab, board, board_pos) in enumerate(BOARDS):
        if only and board not in only:
            continue
        label = f"{tab}/{board}"
        print(f"\n== [{i + 1}/{len(BOARDS)}] {label}")
        # 先切页签、等「默认榜」加载完，再开始抓包：
        # 否则窗口里会混进默认榜的数据（实测 05 段混了繁荣榜 6 帧、04 段混了繁荣+武勋两批）。
        print(f"   先切页签「{tab}」并等默认榜加载完")
        adb.tap(*TAB_POS[tab])
        time.sleep(2.4)

        # 首占榜：先把榜单点开（默认 12 级，数据在窗口外），窗口内只做「切 9 级」，
        # 这样一段 pcap 里就只剩 9 级那 100 行，不会和 12 级叠成 200。
        pre_level = board == "个人首占榜" and getattr(args, "firstocc9", False)
        if pre_level:
            print("   窗口外先点开首占榜（加载 12 级）")
            adb.tap(*board_pos)
            time.sleep(2.8)

        def _act(i=i, board=board, pos=board_pos, pre=pre_level) -> list[str]:
            repeat = max(1, int(getattr(args, "repeat", 1) or 1))
            if pre:
                print("   窗口内切「9 级」")
                if not select_firstocc_level(adb):
                    print("   [warn] 9 级可能没切成功")
            else:
                sibling = SIBLING.get(board) or ""
                sibling_pos = next((p for _t, b, p in BOARDS if b == sibling), None)
                for round_no in range(repeat):
                    if sibling_pos:
                        print(f"   第 {round_no + 1} 轮：点兄弟榜「{sibling}」"
                              f"强制刷新，再点回「{board}」")
                        adb.tap(*sibling_pos)
                        time.sleep(2.2)
                    else:
                        print(f"   第 {round_no + 1} 轮：点榜单「{board}」{pos}")
                    adb.tap(*pos)
                    time.sleep(3.0)
            shots = []
            path = os.path.join(OUT_DIR, f"step_{i + 1:02d}_{board}.jpg")
            vision.save_jpg(adb.screencap(), path, 900, 78)
            shots.append(path)
            deep = int(getattr(args, "deep", 0) or 0)
            if deep:
                moved = deep_scroll(
                    adb, shots,
                    os.path.join(OUT_DIR, f"step_{i + 1:02d}_{board}"), deep)
                print(f"   连续下拖 {moved} 屏（到画面不再变化为止）")
            elif args.scroll:
                for k in range(int(args.scroll)):
                    adb.swipe(*LIST_SWIPE_UP, 420)
                    time.sleep(1.3)
                    p = os.path.join(OUT_DIR, f"step_{i + 1:02d}_{board}_s{k + 1}.jpg")
                    vision.save_jpg(adb.screencap(), p, 900, 78)
                    shots.append(p)
                for _ in range(int(args.scroll)):
                    adb.swipe(*LIST_SWIPE_DOWN, 420)
                    time.sleep(0.9)
            # 弹窗（城池积分）要关掉，否则后面的页签点不到
            if board == "城池积分":
                adb.tap(*MODAL_CLOSE)
                time.sleep(0.9)
            return shots

        window(label, f"{i + 1:02d}_{board}", _act)

    if getattr(args, "extra", False):
        print("\n== 个人页签：横拖榜单条 → 阵营内排行（逐阵营各抓一段）")
        adb.tap(*TAB_POS["个人"])
        time.sleep(1.5)
        print(f"   横拖榜单条 {BOARD_ROW_SWIPE}")
        adb.swipe(*BOARD_ROW_SWIPE, 600)
        time.sleep(1.3)
        tab, board, pos = EXTRA_BOARDS[0]
        print(f"   榜单「{board}」{pos}")
        adb.tap(*pos)
        time.sleep(3.0)

        camps = CAMP_NAMES if getattr(args, "camps", True) else (None,)
        for camp in camps:
            # 「先切另一阵营」放在窗口外：否则同一段 pcap 里会同时出现两个阵营的数据，
            # 统计时同盟会互相污染（实测太平道段里出现了与讨逆军完全相同的计数）。
            if camp:
                other = CAMP_NAMES[1] if camp == CAMP_NAMES[0] else CAMP_NAMES[0]
                print(f"   窗口外先切到「{other}」预热（保证下面切回会重新请求）")
                select_dropdown_item(adb, other)
                time.sleep(2.0)

            def _act(camp=camp) -> list[str]:
                if camp:
                    print(f"   窗口内切回「{camp}」")
                    if not select_dropdown_item(adb, camp):
                        print("   [warn] 阵营可能没切成功，仍抓当前页")
                    time.sleep(1.2)
                suffix = f"_{camp}" if camp else ""
                shots = []
                path = os.path.join(OUT_DIR,
                                    f"step_x01_阵营内排行{suffix}.jpg")
                vision.save_jpg(adb.screencap(), path, 900, 78)
                shots.append(path)
                deep = int(getattr(args, "deep", 0) or 0)
                if deep:
                    moved = deep_scroll(
                        adb, shots,
                        os.path.join(OUT_DIR, f"step_x01_阵营内排行{suffix}"), deep)
                    print(f"   连续下拖 {moved} 屏")
                return shots

            name = f"{board}[{camp}]" if camp else board
            window(f"{tab}/{name}", f"x01{camp or ''}", _act, min_bytes=20000)

    if args.scope:
        print("\n== 切换榜单范围（全服下拉）")

        def _scope() -> list[str]:
            tap_text(adb, "全服", bottom_only=True, fallback=SCOPE_DROPDOWN, wait=1.0)
            img = adb.screencap()
            spans = [s for s in _ocr_spans(img)
                     if len(s.text) <= 6 and s.text not in ("全服", "排行榜")]
            if spans:
                spans.sort(key=lambda s: s.cy)
                pick = spans[min(int(args.scope), len(spans) - 1)]
                print(f"   选第 {args.scope} 项「{pick.text}」")
                adb.tap(pick.cx, pick.cy)
                time.sleep(1.8)
            path = os.path.join(OUT_DIR, "step_99_范围切换.jpg")
            vision.save_jpg(adb.screencap(), path, 900, 78)
            return [path]

        window("范围切换", "scope", _scope)

    # 退回地图，别把界面停在排行页
    try:
        tap_text(adb, "返回", fallback=RANK_BACK, wait=1.5)
        nav.ensure_map()
    except Exception as exc:      # noqa: BLE001
        print("回地图失败（不影响解析）:", exc)
    return sources


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def decode_with_opcode(frame, key: bytes) -> tuple[int | None, bytes | None]:
    """返回 (RPC opcode, sproto-like 对象字节)。"""
    if len(frame.data) <= pc.TRANSPORT_HEADER_SIZE + pc.RPC_PREFIX_SIZE:
        return None, None
    raw = pc.xor_bytes(frame.data[pc.TRANSPORT_HEADER_SIZE:], key)
    opcode = int.from_bytes(raw[:pc.RPC_PREFIX_SIZE], "little")
    return opcode, raw[pc.RPC_PREFIX_SIZE:]


def _row_of(data: bytes, base: int, size: int) -> dict | None:
    fields = cards.try_parse_object(data, base, size)
    if fields is None:
        return None
    texts: list[str] = []
    ints: list[int] = []
    coords: list[tuple[int, int]] = []
    for f in fields:
        if f.kind == "small":
            ints.append(int(f.value))
            continue
        if f.start is None or f.size is None:
            continue
        if f.size == 9:
            x = cards.read_u32(data, f.start + 1)
            y = cards.read_u32(data, f.start + 5)
            if COORD_MIN <= x <= COORD_MAX and COORD_MIN <= y <= COORD_MAX:
                coords.append((x, y))
        if f.size == 4:
            value = cards.read_u32(data, f.start)
            if value < 2 ** 31:
                ints.append(value)
        text = cards.decode_utf8(data, f.start, f.size)
        if text:
            clean = text.strip()
            if len(clean) >= 1 and PRINTABLE_RE.match(clean) and CJK_RE.search(clean):
                texts.append(clean)
    if not (texts or ints or coords):
        return None
    return {"texts": texts, "ints": ints, "coords": coords}


def collect_arrays(data: bytes, base: int = 0, size: int | None = None,
                   depth: int = 0, max_depth: int = 4) -> list[dict]:
    """从帧里挖出「数组对象」，数组的每一项 = 一行（榜单一行）。"""
    out: list[dict] = []
    if depth > max_depth:
        return out
    total = len(data) if size is None else size
    fields = cards.try_parse_object(data, base, total)
    if fields is None:
        return out
    for f in fields:
        if f.kind == "small" or f.start is None or f.size is None:
            continue
        items = cards.try_parse_array(data, f.start, f.size)
        if items:
            rows = []
            for ib, isz, _fields in items:
                row = _row_of(data, ib, isz)
                if row is not None:
                    rows.append(row)
            if rows:
                out.append({"tag": f.tag, "rows": rows})
            continue
        out.extend(collect_arrays(data, f.start, f.size, depth + 1, max_depth))
    return out


def _flatten(node: dict, strings: list, ints: list) -> None:
    strings.extend(node.get("strings") or [])
    ints.extend(node.get("ints") or [])
    for child in node.get("children") or []:
        _flatten(child, strings, ints)


# 榜单行标记（实测）：`02 00 00 00 04 00 00 00` + u32 分数 + u32 + u32 + u32，
# 之后是 `unionName\0<联盟名>\0unionId\0coord\0roleId\0bornStateId\0`。
# 玩家名不在这段里（只有 roleId），名字来自另一条名字缓存 RPC。
ROW_MARK = b"\x02\x00\x00\x00\x04\x00\x00\x00"
UNION_KEY = b"unionName\x00"


def scan_records(data: bytes) -> list[dict]:
    """按行标记切出「分数 + 联盟名」，用于直接看榜单内容。"""
    out: list[dict] = []
    pos = 0
    while True:
        start = data.find(ROW_MARK, pos)
        if start < 0 or start + 24 > len(data):
            break
        score = int.from_bytes(data[start + 8:start + 12], "little")
        first = int.from_bytes(data[start + 12:start + 16], "little")
        second = int.from_bytes(data[start + 16:start + 20], "little")
        union = None
        holder = data.find(UNION_KEY, start, min(len(data), start + 600))
        if holder >= 0:
            text_start = holder + len(UNION_KEY)
            text_end = data.find(b"\x00", text_start,
                                 min(len(data), text_start + 128))
            if text_end > text_start:
                try:
                    union = data[text_start:text_end].decode("utf-8")
                except UnicodeDecodeError:
                    union = None
        out.append({"off": start, "score": score, "a": first, "b": second,
                    "union": union})
        pos = start + len(ROW_MARK)
    return out


def scan_rows(data: bytes) -> list[dict]:
    """按 **sproto 数组**解析榜单行，一行 = 榜单上一个人（行序即名次序）。

    为什么不继续用 `scan_records`
    ----------------------------
    `scan_records` 靠「行标记 `02 00 00 00 04 00 00 00` + 分数」切行，个别行
    的编码变体（分数/名次字段被省略）会漏掉，实测每帧 50 行只切出 48~49 行，
    300 人的榜最后只剩 293~295 行。榜单真正的数据在帧里的**一个数组字段**里
    （每页固定 50 项），用 `collect_arrays` 逐项解析即可拿到完整 50 行。

    同盟名不在数组项里，而是每行的嵌套对象中（`unionName\\0<名字>\\0`），
    实测每帧 unionName 个数与数组项数相等且顺序一致，故按位置配对。
    """
    items: list[dict] = []
    for arr in collect_arrays(data):
        if len(arr["rows"]) >= 10:          # 榜单行数组（每页 50 行）
            items = arr["rows"]
            break
    if not items:
        return []
    names: list[str] = []
    pos = 0
    while True:
        holder = data.find(UNION_KEY, pos)
        if holder < 0:
            break
        text_start = holder + len(UNION_KEY)
        text_end = data.find(b"\x00", text_start,
                             min(len(data), text_start + 128))
        name = ""
        if text_end > text_start:
            try:
                name = data[text_start:text_end].decode("utf-8").strip()
            except UnicodeDecodeError:
                name = ""
        names.append(name)
        pos = text_start
    out: list[dict] = []
    for idx, row in enumerate(items):
        ints = row.get("ints") or []
        score = ints[0] if ints else 0
        rank = ints[1] if len(ints) > 1 else idx + 1
        # a/b 只是保留旧字段名（HTML 去重用），这里用名次填充
        out.append({"off": 0, "score": score, "a": rank,
                    "b": ints[2] if len(ints) > 2 else rank,
                    "rank": rank,
                    "union": names[idx] if idx < len(names) else ""})
    return out


def analyze(sources: list[dict],
            ocr_hints: dict[str, list[str]] | None = None) -> dict:
    """sources: [{label, pcap, shots}]，一个元素 = 一个榜单的一段抓包。

    XOR key 是**每次连接随机**的，且同一局内所有窗口共用一把；
    单段包帧太少时 `discover_key` 容易挑错（实测 03/04 段只解出 2 帧，
    解出来的中文全是乱码）。所以先拿**全部段的帧合集**求一把全局 key。
    """
    loaded: list[tuple[dict, list, list]] = []
    all_frames: list = []
    for src in sources:
        frames, skipped = iter_frames(src["pcap"])
        loaded.append((src, frames, skipped))
        all_frames.extend(frames)
    if not all_frames:
        raise SystemExit("没有抓到任何帧")
    gkey, gscore, _cands = discover_key(all_frames)
    print(f"\n全局 XOR key={gkey.hex()}（可解 {gscore}/{len(all_frames)} 帧）")

    rows: list[dict] = []
    for src, frames, skipped in loaded:
        label = src["label"]
        pcap_path = src["pcap"]
        key = gkey
        if not any(decode_frame(f.data, gkey) for f in frames):
            key, _s, _c = discover_key(frames)
            print(f"   [warn] {label} 用全局 key 解不出，退回本地 key={key.hex()}")
        good = sum(1 for f in frames if decode_frame(f.data, key))
        src["key"] = key.hex()
        src["frames"] = len(frames)
        print(f"\n解码 [{label}] {os.path.basename(pcap_path)}："
              f"帧 {len(frames)}（跳过 {len(skipped)}），可解 {good}")
        for frame in frames:
            opcode, data = decode_with_opcode(frame, key)
            if data is None:
                continue
            tree = walk_object(data, 0, len(data))
            strings: list[str] = []
            ints: list[int] = []
            _flatten(tree, strings, ints)
            raw_texts = [t for t in utf8_runs(data) if CJK_RE.search(t)]
            arrays = collect_arrays(data)
            records = scan_records(data)
            if not (strings or ints or arrays or raw_texts or records):
                continue
            rows.append({
                "index": frame.index,
                "pcap": os.path.basename(pcap_path),
                "opcode": opcode,
                "bytes": len(frame.data),
                "step": label,
                "texts": list(dict.fromkeys(strings + raw_texts))[:60],
                "ints": ints[:80],
                "arrays": [{"tag": a["tag"], "count": len(a["rows"]),
                            "rows": a["rows"][:12]} for a in arrays][:6],
                "recordCount": len(records),
                "records": records[:20],
            })

    # opcode 汇总
    stat: dict[int, dict] = defaultdict(lambda: {"count": 0, "bytes": 0,
                                                 "steps": Counter(),
                                                 "texts": [], "rows": 0})
    for row in rows:
        s = stat[row["opcode"]]
        s["count"] += 1
        s["bytes"] += row["bytes"]
        s["steps"][row["step"]] += 1
        s["rows"] += sum(a["count"] for a in row["arrays"])
        for text in row["texts"]:
            if text not in s["texts"]:
                s["texts"].append(text)

    print(f"\n可解帧 {len(rows)}，涉及 opcode {len(stat)} 个：")
    print(f"{'op':>6} {'帧数':>5} {'总字节':>9} {'数组行':>6}  归属榜单")
    for opcode, s in sorted(stat.items(), key=lambda kv: -kv[1]["bytes"])[:24]:
        where = ",".join(f"{k}×{v}" for k, v in s["steps"].most_common(3))
        print(f"{opcode:>6} {s['count']:>5} {s['bytes']:>9} {s['rows']:>6}  {where}")
        if s["texts"]:
            print(f"       文本样例: {s['texts'][:10]}")

    # 按榜单归并
    by_step: dict[str, dict] = defaultdict(lambda: {"frames": 0, "bytes": 0,
                                                    "opcodes": Counter(),
                                                    "texts": [], "rows": [],
                                                    "records": [],
                                                    "recordCount": 0,
                                                    "frameRows": [],
                                                    "uniqueRows": set()})
    for row in rows:
        bucket = by_step[row["step"]]
        bucket["frames"] += 1
        bucket["bytes"] += row["bytes"]
        bucket["opcodes"][row["opcode"]] += 1
        bucket["recordCount"] += row.get("recordCount") or 0
        if row.get("recordCount"):
            bucket["frameRows"].append(row["recordCount"])
        for rec in row.get("records") or []:
            bucket["uniqueRows"].add((rec["score"], rec["a"], rec["b"],
                                      rec["union"] or ""))
        for text in row["texts"]:
            if text not in bucket["texts"]:
                bucket["texts"].append(text)
        for a in row["arrays"]:
            bucket["rows"].extend(a["rows"][:6])
        if len(bucket["records"]) < 60:
            bucket["records"].extend(row.get("records") or [])

    print("\n按榜单归并：")
    for label, bucket in by_step.items():
        ops = ",".join(f"{o}×{n}" for o, n in bucket["opcodes"].most_common(4))
        board = label.split("/")[-1].split("[")[0]
        expect = EXPECTED_ROWS.get(board)
        win = bucket["frameRows"][:10]
        print(f"  {label:<28} 帧 {bucket['frames']:>3}  "
              f"{bucket['bytes']:>8}B  opcode {ops or '-'}")
        unique = len(bucket["uniqueRows"])
        print(f"      行帧分布 {win}  唯一行 {unique}"
              f"/预期 {expect if expect else '-'}")
        if expect and unique > expect * 1.5:
            print(f"      ! 超过预期 {expect}：本段多半混了东西——"
                  f"要么点页签时默认榜单（繁荣/武勋 300 行）也下发了，"
                  f"要么首占榜 12级+9级 两批各 100 行叠加。"
                  f"按帧尺寸分组后再判（9KB≈榜单行帧，11KB≈首占榜行帧）")
        for rec in bucket["records"][:5]:
            print(f"      分数 {rec['score']:<10} 州 {rec['a']:<4} "
                  f"等级 {rec['b']:<4} 同盟 {rec['union']}")

    # OCR 反查：截图上的文字出现在哪些帧
    hits: dict[str, dict] = {}
    if ocr_hints:
        blobs = [(row, "\x00".join(row["texts"])) for row in rows]
        for label, texts in ocr_hints.items():
            found = []
            for text in texts:
                match = None
                if text:
                    for row, blob in blobs:
                        if text in blob:
                            match = {"text": text, "frame": row["index"],
                                     "pcap": row["pcap"],
                                     "opcode": row["opcode"],
                                     "step": row["step"]}
                            break
                found.append(match or {"text": text, "frame": None})
            hits[label] = {
                "texts": texts,
                "matched": sum(1 for f in found if f["frame"] is not None),
                "detail": found,
            }
        print("\nOCR 文本 → pcap 帧 反查：")
        for label, info in hits.items():
            print(f"  {label:<28} 命中 {info['matched']}/{len(info['texts'])}")

    tag = time.strftime("%Y%m%d_%H%M%S")
    frames_path = os.path.join(OUT_DIR, f"rank_frames_{tag}.jsonl")
    with open(frames_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {
        "sources": [{k: v for k, v in s.items() if k != "shots"} for s in sources],
        "decoded": len(rows),
        "opcodes": {str(o): {"count": s["count"], "bytes": s["bytes"],
                             "rows": s["rows"],
                             "steps": dict(s["steps"]),
                             "texts": s["texts"][:40]}
                    for o, s in stat.items()},
        "byStep": {k: {"frames": v["frames"], "bytes": v["bytes"],
                       "opcodes": dict(v["opcodes"]),
                       "texts": v["texts"][:60],
                       "rows": v["rows"][:20],
                       "recordCount": v["recordCount"],
                       "records": v["records"][:40]} for k, v in by_step.items()},
        "ocrHits": hits,
    }
    report_path = os.path.join(OUT_DIR, f"rank_report_{tag}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n帧明细 -> {frames_path}\n汇总 -> {report_path}")
    return report


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def existing_sources() -> list[dict]:
    """把最近一批 pcap（同一次运行的 *_open.pcap 之后）还原成 sources。"""
    if not os.path.isdir(OUT_DIR):
        raise SystemExit("还没有抓过包")
    files = [os.path.join(OUT_DIR, n) for n in os.listdir(OUT_DIR)
             if n.startswith("rank_") and n.endswith(".pcap")]
    opens = [f for f in files if f.endswith("_open.pcap")]
    if not opens:
        raise SystemExit("找不到 *_open.pcap（先跑一次 --capture）")
    batch_start = max(os.path.getmtime(f) for f in opens) - 5
    batch = sorted(f for f in files if os.path.getmtime(f) >= batch_start)
    sources = []
    for path in batch:
        name = os.path.basename(path)
        slug = name[len("rank_"):-len(".pcap")]
        # rank_<时间戳>_<序号>_<榜单>.pcap → 榜单名
        label = slug.split("_", 2)[-1] if slug.count("_") >= 2 else slug
        label = re.sub(r"^\d+_", "", label)      # 去掉批次内序号，如 01_霸业积分榜
        board = label
        shots: list[str] = []
        for jpg in sorted(os.listdir(OUT_DIR)):
            if not (jpg.startswith("step_") and jpg.endswith(".jpg")):
                continue
            if board not in jpg:
                continue
            jpath = os.path.join(OUT_DIR, jpg)
            if os.path.getmtime(jpath) >= batch_start - 900:
                shots.append(jpath)
        sources.append({"label": label, "pcap": path, "shots": shots,
                        "bytes": os.path.getsize(path)})
    return sources


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="排行页抓包探针")
    parser.add_argument("--capture", action="store_true", help="抓包 + 逐榜单点击（默认）")
    parser.add_argument("--no-capture", action="store_true", help="跳过抓包，解析最近一批")
    parser.add_argument("--analyze", default="", help="只解析指定 pcap")
    parser.add_argument("--scroll", type=int, default=1, help="每个榜单下滑次数（触发分页）")
    parser.add_argument("--scope", type=int, default=0, help="额外切一次全服下拉的第 N 项")
    parser.add_argument("--no-ocr", action="store_true", help="不做 OCR 文本反查")
    parser.add_argument("--cold", action="store_true",
                        help="先冷启动游戏（清客户端榜单缓存，抓首次下发）")
    parser.add_argument("--extra", action="store_true",
                        help="补抓个人页签横拖后的 阵营内排行（逐阵营）")
    parser.add_argument("--no-camps", action="store_true",
                        help="阵营内排行只抓当前阵营，不逐个切换")
    parser.add_argument("--firstocc9", action="store_true",
                        help="抓个人首占榜前，右下角下拉切到 9 级")
    parser.add_argument("--deep", type=int, default=0,
                        help="每个榜单最多下拖 N 次（拖到画面不再变化为止，抓 300 行用 40）")
    parser.add_argument("--only", default="",
                        help="只抓指定榜单，逗号分隔（如 个人繁荣榜,个人武勋榜）")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每榜单在窗口内重复点几次（默认1；抓不满 300 时用 2）")
    parser.add_argument("--tag", default="",
                        help="批次号（多榜单分开抓时共用，如 20261126_003448）")
    args = parser.parse_args(argv)

    if args.analyze:
        analyze([{"label": os.path.basename(args.analyze), "pcap": args.analyze,
                  "shots": []}])
        return 0

    sources: list[dict] = []
    if not args.no_capture:
        adb = Adb()
        adb.ensure_online()
        sources = capture(adb, args)
    else:
        sources = existing_sources()

    ocr_hints: dict[str, list[str]] = {}
    if not args.no_ocr:
        for src in sources:
            texts: list[str] = []
            for shot in src.get("shots") or []:
                try:
                    spans = _ocr_spans(Image.open(shot))
                except Exception:      # noqa: BLE001
                    continue
                for span in spans:
                    if len(span.text) >= 2 and span.text not in texts:
                        texts.append(span.text)
            ocr_hints[src["label"]] = texts[:40]

    analyze(sources, ocr_hints)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
