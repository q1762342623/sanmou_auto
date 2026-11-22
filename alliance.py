"""同盟据点：找离目标最近、且能「调动」的盟归属据点并点选。

能作为调动目标的据点（实测）：
- **同盟大营**：同盟 → 设施 → 左栏「同盟建筑」→ 表格（建筑名/类型/位置/建造者/状态）
- **城 / 水寨 / 军港 / 民港**：同盟 → 城池（底部页签）→ 城池列表（城池/霸业积分/辎重/
  封邑(郡名 + 绿色坐标)/入驻/城主），坐标可点，跳转后右侧同样有「行军/调动/…」

实测（1600x900）：
- 底部「同盟」入口 (905,840)；同盟页底部页签：设施 / 科技 / 成员 / 辎重 / 城池（x≈1523,y≈844）
- 设施页左栏筛选：军屯 / 器械 / 同盟建筑（同盟大营 x/y）/ 建筑（堡垒）
- 点了绿色坐标会弹「是否跳转到坐标」，确定后回地图并**直接选中该据点**
- 城池列表里「入驻」列写「无法入驻 / 枢要之地」的行，跳转后**没有** 行军/调动 按钮，
  必须跳过（本模块在跳转后会校验右侧按钮列里有没有「调动」，没有就换下一个最近的）
- 城池列表会记住滚动位置，扫描/点击前先回顶

坐标含义：全部是 1600x900 横屏 Physical 坐标（adb 触摸坐标系）。
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from . import config, ocrengine, vision
from .adb import Adb
from .mapnav import COORD_RE, MapNav

# ---------------------------------------------------------------------------
# UI 坐标（全分辨率）
# ---------------------------------------------------------------------------
# 竖屏值为布局估算（页签行 y≈1455、返回在底部 115,1548），首次实测后校准
ALLIANCE_ENTRY = config._p((905, 840), (360, 1528))
FACILITY_TAB = config._p((795, 838), (200, 1455))
UNION_BUILD_FILTER = config._p((96, 536), (80, 700))
FACILITY_BACK = config._p((100, 25), (115, 1548))
CITY_TAB = config._p((1523, 844), (560, 1455))

# 同盟页底部页签行（OCR 定位「设施 / 城池」比写死 x 稳）
TAB_ROW_BOX = config._p((600, 750, 1600, 880), (0, 1400, 900, 1560))
# 设施/城池页左栏筛选
LEFT_PANEL_BOX = config._p((0, 80, 380, 880), (0, 60, 300, 1400))

# 设施表格区域（全分辨率）
LIST_BOX = config._p((455, 95, 1600, 660), (0, 60, 900, 1300))
# 表格 OCR 切块参数（400x220 + 110 重叠，实测坐标可整段识别）
LIST_TILE = (400, 220)
LIST_OVERLAP = 110
# 设施表格内向上翻页
LIST_SCROLL = config._p((900, 620, 900, 220), (450, 1250, 450, 800))

# 城池列表：只 OCR「封邑(郡名+坐标)」和「入驻」两列，速度快一倍以上
CITY_TEXT_BOX = config._p((700, 130, 1430, 830), (0, 100, 900, 1300))
CITY_TILE = (620, 200)
CITY_OVERLAP = 60
CITY_SCROLL = config._p((900, 720, 900, 300), (450, 1350, 450, 800))      # 列表内向上翻页
CITY_SCROLL_DOWN = config._p((900, 300, 900, 720), (450, 800, 450, 1350))  # 回顶
CITY_MAX_PAGES = 14
# 跳转后没有「调动」按钮的行（枢要之地等）
NO_MOBILIZE_HINTS = ("无法入驻",)

# 坐标识别：城池列表里 OCR 偶尔丢括号/用点号分隔，这里放宽
CITY_COORD_RE = re.compile(r"[（(]?\s*(\d{2,4})\s*[,，.、]\s*(\d{2,4})\s*[)）]?")

# 判定「跳转后可用」的按钮文字
MOBILIZE_BUTTON = "调动"

# 跳转选中后，左上面板的「名称 Lv.x 类型」区域（读出来做汇报）
SELECTED_TITLE_BOX = (140, 250, 780, 340)
SELECTED_KINDS = ("水寨", "军港", "民港", "郡府", "县城", "关隘", "营寨",
                  "堡垒", "同盟大营", "攻城大营", "王城", "都城", "州府")
# 跳转后地图会把目标移到屏幕中部，标签「郡-县-县(x,y)」里的坐标 = 真实坐标，
# 用它校正列表 OCR 可能读错的坐标（错坐标会让「最近」选偏）
SELECTED_MAP_BOX = (300, 260, 1300, 720)
SELECTED_MAP_CENTER = (800, 430)


@dataclass
class Camp:
    name: str
    coord: tuple[int, int]
    point: tuple[int, int]      # 坐标文字的屏幕坐标（点它即跳转）
    dist_text: str = ""
    status: str = ""
    source: str = "union-build"     # union-build（同盟建筑）/ city（城池列表）
    kind: str = ""                  # 同盟大营 / 堡垒 / 水寨 / 民港 / 城池 …
    note: str = ""                  # 入驻状态等附加信息
    page: int = 0                   # 在列表里的第几屏（扫描时记录，点击时直接滚过去）

    def dist(self, target: tuple[int, int]) -> float:
        return math.dist(self.coord, target)

    @property
    def kind_text(self) -> str:
        if self.kind:
            return self.kind
        return "同盟建筑" if self.source == "union-build" else "城池"

    def describe(self) -> str:
        label = self.kind_text
        if self.name and self.name not in label:
            label = f"{label} {self.name}"
        extra = " ".join(x for x in (self.dist_text, self.status, self.note) if x)
        return f"{label}({self.coord[0]},{self.coord[1]}) {extra}".strip()


class AllianceFacilities:
    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.nav = MapNav(self.adb, log=log)
        self.log = log
        # 页面状态：map / alliance（同盟主页面）/ facility（设施）/ cities（城池）
        self._page = "map"

    # -- 通用 UI -----------------------------------------------------------
    def _find_in(self, box: tuple[int, int, int, int], name: str,
                 scale: float = 3.0):
        """在 box 里 OCR 找包含 name 的文字，返回其中心点。"""
        img = self.adb.screencap()
        for s in ocrengine.ocr(vision.crop(img, box), scale):
            if name in s.text.replace(" ", ""):
                return (int(box[0] + s.cx), int(box[1] + s.cy))
        return None

    def _tabs_text(self) -> list[tuple[str, tuple[int, int]]]:
        """一次 OCR 拿到页签行所有文字（省掉「城池」「设施」各 OCR 一遍）。"""
        img = self.adb.screencap()
        out = []
        for s in ocrengine.ocr(vision.crop(img, TAB_ROW_BOX), 3.0):
            t = s.text.replace(" ", "").strip()
            if t:
                out.append((t, (int(TAB_ROW_BOX[0] + s.cx),
                                int(TAB_ROW_BOX[1] + s.cy))))
        return out

    def _find_tab(self, name: str):
        for t, p in self._tabs_text():
            if name in t:
                return p
        return None

    def _open_tab(self, name: str, fallback: tuple[int, int],
                  wait: float = 2.2) -> None:
        point = self._find_tab(name) or fallback
        self.adb.tap(*point)
        time.sleep(wait)

    # -- 打开 / 返回 -------------------------------------------------------
    def open_alliance_home(self, wait: float = 3.0, tries: int = 2) -> bool:
        """回到「同盟」主页面（底部有 设施/科技/成员/辎重/城池 页签）。

        全屏子页（设施/城池）必须先点左上返回，直接点底部会点进表格里。
        """
        if self._page == "alliance":
            return True                      # 状态已知，免一次 OCR
        for _ in range(max(1, tries)):
            if self.nav.dismiss_modal():        # 「是否继续出征」弹窗会挡住底部入口
                continue
            text = "".join(t for t, _ in self._tabs_text())
            if "城池" in text and "设施" in text:
                self._page = "alliance"
                return True
            if self._page in ("facility", "cities"):
                self.adb.tap(*FACILITY_BACK)
                time.sleep(1.0)
                self._page = "alliance"
                continue
            if self.nav.chat_panel_visible():
                self.nav.close_chat_panel()
                continue
            self.adb.tap(*ALLIANCE_ENTRY)
            time.sleep(wait)
            self._page = "alliance"
        ok = "城池" in "".join(t for t, _ in self._tabs_text())
        if not ok:
            # 可能卡在别的面板（部队详情/聊天）：强制回地图后再开一次
            self.log("  ! 没能确认「同盟」主页面，先强回地图再试")
            self.nav.ensure_map()
            self.adb.tap(*ALLIANCE_ENTRY)
            time.sleep(wait)
            ok = "城池" in "".join(t for t, _ in self._tabs_text())
        self._page = "alliance" if ok else "map"
        if not ok:
            self.log("  ! 仍然打不开「同盟」页面")
        return ok

    def open_union_buildings(self, wait: float = 3.0) -> bool:
        """同盟 → 设施 → 左栏「同盟建筑」。找不到该筛选时返回 False。"""
        if not self.open_alliance_home():
            return False
        self._open_tab("设施", FACILITY_TAB, wait=2.2)
        self._page = "facility"
        point = self._find_in(LEFT_PANEL_BOX, "同盟建筑", scale=2.5)
        if not point:
            self.log("  ! 设施页没有「同盟建筑」筛选（该盟当前可能没有大营）")
            return False
        self.adb.tap(*point)
        time.sleep(1.8)
        return True

    def open_cities_page(self, wait: float = 2.5) -> bool:
        """同盟 → 城池（城 / 水寨 / 军港 / 民港 的完整列表）。"""
        if not self.open_alliance_home():
            return False
        self._open_tab("城池", CITY_TAB, wait=wait)
        self._page = "cities"
        return True

    def back_to_map(self, tries: int = 3) -> None:
        """一路返回到地图：城池/设施 → 同盟主页 → 地图（跳转确认后已在地图则不动）。"""
        for _ in range(max(1, tries)):
            if self._page == "map":
                return
            self.adb.tap(*FACILITY_BACK)
            time.sleep(1.2)
            self._page = ("alliance" if self._page in ("facility", "cities")
                          else "map")
        if self._page != "map":
            self.adb.tap(*FACILITY_BACK)
            time.sleep(1.2)
            self._page = "map"

    # -- 解析：同盟建筑 ---------------------------------------------------
    def parse(self, img=None) -> list[Camp]:
        img = img or self.adb.screencap()
        spans = ocrengine.ocr_tiled(img, LIST_BOX,
                                    LIST_TILE, LIST_OVERLAP, 2.0)
        text = [(s.text.replace(" ", ""), s) for s in spans]

        camps: list[Camp] = []
        seen: set[tuple[tuple[int, int], int]] = set()
        for t, s in text:
            m = COORD_RE.search(t)
            if not m:
                continue
            coord = (int(m.group(1)), int(m.group(2)))
            key = (coord, round(s.cy / 50))
            if key in seen:                 # 切块重叠会重复识别同一行
                continue
            seen.add(key)

            name = ""
            kind = ""
            dist_text = ""
            status = ""
            for t2, s2 in text:
                if s2 is s or abs(s2.cy - s.cy) > 45:
                    continue
                if s2.cx < 700 and not name and not COORD_RE.search(t2):
                    name = t2
                elif 700 <= s2.cx < 900 and not kind and not COORD_RE.search(t2):
                    kind = t2
                elif not dist_text and ("里" in t2 or "格" in t2):
                    dist_text = t2
                elif not status and ("空闲" in t2 or "升级" in t2):
                    status = t2
            camps.append(Camp(name=name or "同盟建筑", coord=coord,
                              point=(int(s.cx), int(s.cy)),
                              dist_text=dist_text, status=status,
                              source="union-build", kind=kind))
        return camps

    # -- 解析：城池（城/水寨/军港/民港） ----------------------------------
    def parse_cities(self, img=None) -> list[Camp]:
        img = img or self.adb.screencap()
        spans = ocrengine.ocr_tiled(img, CITY_TEXT_BOX,
                                    CITY_TILE, CITY_OVERLAP, 2.0)
        items = [(s.text.replace(" ", ""), s) for s in spans]

        out: list[Camp] = []
        seen: set[tuple[int, int]] = set()
        for t, s in items:
            m = CITY_COORD_RE.search(t)
            if not m:
                continue
            coord = (int(m.group(1)), int(m.group(2)))
            if coord in seen:               # 切块重叠/同一行重复
                continue
            seen.add(coord)

            note = ""
            for t2, s2 in items:
                if s2.cx < 1100 or abs(s2.cy - s.cy) > 45:
                    continue
                if t2 in note:                  # 切块重叠会读到两遍
                    continue
                if any(k in t2 for k in ("无法入驻", "枢要之地", "需要审批",
                                         "/220", "/150", "/800", "入驻")):
                    note = f"{note} {t2}".strip()
            out.append(Camp(name=f"城{coord[0]},{coord[1]}", coord=coord,
                            point=(int(s.cx), int(s.cy)),
                            source="city", note=note))
        return out

    # -- 扫描 -------------------------------------------------------------
    def scroll_cities_to_top(self, times: int = 5, wait: float = 0.5) -> None:
        for _ in range(times):
            self.adb.swipe(*CITY_SCROLL_DOWN, 340)
            time.sleep(wait)

    def scan_cities(self, scrolls: int = CITY_MAX_PAGES) -> list[Camp]:
        """把城池列表从上滚到底，收集全部据点（去重）。"""
        self.scroll_cities_to_top()
        found: dict[tuple[int, int], Camp] = {}
        last = None
        for i in range(scrolls + 1):
            rows = self.parse_cities()
            if not rows:
                break
            key = (rows[0].coord, rows[-1].coord)
            if key == last:                 # 已经到底（滚动没生效）
                break
            last = key
            for c in rows:
                c.page = i
                found.setdefault(c.coord, c)
            if i < scrolls:
                self.adb.swipe(*CITY_SCROLL, 450)
                time.sleep(1.0)
        self.log(f"  城池列表共解析到 {len(found)} 个据点")
        return list(found.values())

    def scan_all(self, scrolls: int = CITY_MAX_PAGES) -> list[Camp]:
        """同盟建筑（大营/堡垒）+ 城池列表（城/水寨/军港/民港）一起扫。"""
        camps: list[Camp] = []
        if self.open_union_buildings():
            rows = self.parse()
            self.log(f"  同盟建筑共解析到 {len(rows)} 个据点")
            camps += rows
        self.open_cities_page()
        camps += self.scan_cities(scrolls)
        return camps

    # -- 选择 -------------------------------------------------------------
    def nearest(self, camps: list[Camp], target: tuple[int, int]) -> Camp | None:
        return min(camps, key=lambda c: c.dist(target)) if camps else None

    def find_union_row(self, coord: tuple[int, int],
                       pages: int = 3) -> Camp | None:
        for i in range(pages):
            for c in self.parse():
                if c.coord == coord:
                    return c
            self.adb.swipe(*LIST_SCROLL, 450)
            time.sleep(0.9)
        return None

    def read_city_name(self, point: tuple[int, int]) -> str:
        """在城池列表左侧「城池」列，按行读一下这一行的名字（列表页不在热路径上）。

        注意：不能把 y 上限写死成横屏的 830 —— 竖屏城池行 y≈1300+ 时
        会算出 y1>y2，PIL 直接抛 `Coordinate 'lower' is less than 'upper'`。
        """
        img = self.adb.screencap()
        k = img.width / float(config.SCREEN[0])
        y1 = max(0, point[1] - 62)
        y2 = min(img.height, point[1] + 8)
        if y2 - y1 < 20:                 # 坐标越界时兜底成固定小条，避免反向框
            y1 = max(0, min(img.height - 90, point[1] - 70))
            y2 = min(img.height, y1 + 90)
        box = (0, y1, min(img.width, int(320 * k)), y2)
        best = ""
        for s in ocrengine.ocr(vision.crop(img, box), 2.5):
            t = s.text.replace(" ", "")
            if t and not CITY_COORD_RE.search(t) and len(t) > len(best):
                best = t
        return best

    def _fill_city_name(self, c: Camp) -> Camp:
        name = self.read_city_name(c.point)
        if name:
            c.name = name
            if name in SELECTED_KINDS:
                c.kind = name
        return c

    def find_city_row(self, camp: Camp,
                      pages: int = CITY_MAX_PAGES,
                      full_scan: bool = False) -> Camp | None:
        """用扫描时记录的屏号先滚过去，再前后几屏里找。

        `full_scan=False`（默认）时不回顶全表重扫：屏号来自本轮/本缓存的扫描，
        ±几屏找不到基本就是该据点已经不在了。全表重扫 14 屏（每屏列表 OCR
        5~10s + 滑动）单次就要 2~5 分钟，实测正是「定位最近据点 378s」的主因；
        交给上层换下一个候选只要几十秒。只有最后一个候选才传 full_scan=True。
        """
        self.scroll_cities_to_top()
        start = max(0, camp.page - 2)
        for _ in range(start):
            self.adb.swipe(*CITY_SCROLL, 450)
            time.sleep(0.6)
        for _ in range(start + 7):
            for c in self.parse_cities():
                if c.coord == camp.coord:
                    return self._fill_city_name(c)
            self.adb.swipe(*CITY_SCROLL, 450)
            time.sleep(0.9)
        if not full_scan or start == 0:
            return None
        # 屏号估计不准时回顶完整扫一遍（慢，只在最后一个候选时走）
        self.scroll_cities_to_top()
        for _ in range(pages + 1):
            for c in self.parse_cities():
                if c.coord == camp.coord:
                    return self._fill_city_name(c)
            self.adb.swipe(*CITY_SCROLL, 450)
            time.sleep(0.9)
        return None

    def has_mobilize_button(self, attempts: int = 2) -> bool:
        """跳转选中后，右侧竖排按钮列里是否有「调动」。

        只认「调动 + 至少一个据点按钮（行军/驻守/总览/征兵/委任/整编）」同时出现：
        地图上的行军部队标签也会带「调动」字样，单看一个「调动」会误判。
        实测聊天气泡会遮住上面几个按钮，所以不再强制要求「行军/驻守」都在。
        """
        btns = [t for t, _ in self.nav.find_all_buttons(attempts=attempts)]
        joined = "".join(btns)
        if MOBILIZE_BUTTON not in joined:
            return False
        return any(k in joined for k in ("行军", "驻守", "总览", "征兵", "委任",
                                         "整编", "生产辎重", "结义调动"))

    def read_selected_identity(self) -> tuple[str, str]:
        """跳转选中后，从左上面板读「名称 + 类型」，例如 (藏舟寨, 水寨)。"""
        img = self.adb.screencap()
        texts = [s.text.replace(" ", "")
                 for s in ocrengine.ocr(vision.crop(img, SELECTED_TITLE_BOX), 2.5)]
        name = ""
        kind = ""
        for t in texts:
            if "Lv" in t or "lv" in t:
                name = re.split(r"[Ll]v\.?\s*\d+", t)[0].strip()
            elif t in SELECTED_KINDS and not kind:
                kind = t
        if not kind and name in SELECTED_KINDS:
            kind = name          # 民港/军港 这类面板标题里没有单独的类型词
        return name, kind

    def read_selected_coord(self) -> tuple[int, int] | None:
        """跳转选中后，从地图标签里读真实坐标（离屏幕中心最近的那个）。"""
        img = self.adb.screencap()
        best = None
        best_d = float("inf")
        for s in ocrengine.ocr_tiled(img, SELECTED_MAP_BOX, (500, 300), 60, 2.0):
            for m in CITY_COORD_RE.finditer(s.text.replace(" ", "")):
                coord = (int(m.group(1)), int(m.group(2)))
                d = math.dist((s.cx, s.cy), SELECTED_MAP_CENTER)
                if d < best_d:
                    best, best_d = coord, d
        return best

    def _row_on_screen(self, camp: Camp) -> Camp | None:
        rows = self.parse_cities() if camp.source == "city" else self.parse()
        return next((c for c in rows if c.coord == camp.coord), None)

    def _tap_row_coord(self, camp: Camp, tries: int = 3) -> bool:
        """点坐标并等跳转框；没弹框说明列表滑动/页面动画导致点歪，重新定位再点。

        实测设施页刚滑入时点坐标常常不弹框，因此每次点击带 ±6px 偏移重试，
        并且每次点完都回读一次列表（坐标文字位置可能变）。
        """
        for i in range(max(1, tries)):
            cur = self._row_on_screen(camp)
            if not cur:
                return False
            for dx in (0, 6, -6):
                point = (cur.point[0] + dx, cur.point[1])
                self.log(f"  · 点击坐标 {point}")
                self.adb.tap(*point)
                time.sleep(1.3)
                if jump_dialog_visible(self.adb):
                    return True
            self.log("  · 没弹出跳转框（页面还在动/点歪），重新定位再点")
            time.sleep(0.8)
        return False

    def reselect(self, camp: Camp) -> bool:
        """跳到该据点并重新选中（「调动」面板被收起后再次打开用）。"""
        self.nav.ensure_map()                # 先清掉可能误触出来的面板
        if not self.select(camp, confirm_wait=2.2):
            return False
        if self.has_mobilize_button():
            self.log(f"  ✓ 已重新选中 {camp.describe()}")
            return True
        return False

    def select(self, camp: Camp, confirm_wait: float = 2.2,
               full_scan: bool = False) -> bool:
        """打开对应列表 → 找到该坐标 → 点击 → 确认跳转 → 校验右侧有「调动」。

        返回 True 即表示「跳转后确实看到了调动按钮」——调用方不必再校验一次
        （`has_mobilize_button()` 是一次整屏按钮 OCR，重复调用纯属浪费）。
        """
        if camp.source == "city":
            self.open_cities_page()
            hit = self.find_city_row(camp, full_scan=full_scan)
        else:
            if not self.open_union_buildings():
                return False
            hit = self.find_union_row(camp.coord)
        time.sleep(0.8)                 # 等列表滑入动画结束，否则点坐标不弹跳转框
        if not hit:
            self.log(f"  ! 列表里找不到 {camp.coord}，跳过")
            return False

        self.log(f"== 点击 {camp.describe()} 的坐标 @{hit.point}")
        for i in range(2):
            if not self._tap_row_coord(camp):
                self.log("  ! 没弹出「是否跳转到坐标」，可能点歪了")
                continue
            self.adb.tap(*config.CONFIRM_JUMP)
            time.sleep(confirm_wait)
            self._page = "map"              # 跳转确认后会回到地图
            time.sleep(0.3)
            if self.has_mobilize_button():
                return True
            self.log(f"  ! 跳转后没看到「行军/调动」按钮，重试一次（{i + 1}/2）")
            if camp.source == "city":
                self.open_cities_page()
                hit = self.find_city_row(camp, full_scan=full_scan)
            else:
                if not self.open_union_buildings():
                    return False
                hit = self.find_union_row(camp.coord)
            if not hit:
                return False
        return False

    # -- 主流程 -----------------------------------------------------------
    def goto_nearest(self, target: tuple[int, int],
                     confirm_wait: float = 3.0, tries: int = 4,
                     camps: list[Camp] | None = None) -> Camp | None:
        """在所有「能调动」的同盟据点里找离 target 最近的一个并选中。

        候选 = 同盟建筑（同盟大营 / 堡垒）+ 城池列表（城 / 水寨 / 军港 / 民港）。
        按距离从近到远尝试，跳转后校验右侧有没有「调动」按钮；
        没有（例如「无法入驻」的枢要之地）就换下一个，最多试 tries 个。

        `camps` 传入已扫描好的列表时跳过扫描（自动拉线循环复用上一轮结果，
        省掉每轮 ≈340s 的同盟建筑 + 14 屏城池列表 OCR）。
        """
        if camps is None:
            camps = self.scan_all()
        camps = [c for c in camps
                 if not any(h in c.note for h in NO_MOBILIZE_HINTS)]
        if not camps:
            self.log("  × 同盟建筑 / 城池列表都没解析到可跳转的据点")
            self.back_to_map()
            return None

        ordered = sorted(camps, key=lambda c: c.dist(target))
        for c in ordered[:6]:
            self.log(f"  候选 {c.describe()} 距目标 {c.dist(target):.1f}")

        for i, c in enumerate(ordered[:tries]):
            if not self.select(c, confirm_wait=confirm_wait):
                self.back_to_map()
                continue
            if self.has_mobilize_button():
                # 选中状态只维持几秒：这里不再做名称/坐标 OCR，立刻交给调用方开「调动」
                self.log(f"== 最近可调动据点：{c.describe()}（距目标 {c.dist(target):.1f}）")
                return c
            self.log(f"  ! {c.describe()} 跳转后没有「{MOBILIZE_BUTTON}」按钮，换下一个")
            self.back_to_map()

        self.log("  × 最近的几个据点都没有「调动」按钮")
        self.back_to_map()
        return None


def jump_dialog_visible(adb: Adb) -> bool:
    img = adb.screencap()
    text = ocrengine.ocr_text(vision.crop(img, config.JUMP_DIALOG_BOX), 2.0)
    return "跳转到坐标" in text or "是否跳转" in text
