"""地图战斗操作（不含邮件）：红色敌对目标 / 动作按钮 / 回城 / 战报坐标。

保留内容（热点驱动链路与只读探针共用）：
1. `BattleCommander`：可视地图按颜色找高饱和红色块（敌对标签/血条），点它 → 右侧
   出现 `攻打/攻击/攻占/行军` 就出征；也可打开左下「战报」读绿色坐标跳转；
2. 回城：重新选中据点 → 左下「回城」→ 处理「确认退出/今日登录不再提示」弹窗；
3. `find_red_targets` / `red_clusters` / `find_green_clusters`：纯图像识别工具，
   供 `tools/red_probe.py` 等只读探针复用。

**邮件链路已从本工程删除**：`mail.py`、`flow_mail.py`、`flow_mobilize.py`、
`flow_loop.py`、`flow_line_report.py` 及其 CLI 入口均已移除；正式长跑只走
`sanmou_auto.atomic_loop`（原子指令 + 战报热点）。

实测要点（1600x900）：
- 右侧按钮列在 x≈1075–1215，必须窄条 OCR（`MapNav.find_all_buttons`）。
- 回城按钮实测 bbox = (22,712)–(176,884)，点中心 (99,798)。
"""
from __future__ import annotations

import math
import re
import time

from PIL import Image

from . import config, fastwait, intent, ocrengine, vision
from .adb import Adb
from .mapnav import MOBILIZE_PANEL_HINT_BOX, MapNav
from .mobilize import Mobilizer, _to_seconds
from .timing import STAGE_ATTACK, STAGE_HOME

# 邮件动作 -> 地块右侧按钮候选（顺序即优先级）
ACTION_BUTTONS = {
    intent.OCCUPY: ("攻占", "占领", "攻城", "攻击", "攻打", "行军", "出征"),
    intent.MARCH: ("集结", "行军", "出征", "攻占"),
    intent.GARRISON: ("驻守", "调动"),
}
# 邮件无法执行时的兜底：任何能「打起来」的按钮
DEFAULT_ACTION_BUTTONS = ("集结", "攻打", "攻击", "攻占", "占领", "攻城",
                          "行军", "出征")
# 这些字样不是战斗按钮（点了会打开详情/整编，浪费一轮）
EXCLUDE_HINTS = ("总览", "整编", "奔袭", "结义")

# 敌对目标标红：高饱和红色像素（红色敌军标签 / 敌军血条）
# 阈值要排除橙色火焰特效（g≈120–160）和紫色指令队列（b 高），
# 否则会点到自家据点中央的红色/紫色特效，而不是敌人。
RED_R = 165
RED_DG = 70
RED_DB = 70
RED_GMAX = 105
RED_BMAX = 105
# 严格阈值找不到时放宽一次（暗红标签也能命中）：误点代价只是一次无效点击
RED_RELAXED = (140, 45, 45, 150, 150)
RED_GRID = 44
RED_MIN_PIXELS = 35
RED_MAX_CANDS = 6
# 红色目标应该是「标签/小图标」大小：过大的连通块是战斗特效/建筑，跳过
# 敌军名字标签可能「宽而扁」（最长约 300），所以宽度上限单独放宽
RED_MAX_BOX = 130
RED_MAX_WIDTH = 320
RED_MIN_DENSITY = 0.045
# 屏幕正中通常是「当前选中的地块 + 红色选中特效」；优先点偏离中心的目标，
# 只有地图上找不到别的红色块时才允许点中心
RED_CENTER_SKIP = 80
# 只扫地图中央区域：排除左侧功能图标列、底部「回城/邮件」栏、右侧部队队列，
# 以及右下角展开菜单（结义/城务/编队/背包…）上的红色通知角标
RED_BOX = config._p((250, 140, 1400, 560), (0, 300, 890, 1290))
# 圆形红色通知角标只有 ~10x12，不是可打的敌对目标
RED_MIN_BADGE = 14

# 左下「回城」按钮（横屏实测图标中心 ≈ (60,772)；竖屏实测 ≈ (62,1355)）
RETURN_HOME_POINTS = config._p(
    ((60, 772), (70, 790), (50, 752)),
    ((62, 1355), (75, 1440), (90, 1400)))
# 城内视角特征（回城后应出现）
CITY_VIEW_BOX = config._p((0, 55, 900, 420), (0, 260, 900, 1000))
CITY_VIEW_HINTS = ("君王殿", "研究所", "亲卫营", "出城", "城内", "城墙", "民居")

