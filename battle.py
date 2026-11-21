"""正面战斗：邮件驱动的「集结 / 攻占 / 行军」+ 红色敌对目标兜底 + 回城。

流程（在 `flow_mobilize` 把部队调动到最近据点之后执行）：
1. **邮件能执行**：重新打开那封邮件 → 点正文绿色坐标 → 确认跳转（相机到战场并选中地块），
   然后在右侧竖排按钮里按动作优先级点 `攻占/占领/攻城/攻击/攻打/行军/集结`，
   点完出现与「调动」同款的出征面板（5 张部队卡 + 金色「出征」）。
2. **邮件意义不明 / 地图上执行不了**（没坐标、没命中关键字、没有对应按钮）：
   先在可视地图里按颜色找高饱和红色块（敌对标签/血条），点它 → 右侧出现
   `攻打/攻击/攻占/行军` 就出征，没有就换下一个红色目标（对撞产生战斗）。
   视野内确实没有红敌时，打开左下「战报」→ 读「防守/交战」记录头部的**绿色坐标**
   → 点它跳转 → 再按 `攻城/攻占/行军` 出征（实测 7级木材(709,767) 出「攻城」）。
3. 出征后若弹出「部队未携带满战法，是否继续出征？」→ 自动点「继续出征」。
4. 派兵复用 `Mobilizer`（含「兵力不满先点白色补兵、补完仍 <30000 就跳过」）。
5. 等战斗结束（按到达时间 + 60s 预判，再轮询据点面板确认不再「行军/出征」），
   重新选中据点，点左下角「回城」回老家（城内视角：君王殿/研究所/亲卫营）。
   即使最终没找到可打目标，也会回据点并回城，保证流程收尾。

实测要点（1600x900）：
- 右侧按钮列在 x≈1075–1215，必须窄条 OCR（`MapNav.find_all_buttons`）。
- 出征面板与「调动」面板同款，可直接复用 `Mobilizer.panel_open/read_statuses/dispatch_one`。
- 回城按钮实测 bbox = (22,712)–(176,884)，点中心 (99,798)。
"""
from __future__ import annotations

import math
import re
import time

from PIL import Image

from . import config, intent, ocrengine, vision
from .adb import Adb
from .mail import MailBody, MailReader
from .mapnav import MapNav
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
RED_BOX = (250, 140, 1400, 560)
# 圆形红色通知角标只有 ~10x12，不是可打的敌对目标
RED_MIN_BADGE = 14

# 左下「回城」按钮（实测：图标中心 ≈ (60,772)，D6 bbox = 22,712–176,884）
# 注意：(99,798) 已经在图标右侧的空白处点不动，故把 (60,772) 放第一位
RETURN_HOME_POINTS = ((60, 772), (70, 790), (50, 752))
# 城内视角特征（回城后应出现）
CITY_VIEW_BOX = (0, 55, 900, 420)
CITY_VIEW_HINTS = ("君王殿", "研究所", "亲卫营", "出城", "城内", "城墙", "民居")

BATTLE_EXTRA_SECONDS = 60       # 到达战场后再留 60s 给战斗/返回
POLL_SECONDS = 20.0

# ---------------------------------------------------------------------------
# 战报兜底（实测：左下 D5 = 战报；「防守」记录头部有绿色可点坐标，如 7级木材(709,767)）
# ---------------------------------------------------------------------------
BATTLE_REPORT_ENTRY = (363, 743)
REPORT_TITLE_BOX = (0, 20, 460, 90)
REPORT_TAB_BOX = (0, 95, 900, 140)
# 每屏 3 条战报，头部行是全分辨率 y≈140 / 412 / 684
REPORT_HEADER_BANDS = ((120, 130, 1500, 205),
                       (392, 402, 1500, 477),
                       (664, 674, 1500, 749))
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
                time.sleep(2.5)
                # 城内视角：能 OCR 到 君王殿/研究所/亲卫营；或世界地图底栏消失
                if self.in_city_view() or not self.nav.on_map():
                    self.log("  ✓ 已回城（离开世界地图 / 城内视角）")
                    return True
            self.log(f"  · 第{i + 1}次点「回城」没生效，重试")
        return self.in_city_view() or not self.nav.on_map()