BATTLE_EXTRA_SECONDS = 60       # 到达战场后再留 60s 给战斗/返回
POLL_SECONDS = 20.0


def _pause(seconds: float, hook=None) -> None:
    """等待 `seconds` 秒；有 hook 时交给它（可在等待里插入战报收集）。"""
    seconds = float(seconds)
    if seconds <= 0:
        return
    if hook is not None:
        hook(seconds)
    else:
        time.sleep(seconds)

# ---------------------------------------------------------------------------
# 战报兜底（横屏：左下 D5 = 战报；竖屏：主界面左下交叉剑图标 ≈ (378,1317)）
# ---------------------------------------------------------------------------
BATTLE_REPORT_ENTRY = config._p((363, 743), (378, 1317))
REPORT_TITLE_BOX = config._p((0, 20, 460, 90), (0, 0, 300, 60))
REPORT_TAB_BOX = config._p((0, 95, 900, 140), (0, 1415, 900, 1485))
# 横屏每屏 3 条（头部 y≈140/412/684）；竖屏每屏 4 条（头部 y≈101/477/852/1227）
REPORT_HEADER_BANDS = config._p(
    ((120, 130, 1500, 205),
     (392, 402, 1500, 477),
     (664, 674, 1500, 749)),
    ((60, 96, 890, 160),
     (436, 472, 890, 536),
     (811, 847, 890, 911),
     (1186, 1222, 890, 1286)))
REPORT_COORD_RE = re.compile(r"(\d{2,4})\s*[,，.、]\s*(\d{2,4})")
# 战报头部里的坐标文字是绿色（实测文字像素 rgb≈(80,140,110)）
GREEN_G = 118
GREEN_DG = 45
GREEN_DB = 22
GREEN_GRID = 24
GREEN_MIN_PIXELS = 6
GREEN_MAX_CANDS = 3