class BattleFlow:
    """一次完整的「到战场 → 出征 → 打完 → 回据点 → 回城」编排。"""

    def __init__(self, adb: Adb | None = None, log=print, timer=None):
        self.adb = adb or Adb()
        self.nav = MapNav(self.adb, log=log)
        self.reader = MailReader(self.adb, log=log)
        self.cmd = BattleCommander(self.adb, log=log)
        self.log = log
        self.mob: Mobilizer | None = None
        self._reopen = None                 # 面板被收起时的「重新选中目标」回调
        self._enemy_point: tuple[int, int] | None = None
        # 可选的阶段计时器（与 flow_mobilize 共用同一轮）
        self.timer = timer

    def _mark(self, name: str, kind: str = "执行", note: str = "",
              stage: str = STAGE_ATTACK) -> None:
        if self.timer is not None:
            self.timer.mark(name, kind=kind, note=note, stage=stage)

    # -- 目标面板 ---------------------------------------------------------
    def _panel_open(self) -> bool:
        return bool(self.mob and self.mob.panel_open())

    def _reopen_panel(self) -> bool:
        if callable(self._reopen):
            return bool(self._reopen())
        return False

    def _jump_mail(self, mail: MailBody) -> MailBody | None:
        """重新打开邮件 → 点正文坐标 → 确认跳转。"""
        try:
            body = self.reader.open_mail_at(mail)
        except Exception as e:                  # 邮件页异常时不阻塞战斗兜底
            self.log(f"  × 重新打开邮件失败：{e}")
            return None
        if body.coord_point is None:
            self.log("  × 邮件正文没有可点击坐标，无法跳转")
            self.reader.back()
            return None
        coord = body.intent.coord if body.intent else None
        self.log(f"== 邮件坐标跳转 {coord or ''} @{body.coord_point}")
        self.adb.tap(*body.coord_point)
        time.sleep(1.4)
        if self.nav.jump_dialog_visible():
            self.nav.confirm_jump(2.8)
        else:
            self.log("  ! 未检测到跳转弹窗，按已跳转处理")
        return body

    def _click_action(self, names: tuple[str, ...],
                      timeout: float = 8.0) -> str:
        """在地块右侧按钮里点战斗按钮，成功（出现出征面板）返回按钮文字。"""
        name, point, labels = self.cmd.wait_action_button(names, timeout=timeout)
        self.log("  地块按钮: " + (", ".join(labels) or "无"))
        if not name:
            self.log(f"  × 没有可用的战斗按钮（候选 {names}）")
            return ""
        self.log(f"== 点击战斗按钮「{name}」@{point}")
        self.adb.tap(*point)
        time.sleep(1.8)
        if self._panel_open():
            return name
        self.log("  ! 点完战斗按钮没看到出征面板")
        return ""

    def _open_mail_target(self, mail: MailBody, names: tuple[str, ...],
                          result: dict | None = None) -> tuple[bool, str]:
        body = self._jump_mail(mail)
        if body is None:
            return False, ""
        if result is not None:
            result["shots"].append(
                self.nav.shoot(f"b1_战场_{time.strftime('%H%M%S')}"))
        name = self._click_action(names)
        if not name:
            # 邮件动作与地块按钮名不一致（如「集结」邮件落到敌方格只有「攻打」）
            self.log("  · 地块没有邮件动作按钮，改试其它战斗按钮")
            name = self._click_action(DEFAULT_ACTION_BUTTONS, timeout=3.0)
        if not name:
            return False, ""
        all_names = tuple(dict.fromkeys(names + DEFAULT_ACTION_BUTTONS))
        self._reopen = lambda: self._reopen_mail_target(mail, all_names)
        return True, name

    def _reopen_mail_target(self, mail: MailBody,
                            names: tuple[str, ...]) -> bool:
        self.nav.ensure_map()
        if self._jump_mail(mail) is None:
            return False
        if self._click_action(names, timeout=6.0):
            return True
        return bool(self._click_action(DEFAULT_ACTION_BUTTONS, timeout=3.0))

    def _open_enemy_target(self, result: dict | None = None) -> tuple[bool, str]:
        """邮件意义不明时：找红色敌对目标对撞。"""
        self.nav.ensure_map()
        cands = self.cmd.find_red_targets()
        if not cands:
            self.log("  · 严格阈值没找到红色目标，放宽阈值再试")
            cands = self.cmd.find_red_targets(relaxed=True)
        if not cands:
            self.log("  × 可视地图里没找到红色敌对目标")
            return False, ""
        for i, c in enumerate(cands):
            self.nav.ensure_map()
            self.log(f"== 敌对目标#{i + 1} ({c['x']},{c['y']}) "
                     f"红色像素 {c['count']} 距中心 {c['dist']}")
            self.adb.tap(c["x"], c["y"])
            time.sleep(1.8)
            name = self._click_action(DEFAULT_ACTION_BUTTONS, timeout=4.0)
            if not name:
                self.log("  · 该红色目标没有战斗按钮，换下一个")
                continue
            self._enemy_point = (c["x"], c["y"])
            self._reopen = self._reopen_enemy_target
            if result is not None:
                result["enemy_target"] = [c["x"], c["y"]]
            return True, name
        return False, ""

    def _reopen_enemy_target(self) -> bool:
        self.nav.ensure_map()
        if self._enemy_point:
            self.adb.tap(*self._enemy_point)
            time.sleep(1.6)
            if self._click_action(DEFAULT_ACTION_BUTTONS, timeout=5.0):
                return True
        # 原目标消失（已被打掉/走开）→ 重新扫描红色目标
        return self._open_enemy_target()[0]

    def _open_report_target(self, result: dict | None = None) -> tuple[bool, str]:
        """视野内找不到红色敌人时：用战报里最新一条交战的绿色坐标跳转并攻打。"""
        if not self.cmd.open_battle_reports():
            return False, ""
        target = self.cmd.read_report_target()
        if not target:
            self.log("  × 战报里没有可点的绿色坐标")
            self.cmd.close_battle_reports()
            return False, ""
        coord = target["coord"]
        self.log(f"== 战报目标：{target['text']} 坐标 {coord} @{target['point']}")
        self.adb.tap(*target["point"])
        time.sleep(1.4)
        if self.nav.jump_dialog_visible():
            self.nav.confirm_jump(2.8)
        else:
            self.log("  ! 未检测到跳转弹窗，按已跳转处理")
        if result is not None:
            result["report_target"] = list(coord) if coord else None
            result["report_text"] = target["text"]
            result["shots"].append(
                self.nav.shoot(f"b1b_战报_{time.strftime('%H%M%S')}"))
        name = self._click_action(DEFAULT_ACTION_BUTTONS, timeout=6.0)
        if not name:
            if self.cmd.report_visible():
                self.cmd.close_battle_reports()
            return False, ""
        self._reopen = self._reopen_report_target
        return True, name

    def _reopen_report_target(self) -> bool:
        self.nav.ensure_map()
        if not self.cmd.open_battle_reports():
            return False
        target = self.cmd.read_report_target()
        if not target:
            return False
        self.adb.tap(*target["point"])
        time.sleep(1.4)
        if self.nav.jump_dialog_visible():
            self.nav.confirm_jump(2.5)
        return bool(self._click_action(DEFAULT_ACTION_BUTTONS, timeout=5.0))

    # -- 等待/收尾 --------------------------------------------------------
    def _wait_battle_return(self, camp, fac, indexes: tuple[int, ...],
                            mr, timeout: float) -> bool:
        """等部队打完并回到据点（面板里不再有「行军/出征 mm:ss」）。"""
        mob = self.mob
        assert mob is not None
        deadline = time.monotonic() + max(30.0, timeout)
        # 先按「最后到达 + 60s」预判，避免每 20s 去重选据点（很慢）
        if mr.last_arrival:
            now = mob.clock.now()
            a, b = _to_seconds(now), _to_seconds(mr.last_arrival)
            if a is not None and b is not None:
                wait = (b - a) % (24 * 3600) + BATTLE_EXTRA_SECONDS
                wait = min(wait, max(30.0, timeout))
                self.log(f"  预计 {wait:.0f}s 后战斗结束"
                         f"（到达 {mr.last_arrival} + {BATTLE_EXTRA_SECONDS}s）")
                time.sleep(max(0.0, wait))
        if fac is None or camp is None:
            # 没有据点上下文时无法重选据点轮询，按预估时间视为已结束
            self.log("  · 缺少据点上下文，按等待时间视为战斗结束")
            return True
        deadline = max(deadline, time.monotonic() + 60.0)
        fails = 0
        while time.monotonic() < deadline:
            fac.reselect(camp)
            if mob.panel_open() or mob.open_from_camp():
                sts = mob.read_statuses()
                transit = [i for i in indexes
                           if ":" in sts.get(i, "")
                           and any(k in sts[i] for k in ("行军", "出征"))]
                self.log("  战场核对：" + (", ".join(
                    f"部队{k}={v}" for k, v in sorted(sts.items())) or "未识别"))
                if not transit:
                    self.log("  ✓ 部队已结束战斗/回到据点")
                    mob.close()
                    return True
                fails = 0
                self.log(f"  · 仍在途中: 部队{transit}")
            else:
                fails += 1
                self.log(f"  · 打不开据点面板（{fails}/3）")
                if fails >= 3:
                    break
            rest = deadline - time.monotonic()
            time.sleep(max(1.0, min(POLL_SECONDS, rest)))
        self.log("  ! 等待战斗结束超时（按已出征处理）")
        mob.close()
        return False

    # -- 主流程 -----------------------------------------------------------
    def run(self, camp, mail: MailBody | None, fac=None,
            indexes: tuple[int, ...] = (1, 2, 3, 4),
            min_strength: int = 30000, replenish: bool = True,
            wait_return: bool = True, timeout: float = 900.0,
            enemy_fallback: bool = True) -> dict:
        """执行一次正面战斗。camp/fac 用来打完后回据点与回城。"""
        result: dict = {
            "ok": False, "mode": "battle", "shots": [],
            "camp": camp.describe() if camp is not None else "",
            "min_strength": min_strength,
        }
        self.adb.ensure_online()
        self.nav.ensure_map()
        self._mark("战斗准备", "准备")

        self.mob = Mobilizer(self.adb, log=self.log,
                             reselect=lambda: self._reopen_panel(),
                             timer=self.timer)
        mob = self.mob
        opened, action = False, ""

        # 1) 邮件指令：只有「可执行」的邮件才按动作点按钮
        if (mail is not None and mail.intent is not None
                and mail.intent.actionable):
            names = ACTION_BUTTONS.get(mail.intent.action or "",
                                       DEFAULT_ACTION_BUTTONS)
            self.log(f"== 尝试按邮件执行：{mail.intent.describe()}")
            opened, action = self._open_mail_target(mail, names, result)
            if not opened:
                self.log("  ! 邮件指令在地图上执行不了 → 改为红色敌对目标对撞")
        elif mail is not None:
            self.log("  ! 邮件意义不明（无可执行关键字/坐标）→ 红色敌对目标对撞")

        # 2) 兜底 A：当前视野内找红色敌对目标
        if not opened and enemy_fallback:
            opened, action = self._open_enemy_target(result)
        # 3) 兜底 B：视野里没有红敌 → 用「战报」里的交战坐标
        if not opened and enemy_fallback:
            self.log("  · 视野内没有红色目标，改用「战报」交战坐标")
            opened, action = self._open_report_target(result)
        self._mark("寻找战斗目标", "分析",
                   note=(action or ("未找到" if not opened else "")))
        if not opened:
            result["reason"] = "no-battle-target"
            # 即使没打到，也要回据点并回城，保证一次流程收尾
            if fac is not None and camp is not None:
                fac.reselect(camp)
            result["shots"].append(
                self.nav.shoot(f"b3_无目标_{time.strftime('%H%M%S')}"))
            result["home"] = self.cmd.return_home()
            result["shots"].append(
                self.nav.shoot(f"b4_回城_{time.strftime('%H%M%S')}"))
            if not result["home"]:
                result["reason"] = "no-battle-target+home-failed"
            self._mark("无目标收尾", "执行", stage=STAGE_HOME)
            return result
        result["action"] = action

        # 3) 出征（复用 Mobilizer：兵力检查 + 补兵 + 金色出征）
        mr = mob.mobilize(indexes=indexes, skip_busy=True,
                          min_strength=min_strength, replenish=replenish,
                          send_stationed=True)
        mr.camp = result["camp"]
        result.update(mr.as_dict())
        result["shots"].append(
            self.nav.shoot(f"b2_出征_{time.strftime('%H%M%S')}"))
        mob.close()
        self._mark("战斗出征", "执行")

        # 4) 等战斗结束
        if wait_return:
            result["returned"] = self._wait_battle_return(
                camp, fac, indexes, mr, timeout)
            self._mark("等待战斗结束", "等待",
                       note=f"最后到达 {mr.last_arrival or '-'}")

        # 5) 回据点 + 回城
        if fac is not None and camp is not None:
            fac.reselect(camp)
        result["shots"].append(
            self.nav.shoot(f"b3_据点_{time.strftime('%H%M%S')}"))
        result["home"] = self.cmd.return_home()
        result["shots"].append(
            self.nav.shoot(f"b4_回城_{time.strftime('%H%M%S')}"))
        self._mark("回城", "执行", stage=STAGE_HOME)

        dispatched = any(t.dispatched for t in mr.troops)
        result["failed_troops"] = [t.index for t in mr.troops
                                   if not t.dispatched]
        finished = result.get("returned", not wait_return)
        result["ok"] = bool(dispatched and result["home"] and finished)
        if not result["ok"]:
            if not dispatched:
                result["reason"] = "not-dispatched"
            elif wait_return and not finished:
                result["reason"] = "battle-not-finished"
            else:
                result["reason"] = "return-home-failed"
        return result