def red_clusters(img: Image.Image,
                 box: tuple[int, int, int, int] = RED_BOX,
                 step: int = 2,
                 relaxed: bool = False) -> list[dict]:
    """把红色像素按相邻网格聚成簇，返回原始统计（不套过滤条件，供标定用）。"""
    rmin, dg, db, gmax, bmax = RED_RELAXED if relaxed else (
        RED_R, RED_DG, RED_DB, RED_GMAX, RED_BMAX)
    k = img.width / config.SCREEN[0]
    x1 = max(0, round(box[0] * k))
    y1 = max(0, round(box[1] * k))
    x2 = min(img.width, round(box[2] * k))
    y2 = min(img.height, round(box[3] * k))
    if x2 - x1 < 10 or y2 - y1 < 10:
        return []
    px = img.load()
    cells: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for y in range(y1, y2, step):
        for x in range(x1, x2, step):
            r, g, b = px[x, y]
            if (r >= rmin and r - g >= dg and r - b >= db
                    and g <= gmax and b <= bmax):
                cells.setdefault((x // RED_GRID, y // RED_GRID), []).append((x, y))

    seen: set[tuple[int, int]] = set()
    clusters: list[list[tuple[int, int]]] = []
    neighbors = ((1, 0), (-1, 0), (0, 1), (0, -1),
                 (1, 1), (-1, -1), (1, -1), (-1, 1))
    for key in list(cells):
        if key in seen:
            continue
        stack = [key]
        seen.add(key)
        pts: list[tuple[int, int]] = []
        while stack:
            c = stack.pop()
            pts += cells[c]
            for nx, ny in neighbors:
                nb = (c[0] + nx, c[1] + ny)
                if nb in cells and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        clusters.append(pts)

    cx0 = (x1 + x2) / 2
    cy0 = (y1 + y2) / 2
    out: list[dict] = []
    for pts in clusters:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x = (min(xs) + max(xs)) // 2
        y = (min(ys) + max(ys)) // 2
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        dist = math.dist((x, y), (cx0, cy0)) / k
        out.append({
            "x": round(x / k), "y": round(y / k), "count": len(pts),
            "w": round(w / k), "h": round(h / k),
            "density": round(len(pts) / max(1, (w + 2) * (h + 2)), 3),
            "dist": round(dist, 1),
            "bbox": [round(min(xs) / k), round(min(ys) / k),
                     round(max(xs) / k), round(max(ys) / k)],
        })
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


def find_red_targets(img: Image.Image,
                     box: tuple[int, int, int, int] = RED_BOX,
                     step: int = 2,
                     min_pixels: int = RED_MIN_PIXELS,
                     max_cands: int = RED_MAX_CANDS,
                     relaxed: bool = False) -> list[dict]:
    """在可视地图区域里找红色敌对目标，返回候选点（含红色像素数/评分）。

    只扫 `RED_BOX`（去掉顶栏/底栏/左右 UI），红色判定：
    r≥165 且 r-g≥70 且 r-b≥70 且 g,b≤105 —— 橙色火焰/紫色「指令队列」会被排除。
    相邻网格合并成一个目标，按「红色像素多 + 离屏幕中心近」排序。
    """
    cands: list[dict] = []
    min_pixels = 25 if relaxed else min_pixels
    min_density = 0.03 if relaxed else RED_MIN_DENSITY
    for c in red_clusters(img, box=box, step=step, relaxed=relaxed):
        n = c["count"]
        if n < min_pixels:
            continue
        w, h = c["w"], c["h"]          # red_clusters 已换算成全分辨率
        # 大块红色 = 火焰/特效/建筑高亮，不是可点的敌对标签
        if h > RED_MAX_BOX or w > RED_MAX_WIDTH:
            continue
        # 又宽又高的方块状红块同样是特效；标签是「宽而扁」、小图标是「窄而小」
        if w > 90 and h > 45:
            continue
        # UI 上的红点角标（编队/背包等的未读提示）不是敌人
        if w < RED_MIN_BADGE and h < RED_MIN_BADGE:
            continue
        if c["density"] < min_density:
            continue
        cands.append({
            "x": c["x"], "y": c["y"], "count": n,
            "dist": c["dist"],
            "score": round(n - c["dist"] * 0.6, 1),
            "bbox": c["bbox"],
        })
    cands.sort(key=lambda c: c["score"], reverse=True)
    # 优先排除「屏幕正中当前选中地块的红色特效」
    far = [c for c in cands if c["dist"] > RED_CENTER_SKIP]
    return (far or cands)[:max_cands]


def find_green_clusters(img: Image.Image,
                        box: tuple[int, int, int, int],
                        step: int = 2,
                        min_pixels: int = GREEN_MIN_PIXELS,
                        max_cands: int = GREEN_MAX_CANDS) -> list[dict]:
    """找区域内的绿色文字簇（战报头部的可点坐标）。"""
    k = img.width / config.SCREEN[0]
    x1 = max(0, round(box[0] * k))
    y1 = max(0, round(box[1] * k))
    x2 = min(img.width, round(box[2] * k))
    y2 = min(img.height, round(box[3] * k))
    if x2 - x1 < 10 or y2 - y1 < 6:
        return []
    px = img.load()
    cells: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for y in range(y1, y2, step):
        for x in range(x1, x2, step):
            r, g, b = px[x, y]
            if g >= GREEN_G and g - r >= GREEN_DG and g - b >= GREEN_DB:
                cells.setdefault((x // GREEN_GRID, y // GREEN_GRID), []).append((x, y))
    seen: set[tuple[int, int]] = set()
    out: list[dict] = []
    for key in list(cells):
        if key in seen:
            continue
        stack = [key]
        seen.add(key)
        pts: list[tuple[int, int]] = []
        while stack:
            c = stack.pop()
            pts += cells[c]
            for nx, ny in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (c[0] + nx, c[1] + ny)
                if nb in cells and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if len(pts) < min_pixels:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w = (max(xs) - min(xs)) / k
        h = (max(ys) - min(ys)) / k
        if w < 12 or h < 4:
            continue
        out.append({
            "x": round((min(xs) + max(xs)) / 2 / k),
            "y": round((min(ys) + max(ys)) / 2 / k),
            "count": len(pts),
            "bbox": [round(min(xs) / k), round(min(ys) / k),
                     round(max(xs) / k), round(max(ys) / k)],
        })
    out.sort(key=lambda c: c["count"], reverse=True)
    return out[:max_cands]


class BattleCommander:
    """地图层面的战斗操作：找红色目标、点右侧战斗按钮、回城。"""

    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.nav = MapNav(self.adb, log=log)
        self.log = log

    def find_red_targets(self, img: Image.Image | None = None,
                         **kw) -> list[dict]:
        img = img or self.adb.screencap()
        return find_red_targets(img, **kw)

    @staticmethod
    def _match_button(text: str, names: tuple[str, ...]) -> bool:
        if not any(n in text for n in names):
            return False
        return not any(bad in text for bad in EXCLUDE_HINTS)

    def find_action_button(self, names: tuple[str, ...], attempts: int = 2):
        """返回 (按钮文字, 点击点, 当前按钮列表)；没有返回 (None, None, 列表)。"""
        btns = self.nav.find_all_buttons(attempts=attempts)
        labels = [t for t, _ in btns]
        for t, p in btns:
            if self._match_button(t, names):
                return t, p, labels
        return None, None, labels

    def wait_action_button(self, names: tuple[str, ...], timeout: float = 6.0):
        deadline = time.monotonic() + timeout
        labels: list[str] = []
        while True:
            t, p, labels = self.find_action_button(names)
            if t:
                return t, p, labels
            if time.monotonic() >= deadline:
                return None, None, labels
            time.sleep(0.8)

    def in_city_view(self, img: Image.Image | None = None) -> bool:
        """回城后是城内视角：君王殿 / 研究所 / 亲卫营 …"""
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, CITY_VIEW_BOX), 2.0)
        return any(h in text for h in CITY_VIEW_HINTS)

    # -- 战报兜底 ---------------------------------------------------------
    def report_visible(self, img: Image.Image | None = None) -> bool:
        img = img or self.adb.screencap()
        title = ocrengine.ocr_text(vision.crop(img, REPORT_TITLE_BOX), 2.0)
        tabs = ocrengine.ocr_text(vision.crop(img, REPORT_TAB_BOX), 2.0)
        return "战报" in title or ("全部" in tabs and "交战" in tabs)

    def open_battle_reports(self, wait: float = 2.5, tries: int = 2) -> bool:
        """打开左下 D5「战报」（幂等）。"""
        if self.report_visible():
            return True
        self.nav.ensure_map()
        for i in range(max(1, tries)):
            self.log(f"== 打开战报（{i + 1}/{tries}）@{BATTLE_REPORT_ENTRY}")
            self.adb.tap(*BATTLE_REPORT_ENTRY)
            time.sleep(wait)
            if self.report_visible():
                return True
        self.log("  ! 战报页没打开")
        return False

    def close_battle_reports(self) -> None:
        if self.report_visible():
            self.adb.tap(100, 25)
            time.sleep(1.2)

    def read_report_coord_only(self):
        """只读最新战报坐标（打开战报 → 读绿色坐标 → 返回地图）。"""
        if not self.open_battle_reports():
            return None
        target = self.read_report_target()
        self.close_battle_reports()
        self.nav.ensure_map()
        return target["coord"] if target else None

    @staticmethod
    def parse_report_coord(text: str):
        """从战报头部解析坐标（兼容 OCR 丢分隔符：709,767 → 709767）。"""
        flat = text.replace(" ", "")
        m = REPORT_COORD_RE.search(flat)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"[（(]\s*(\d{6})\s*[)）]", flat)
        if m:
            d = m.group(1)
            return int(d[:3]), int(d[3:])
        m = re.search(r"[（(]\s*(\d{8})\s*[)）]", flat)
        if m:
            d = m.group(1)
            return int(d[:4]), int(d[4:])
        m = re.search(r"(?<!\d)(\d{3})(\d{3})(?!\d)", flat)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None

    def read_report_target(self, img: Image.Image | None = None) -> dict | None:
        """读最新一条战报头部里的绿色可点坐标。

        返回 {"point","coord","text","band"}；没有战报/没有绿色坐标返回 None。
        """
        img = img or self.adb.screencap()
        for band in REPORT_HEADER_BANDS:
            greens = find_green_clusters(img, band)
            if not greens:
                continue
            text = ocrengine.ocr_text(vision.crop(img, band), 2.0)
            flat = text.replace(" ", "")
            return {"point": (greens[0]["x"], greens[0]["y"]),
                    "coord": self.parse_report_coord(flat),
                    "text": flat.strip(), "band": list(band)}
        return None

    def return_home(self, tries: int = 3) -> bool:
        """点左下角「回城」回老家（城内视角）。"""
        if self.in_city_view():                 # 已经在城里，不要再点「出城」
            self.log("  · 已在城内视角，无需回城")
            return True
        for i in range(max(1, tries)):
            self.nav.ensure_map()
            for point in RETURN_HOME_POINTS:
                self.log(f"== 点击回城按钮 @{point}")
                self.adb.tap(*point)
                # 城内视角 ~0.8~1.8s 加载：轮询命中即返回，替代固定 2.5s
                if fastwait.wait_until(
                        self.adb,
                        lambda img: self.in_city_view(img),
                        timeout=3.0, interval=0.3, min_wait=0.5) \
                        or not self.nav.on_map():
                    self.log("  ✓ 已回城（离开世界地图 / 城内视角）")
                    return True
            self.log(f"  · 第{i + 1}次点「回城」没生效，重试")
        return self.in_city_view() or not self.nav.on_map()
