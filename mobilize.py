"""同盟大营「调动」：把 部队1-4 逐支派往大营，并记录最后到达时间。

实测补充（2026-11-21）：
- 出征后可能先弹「部队未携带满战法，是否继续出征？」确认框 → 自动点「继续出征」
  （实测按钮文字中心 (609,543)，固定兜底 (608,541)）
- 派兵前读卡片底部「兵力 N」；< 30000 先点出征左侧白色「补兵 / 自动征兵」，
  补后仍 < 30000 就跳过该队；OCR 读不到时不误跳过

实测要点（1600x900）：
- 大营选中后右侧竖排按钮：行军 / 调动 / 驻守 …
- 「调动」面板：上半是选点地图，下半是 5 张部队卡 + 右侧「体力 / 耗时」+ 金色「出征」按钮
- **出征不会关闭面板**：被派出的部队卡状态由「空闲」变成「行军 00:08:23」，
  面板保持打开，可继续选下一支部队再点出征
- 卡片坐标：x = 115 / 323 / 539 / 760 / 960，y ≈ 700
- 出征按钮是金色圆角块（**实测本体 bbox = 1283,779–1576,861**）；
  注意它上方 y≈680–760 还有金色的「体力 / 耗时」图标和文字，
  所以定位时按「整行金色像素数量」过滤行，只留按钮本体，点击点取包围盒正中；
  点完轮询卡面状态确认（行军/调动），没生效就先确认面板/选卡再换点重点
- 从 城 / 水寨 / 军港 / 民港 调动时，点「出征」成功后面板会**自动收起**（同盟大营不会）；
  面板收起 ≠ 失败：要重新选中该据点再开面板核对（`reselect` 回调）
- 出征按钮可能整体是禁用灰块（无可用路径/受限）：此时靠提示行判断面板是否打开，
  不要按「金色按钮不存在」去重复点「调动」，否则会把面板开关反、进而在错误界面上乱点
- 状态 OCR 必须按卡裁窄条（0.3s/张），整条状态带切块一次要 10s+，是「操作很卡」的元凶
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from PIL import Image

import re

from . import config, ocrengine, vision
from .adb import Adb
from .mapnav import MapNav
from .timing import STAGE_MOBILIZE

# 状态文字规范化：OCR 会把同一行邻卡文字/倒计时粘在一起
_STATUS_RE = re.compile(r"(空闲|停留|行军|调动|驻守|出征)(?:\D{0,2}(\d{1,2}:\d{2}:\d{2}))?")

# 卡片中心（全分辨率）
CARD_X = {1: 115, 2: 323, 3: 539, 4: 760, 5: 960}
CARD_Y = 700
# 卡片状态文字条带（避开上方「请选择出征部队」提示行 y≈592）
STATUS_BAND = (0, 612, 1090, 678)
# 单卡状态条：整条带切块 OCR 一次要 10s+，按卡裁窄条只要 0.3s（实测）
STATUS_CARD_WIDE = 130          # 首选：cx±130（能同时读到「行军 + 倒计时」）
STATUS_CARD_NARROW = 95         # 兜底：cx±95（个别卡窄条才读得出）
# 选中卡上边框是亮金色（实测在 y≈629，宽约 210 像素，单行金色像素 ≈124；
# 未选中的卡这一行几乎为 0），用它判断「到底选中了哪支部队」
CARD_SELECT_BAND = (620, 646)
CARD_HALF_W = 105
CARD_SELECT_MIN = 100      # 选中卡 ≈116~124，非选中卡 ≤10（避免误判）
# 右侧「耗时」值
COST_BOX = (1455, 698, 1600, 742)
# 金色「出征」按钮的搜索区域：只框按钮所在的下半块
# 实测按钮本体 bbox = 1283,779–1576,861；上方 y≈680–760 的金色「体力 / 耗时」
# 图标与文字必须排除，否则算出来的点击点会落在按钮上方的说明行（点不出征的主因）
EXIT_REGION = (1200, 745, 1600, 890)
# 按钮本体最小尺寸与金色像素总量下限
EXIT_MIN_W, EXIT_MIN_H = 160, 44
EXIT_MIN_AREA = 4000        # 能定位到按钮的最低金色像素总量（禁用态也高于它）
EXIT_DIM_PIXELS = 14000     # 正常态约 1.7 万，低于此值说明按钮被压暗（该部队被限制）
# 按钮宽高比（293x82 ≈ 3.6），用来排除地图上的其它金色块
EXIT_RATIO = (2.0, 5.0)
# 金色判定：先严格，找不到再放宽（按钮禁用态会暗一些）
GOLD_STRICT = (205, 175, 120, 205)      # r>205 g>175 120<b<205
GOLD_LOOSE = (180, 150, 100, 215)
# 兜底点：实测按钮中心 (1430,820)，全部落在按钮内部
EXIT_FALLBACKS = ((1430, 820), (1430, 806), (1430, 834), (1400, 820))
# ---------------------------------------------------------------------------
# 兵力 / 补兵（实测截图 n2_调动_094349.jpg，1600x900）
# 卡片底部一行「兵力 30000」；其中「兵力」在卡左半、数字在卡右半（y≈800–865），
# 单卡按 cx-125..cx+115 裁条 OCR 可稳定读到（整条带一次 OCR 会把 5 张卡粘成一行）
# ---------------------------------------------------------------------------
STRENGTH_BAND = (800, 868)
STRENGTH_BOX_DX = (-125, 115)       # 相对卡片中心 x 的左右偏移
STRENGTH_NUMBER_DX = 70             # 数字片段大致中心（相对卡片中心 x）
STRENGTH_FULL = 30000               # 单队满编兵力（低于它视为不满，需要补兵）
STRENGTH_OCR_SCALES = (3.5, 4.0, 3.0)
# 「补兵」按钮：出征按钮附近（不同据点面板位置会变，实测有「一键补兵」贴出征上方，
# 也有白色小方块贴在出征左侧）。注意卡片行里的红色「征兵 03:18:30」是自动征兵的
# 倒计时徽标，不是按钮；右下角「消耗辎重」开关也不要碰。
REPLENISH_REGION = (1000, 620, 1600, 880)
REPLENISH_REGIONS = ((1000, 620, 1600, 880), (1000, 300, 1600, 470))
REPLENISH_HINTS = ("补兵", "一键补兵")
REPLENISH_SKIP_HINTS = ("消耗辎重", "征兵 ", "自动", "今日已补兵")
# 补兵后可能的确认弹窗（只认「确定/确认/立即征兵」，避免把「自动征兵」文字再点一次）
REPLENISH_DIALOG_BOX = (400, 300, 1260, 800)
REPLENISH_CONFIRM_HINTS = ("确定", "确认", "立即征兵")
# 「调动」面板的静态提示行：出征按钮不可用（灰色）时靠它判断面板仍然开着
PANEL_HINT_BOX = (400, 575, 1150, 610)
PANEL_HINTS = ("请选择出征部队", "调动速度")
# 出征确认弹窗：「部队未携带满战法，是否继续出征？」→ 点左侧「继续出征」
# 实测（1600x900）：提示框居中，继续出征 ≈ (608,541)，前往调整 ≈ (987,541)
DISPATCH_DIALOG_BOX = (420, 260, 1300, 800)
DISPATCH_DIALOG_HINTS = ("是否继续出征", "未携带满战法", "继续出征")
DISPATCH_CONTINUE_POINT = (608, 541)
# 顶部服务器时间
SERVER_TIME_BOX = (1425, 16, 1592, 64)
# 面板左上返回
PANEL_BACK = (100, 25)

RUNNING_HINTS = ("行军", "调动", "出征", "驻守")


@dataclass
class Troop:
    index: int
    status: str = ""
    cost_text: str = ""
    dispatched: bool = False
    arrived: bool = False
    arrive_at: str = ""
    strength: int | None = None        # 出征前读到的兵力
    replenished: bool = False          # 是否点过补兵
    skipped_reason: str = ""           # low-strength / select-failed …

    @property
    def idle(self) -> bool:
        return "空闲" in self.status


@dataclass
class MobilizeResult:
    camp: str = ""
    troops: list[Troop] = field(default_factory=list)
    last_arrival: str = ""
    last_troop: int = 0

    def as_dict(self) -> dict:
        return {
            "camp": self.camp,
            "troops": [{"index": t.index, "status": t.status,
                        "cost": t.cost_text, "dispatched": t.dispatched,
                        "arrived": t.arrived,
                        "arrive_at": t.arrive_at,
                        "strength": t.strength,
                        "replenished": t.replenished,
                        "skipped": t.skipped_reason} for t in self.troops],
            "last_arrival": self.last_arrival,
            "last_troop": self.last_troop,
        }


class ServerClock:
    """服务器时间基准。

    实测：游戏内顶栏时间与本机时间一致（东八区），且「调动」面板打开后
    顶栏会被压暗、时间读不到，所以用「一次同步 + 本机计时」的方式推算。
    """

    def __init__(self, adb: Adb | None = None):
        self.adb = adb
        self._base = None
        self._base_local = None

    _TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")

    def sync(self) -> str:
        """尝试 OCR 顶栏时间；失败则退化成本机时间。"""
        raw = ""
        if self.adb is not None:
            img = self.adb.screencap()
            for s in ocrengine.ocr(vision.crop(img, SERVER_TIME_BOX), 4.0):
                t = s.text.replace("：", ":").replace(" ", "").strip()
                m = self._TIME_RE.search(t)
                if not m:
                    continue
                h, mi, se = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if h < 24 and mi < 60 and se < 60:
                    raw = f"{h:02d}:{mi:02d}:{se:02d}"
                    break
        if raw:
            parts = [int(p) for p in raw.split(":")]
            self._base = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
            now = time.localtime()
            self._base = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
        self._base_local = time.monotonic()
        return raw or _fmt_seconds(self._base)

    def now(self) -> str:
        if self._base is None:
            self.sync()
        elapsed = int(time.monotonic() - self._base_local)
        return _fmt_seconds(self._base + elapsed)


def _to_seconds(text: str) -> int | None:
    parts = text.replace("：", ":").replace(" ", "").split(":")
    try:
        nums = [int(p) for p in parts if p.isdigit()]
    except ValueError:
        return None
    if not nums:
        return None
    s = 0
    for n in nums:
        s = s * 60 + n
    return s


def _normalize_status(text: str) -> str:
    """从一堆粘连文字里抽「状态 + 倒计时」，例如 '行军行军行军00:09:25' -> '行军 00:09:25'。"""
    m = _STATUS_RE.search(text)
    if not m:
        return ""
    kind, timer = m.group(1), m.group(2)
    return f"{kind} {timer}" if timer else kind


def _status_seconds(status: str) -> int | None:
    """从卡面状态里取剩余秒数（'行军 00:08:23' -> 503）；没有倒计时返回 None。"""
    m = _STATUS_RE.search(status or "")
    if m and m.group(2):
        return _to_seconds(m.group(2))
    return None


def _fmt_seconds(total: int) -> str:
    total %= 24 * 3600
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _add_seconds(hhmmss: str, seconds: int) -> str:
    parts = ([int(p) for p in hhmmss.split(":")]
             if hhmmss.count(":") == 2 else [0, 0, 0])
    base = parts[0] * 3600 + parts[1] * 60 + parts[2]
    return _fmt_seconds(base + seconds)


class Mobilizer:
    def __init__(self, adb: Adb | None = None, log=print, reselect=None,
                 timer=None):
        self.adb = adb or Adb()
        self.nav = MapNav(self.adb, log=log)
        self.clock = ServerClock(self.adb)
        self.log = log
        # 面板被关掉时用来「重新跳回该据点并选中」的回调（城市/港口出征后会收面板）
        self.reselect = reselect
        self._last_img: Image.Image | None = None
        # 可选的阶段计时器（见 sanmou_auto/timing.py），None 时不记录
        self.timer = timer

    def _mark(self, name: str, kind: str = "执行", note: str = "",
              stage: str = STAGE_MOBILIZE) -> None:
        if self.timer is not None:
            self.timer.mark(name, kind=kind, note=note, stage=stage)

    # -- 打开 / 关闭 -------------------------------------------------------
    @staticmethod
    def _scaled_box(img: Image.Image, box: tuple[int, int, int, int]):
        """配置坐标是 1600x900；截图被缩放时按比例换算并夹到图像范围内。"""
        k = img.width / config.SCREEN[0]
        if abs(k - 1.0) < 1e-3:
            return box
        x1 = max(0, min(img.width, round(box[0] * k)))
        y1 = max(0, min(img.height, round(box[1] * k)))
        x2 = max(0, min(img.width, round(box[2] * k)))
        y2 = max(0, min(img.height, round(box[3] * k)))
        return (x1, y1, x2, y2)

    def _hint_visible(self, img: Image.Image) -> bool:
        """面板提示行「请选择出征部队 / 调动速度…」——出征按钮变灰时也能认出面板。"""
        text = "".join(s.text for s in ocrengine.ocr(
            vision.crop(img, self._scaled_box(img, PANEL_HINT_BOX)),
            2.0)).replace(" ", "")
        return any(h in text for h in PANEL_HINTS)

    def panel_open(self, img: Image.Image | None = None) -> bool:
        """判断「调动」面板是否已经打开。

        出征按钮可用时按金色按钮判断（快）；按钮被禁用（灰）时金色块不存在，
        再 OCR 提示行兜底——否则会把「面板开着但按钮不可用」误判成面板没开，
        进而反复点「调动」把面板开关反了。
        """
        img = img or self.adb.screencap()
        if self.find_exit_box(img) is not None:
            return True
        return self._hint_visible(img)

    def open_from_camp(self, wait: float = 1.8, tries: int = 3) -> bool:
        """点「调动」打开面板；地图上没有该按钮时用 reselect 重新选中据点。

        实测：世界聊天气泡会横穿右侧按钮列，把「行军/调动/驻守」挡住
        （OCR 只读出聊天句子）。所以没读到「调动」时先等几秒重试（气泡会滚走），
        再把「面板是否打开」当作最终判据，而不是硬性要求按钮文字齐全。
        """
        if self.panel_open():
            return True
        reselect_used = False
        for i in range(max(1, tries)):
            if self.nav.chat_panel_visible():
                self.nav.close_chat_panel()
            # reselect 可能已经把面板重新打开了：每轮开头都再确认一次
            if self.panel_open():
                return True
            btns = self.nav.find_all_buttons(attempts=2)
            hit = next(((t, p) for t, p in btns if "调动" in t), None)
            if hit:
                self.log(f"== 点击「{hit[0]}」@{hit[1]}")
                self.adb.tap(*hit[1])
                time.sleep(wait)
                # 面板有时带 toast/动画，最多等 4s
                for _ in range(6):
                    if self.panel_open():
                        return True
                    time.sleep(0.7)
                self.log(f"  · 第{i + 1}次点「调动」后面板没打开（可能点到地图标签），重试")
                self.nav.ensure_map()
                continue
            if self.reselect and not reselect_used and i >= 1:
                reselect_used = True
                self.log(f"  · 地图上没有「调动」按钮（当前: {[t for t, _ in btns]}），"
                         "重新选中据点")
                if not self.reselect():
                    self.log("  × 重新选中据点失败")
                    return False
                continue
            if i < tries - 1:
                self.log(f"  · 没读到「调动」（当前: {[t for t, _ in btns]}），"
                         "等 3s 让聊天气泡滚走再试")
                time.sleep(3.0)
                continue
            self.log(f"  × 没找到「调动」按钮，当前按钮: {[t for t, _ in btns]}")
            return False
        return self.panel_open()

    def _confirm_dispatch(self, img: Image.Image | None = None) -> bool:
        """处理出征后的确认框（未携带满战法 / 是否继续出征）→ 点「继续出征」。"""
        img = img or self.adb.screencap()
        k = img.width / config.SCREEN[0]
        box = self._scaled_box(img, DISPATCH_DIALOG_BOX)
        spans = ocrengine.ocr(vision.crop(img, box), 2.0)
        text = "".join(s.text for s in spans)
        if not any(h in text for h in DISPATCH_DIALOG_HINTS):
            return False
        for s in spans:
            if "继续出征" in s.text.replace(" ", ""):
                point = (int((box[0] + s.cx) / k), int((box[1] + s.cy) / k))
                self.log(f"  · 出征确认框：点击「继续出征」@{point}")
                self.adb.tap(*point)
                time.sleep(1.2)
                return True
        self.log(f"  · 出征确认框：按固定点点击「继续出征」{DISPATCH_CONTINUE_POINT}")
        self.adb.tap(*DISPATCH_CONTINUE_POINT)
        time.sleep(1.2)
        return True

    def close(self) -> None:
        # 出征后可能残留「未携带满战法，是否继续出征？」弹窗，先点掉它
        self.nav.dismiss_modal()
        # 城池/港口出征成功后 面板本来就会收起；这时再点左上角会误触地图 UI
        if not self.panel_open():
            self.log("  · 调动面板已收起，无需再关")
            return
        self.adb.tap(*PANEL_BACK)
        time.sleep(1.0)

    # -- 读取 -------------------------------------------------------------
    def _read_card_status(self, img: Image.Image, idx: int) -> str:
        """只裁这一张卡的状态条来 OCR（0.3s），读不到再换窄条/稍放大。"""
        cx = CARD_X[idx]
        y1, y2 = STATUS_BAND[1], STATUS_BAND[3]
        for half, scale, dy in ((STATUS_CARD_WIDE, 3.0, 0),
                                (STATUS_CARD_NARROW, 3.5, 0),
                                (STATUS_CARD_WIDE, 3.0, 8)):
            box = (max(0, cx - half), y1 + dy,
                   min(img.width, cx + half), min(img.height, y2 + dy))
            text = "".join(s.text for s in
                           ocrengine.ocr(vision.crop(img, box), scale))
            norm = _normalize_status(text.replace(" ", ""))
            if norm:
                return norm
        return ""

    def read_status(self, idx: int, img: Image.Image | None = None) -> str:
        """读某张部队卡左上角的状态文字（空闲 / 行军 00:08:23 / 停留 / 调动）。"""
        img = img or self.adb.screencap()
        return self._read_card_status(img, idx)

    def read_statuses(self, img: Image.Image | None = None) -> dict[int, str]:
        """逐张卡裁窄条 OCR（5 张约 2s；整条带切块要 10s+）。"""
        img = img or self.adb.screencap()
        out: dict[int, str] = {}
        for idx in CARD_X:
            st = self._read_card_status(img, idx)
            if st:
                out[idx] = st
        return out

    def read_cost(self, img: Image.Image | None = None) -> tuple[str, int | None]:
        img = img or self.adb.screencap()
        for s in ocrengine.ocr(vision.crop(img, COST_BOX), 4.0):
            t = s.text.replace("：", ":").strip()
            if t.count(":") == 2:
                return t, _to_seconds(t)
        return "", None

    def read_server_time(self, img: Image.Image | None = None) -> str:
        img = img or self.adb.screencap()
        best = ""
        for s in ocrengine.ocr(vision.crop(img, SERVER_TIME_BOX), 4.0):
            t = s.text.replace("：", ":").replace(" ", "").strip()
            if t.count(":") == 2 and all(p.isdigit() for p in t.split(":")):
                if not best:
                    best = t
        return best

    # -- 兵力 / 补兵 ------------------------------------------------------
    @staticmethod
    def _num_from_text(text: str) -> int | None:
        """从 OCR 文字里取兵力数字（容忍 O/〇/l 等误识别）。"""
        t = (text.replace(" ", "").replace("O", "0").replace("o", "0")
             .replace("〇", "0").replace("l", "1").replace("I", "1"))
        nums = re.findall(r"\d{1,6}", t)
        for n in nums:
            v = int(n)
            if 0 < v <= 999999:
                return v
        return None

    def read_strength(self, idx: int, img: Image.Image | None = None) -> int | None:
        """读某支部队卡底部的「兵力 N」。

        实测卡片布局（1600x900）：`兵力 30000` 在卡底部 y≈800–865，
        「兵力」在卡左半、数字在卡右半。按 cx-125..cx+115 裁条后逐段 OCR，
        再取「离数字区（cx+70）最近」那一段里的数字，避免读到邻卡的数字。
        """
        img = img or self.adb.screencap()
        k = img.width / config.SCREEN[0]
        cx = CARD_X[idx]
        dx1, dx2 = STRENGTH_BOX_DX
        box = (max(0, round((cx + dx1) * k)), round(STRENGTH_BAND[0] * k),
               min(img.width, round((cx + dx2) * k)),
               min(img.height, round(STRENGTH_BAND[1] * k)))
        crop = vision.crop(img, box)
        # 多个缩放各读一次：OCR 偶尔把 30000 读成 3000（丢尾零），
        # 取「位数最多、同位数更大」的结果，减少把满编误判成不满
        best: tuple[int, int] | None = None
        for scale in STRENGTH_OCR_SCALES:
            nearest: tuple[float, int] | None = None
            for s in ocrengine.ocr(crop, scale):
                v = self._num_from_text(s.text)
                if v is None:
                    continue
                full_x = (box[0] + s.cx) / k
                d = abs(full_x - (cx + STRENGTH_NUMBER_DX))
                if nearest is None or d < nearest[0]:
                    nearest = (d, v)
            if nearest is None:
                continue
            key = (len(str(nearest[1])), nearest[1])
            if best is None or key > best:
                best = key
        return best[1] if best is not None else None

    def read_strengths(self, img: Image.Image | None = None) -> dict[int, int]:
        img = img or self.adb.screencap()
        out: dict[int, int] = {}
        for idx in CARD_X:
            v = self.read_strength(idx, img)
            if v is not None:
                out[idx] = v
        return out

    @staticmethod
    def _white_block_center(img: Image.Image, box: tuple[int, int, int, int]):
        """在 box 内找一块近白色小方块（补兵复选框），返回中心点。"""
        k = img.width / config.SCREEN[0]
        x1 = max(0, round(box[0] * k))
        y1 = max(0, round(box[1] * k))
        x2 = min(img.width, round(box[2] * k))
        y2 = min(img.height, round(box[3] * k))
        if x2 - x1 < 4 or y2 - y1 < 4:
            return None
        px = img.load()
        cells: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for y in range(y1, y2, 2):
            for x in range(x1, x2, 2):
                r, g, b = px[x, y]
                if r >= 200 and g >= 200 and b >= 200:
                    cells.setdefault((x // 10, y // 10), []).append((x, y))
        best = None
        for pts in cells.values():
            if len(pts) < 40:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            w = max(xs) - min(xs)
            h = max(ys) - min(ys)
            if not (8 <= w <= 90 and 8 <= h <= 90):
                continue
            score = len(pts) - abs(w - h) * 2
            if best is None or score > best[0]:
                best = (score, sum(xs) // len(xs), sum(ys) // len(ys))
        if best is None:
            return None
        return best[1] / k, best[2] / k

    def find_replenish_button(self, img: Image.Image | None = None):
        """定位「补兵 / 一键补兵」按钮，返回点击点；找不到返回 None（不瞎点）。"""
        img = img or self.adb.screencap()
        k = img.width / config.SCREEN[0]
        for region in REPLENISH_REGIONS:
            box = self._scaled_box(img, region)
            best = None
            for s in ocrengine.ocr(vision.crop(img, box), 2.0):
                t = s.text.replace(" ", "")
                if any(bad in t for bad in REPLENISH_SKIP_HINTS):
                    continue
                if any(h in t for h in REPLENISH_HINTS):
                    if best is None or len(t) < len(best[0]):
                        best = (t, (int((box[0] + s.cx) / k),
                                    int((box[1] + s.cy) / k)))
            if best:
                return best[1]
        pt = self._white_block_center(
            img, (REPLENISH_REGION[0], REPLENISH_REGION[1] + 10,
                  REPLENISH_REGION[2], REPLENISH_REGION[3]))
        return (int(pt[0]), int(pt[1])) if pt else None

    def _confirm_replenish(self, img: Image.Image | None = None) -> bool:
        """补兵后若弹出「确定/确认/立即征兵」弹窗，点掉它。"""
        img = img or self.adb.screencap()
        k = img.width / config.SCREEN[0]
        box = self._scaled_box(img, REPLENISH_DIALOG_BOX)
        for s in ocrengine.ocr(vision.crop(img, box), 2.0):
            t = s.text.replace(" ", "")
            if any(h in t for h in REPLENISH_CONFIRM_HINTS):
                point = (int((box[0] + s.cx) / k), int((box[1] + s.cy) / k))
                self.log(f"  · 补兵确认弹窗「{t}」@{point}")
                self.adb.tap(*point)
                time.sleep(1.0)
                return True
        return False

    def ensure_strength(self, idx: int, min_strength: int = STRENGTH_FULL,
                        replenish: bool = True) -> tuple[int | None, bool, bool]:
        """确保部队兵力达标：不满先点白色补兵，补完仍不满就跳过。

        返回 (兵力, 是否可以出征, 是否点过补兵)；读不到兵力时按「可以出征」处理
        （OCR 失败不能当作兵力不足，否则会误跳过整支部队）。
        """
        st = self.read_strength(idx)
        if st is None:
            self.log(f"  ! 部队{idx} 读不到兵力，跳过补兵检查")
            return None, True, False
        if st >= min_strength:
            self.log(f"  部队{idx} 兵力 {st}（达标）")
            return st, True, False
        if not replenish:
            self.log(f"  × 部队{idx} 兵力 {st} < {min_strength}，未启用补兵 → 跳过")
            return st, False, False
        # 补兵作用于「当前选中」的部队：先点卡确认选中
        if not self.select_card(idx):
            self.log(f"  × 部队{idx} 兵力 {st} 不满，但点选不中无法补兵 → 跳过")
            return st, False, False
        point = self.find_replenish_button()
        if not point:
            self.log(f"  ! 部队{idx} 没找到「补兵」按钮（可能已在自动征兵），跳过")
            return st, False, False
        self.log(f"  部队{idx} 兵力 {st} < {min_strength} → 点击补兵 {point}")
        self.adb.tap(*point)
        time.sleep(1.6)
        self._confirm_replenish()
        time.sleep(0.8)
        st2: int | None = None
        for _ in range(4):
            st2 = self.read_strength(idx)
            if st2 is not None and st2 >= min_strength:
                break
            time.sleep(0.8)
        if st2 is None:
            self.log(f"  ! 部队{idx} 补兵后读不到兵力，按已补兵处理")
            return st, True, True
        ok = st2 >= min_strength
        self.log(f"  部队{idx} 补兵后兵力 {st2}"
                 + ("" if ok else f" 仍 < {min_strength} → 跳过"))
        return st2, ok, True

    # -- 金色出征按钮 -----------------------------------------------------
    @staticmethod
    def _gold_row_spans(px, region, bounds):
        """逐行统计金色像素，返回 ([(count, first_x, last_x), ...], 总数)。"""
        x0, y0, x1, y1 = region
        rmin, gmin, bmin, bmax = bounds
        rows: list[tuple[int, int, int]] = []
        total = 0
        for y in range(y1 - y0):
            cnt = 0
            first = -1
            last = -1
            for x in range(x1 - x0):
                r, g, b = px[x0 + x, y0 + y]
                if r > rmin and g > gmin and bmin < b < bmax:
                    cnt += 1
                    if first < 0:
                        first = x
                    last = x
            total += cnt
            rows.append((cnt, first, last))
        return rows, total

    def _locate_exit(self, img: Image.Image | None = None,
                     region: tuple[int, int, int, int] = EXIT_REGION):
        """定位金色「出征」按钮本体，返回 ((x1,y1,x2,y2), 金色像素数)。

        按钮上方还有金色的「体力 / 耗时」图标与文字，直接取所有金色像素的
        最小 y 会把点击点算到按钮外面（这就是点不出征的主因）。这里改成按行过滤：
        一行里金色像素达到「按钮行」量级才计入，文字/图标一行只有几十个像素会被剔掉，
        剩下的就是按钮本体，点击点取包围盒正中，稳定落在按钮内部。
        """
        img = img or self.adb.screencap()
        px = img.load()
        # 配置里的坐标都是 1600x900 全分辨率；截图被缩放时先换算，避免越界
        k = img.width / config.SCREEN[0]
        x0, y0 = round(region[0] * k), round(region[1] * k)
        x1 = min(img.width, round(region[2] * k))
        y1 = min(img.height, round(region[3] * k))
        region = (x0, y0, x1, y1)
        # 一行里金色像素占区域宽度的 1/3 以上才算「按钮行」；文字/图标远达不到
        need = max(60, (x1 - x0) // 3)
        for bounds in (GOLD_STRICT, GOLD_LOOSE):
            rows, total = self._gold_row_spans(px, region, bounds)
            band = [i for i, (cnt, _, _) in enumerate(rows) if cnt >= need]
            if not band:
                continue
            top, bottom = band[0], band[-1]
            left = min(rows[i][1] for i in band)
            right = max(rows[i][2] for i in band)
            w, h = right - left, bottom - top
            if w < EXIT_MIN_W * k or h < EXIT_MIN_H * k:
                continue
            if total < EXIT_MIN_AREA * k * k:
                continue
            if not EXIT_RATIO[0] <= (w / max(1, h)) <= EXIT_RATIO[1]:
                continue
            box = tuple(round(v / k) for v in
                        (x0 + left, y0 + top, x0 + right, y0 + bottom))
            return box, round(total / (k * k))
        return None, 0

    def find_exit_box(self, img: Image.Image | None = None,
                      region: tuple[int, int, int, int] = EXIT_REGION):
        """返回出征按钮本体 bbox (x1, y1, x2, y2)；找不到返回 None。"""
        return self._locate_exit(img, region)[0]

    def find_exit_button(self, img: Image.Image | None = None,
                         region: tuple[int, int, int, int] = EXIT_REGION):
        """返回 (按钮中心点, 按钮区域金色像素数)；找不到返回 (None, 0)。"""
        box, count = self._locate_exit(img, region)
        if not box:
            return None, 0
        x1, y1, x2, y2 = box
        return ((x1 + x2) // 2, (y1 + y2) // 2), count

    def exit_point_candidates(self, img: Image.Image | None = None):
        """按钮内部的一组点击点（中心优先），最后才退化成实测固定点。"""
        img = img or self.adb.screencap()
        box = self.find_exit_box(img)
        cands: list[tuple[int, int]] = []
        if box:
            x1, y1, x2, y2 = box
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            dy = max(8, (y2 - y1) // 6)
            dx = max(20, (x2 - x1) // 4)
            cands += [(cx, cy), (cx, cy - dy), (cx, cy + dy),
                      (cx - dx, cy), (cx + dx, cy)]
        cands += list(EXIT_FALLBACKS)
        seen = set()
        uniq = []
        for p in cands:
            if p in seen:
                continue
            seen.add(p)
            uniq.append(p)
        return uniq

    # -- 选卡 -------------------------------------------------------------
    def card_selected_score(self, img: Image.Image, idx: int) -> int:
        """选中卡的上边框是亮金色；返回该条带里最亮一行的金色像素数。

        实测：选中卡 ≈124，未选中卡 ≈0，区分度很高，
        因此可以准确判断「现在选中的到底是哪支部队」，避免把别人的部队派出去。
        """
        k = img.width / config.SCREEN[0]
        cx = round(CARD_X[idx] * k)
        x1 = max(0, cx - round(CARD_HALF_W * k))
        x2 = min(img.width, cx + round(CARD_HALF_W * k))
        y1, y2 = (round(v * k) for v in CARD_SELECT_BAND)
        y2 = min(y2, img.height)
        px = img.load()
        best = 0
        for y in range(y1, min(y2, img.height)):
            cnt = 0
            for x in range(x1, x2):
                r, g, b = px[x, y]
                if r > 205 and g > 175 and 120 < b < 205:
                    cnt += 1
            if cnt > best:
                best = cnt
        return best

    def selected_card(self, img: Image.Image | None = None) -> int:
        """返回当前高亮的部队序号；识别不到返回 0。"""
        img = img or self.adb.screencap()
        scores = {i: self.card_selected_score(img, i) for i in CARD_X}
        idx, best = max(scores.items(), key=lambda kv: kv[1])
        return idx if best >= CARD_SELECT_MIN else 0

    def select_card(self, idx: int, tries: int = 3, wait: float = 0.6) -> bool:
        """点选部队卡，并用「金框高亮」确认选中的确实是这一支。

        每次点之前都确认「调动」面板还在：面板没了还按原坐标点，
        会点到地图左下角的「军报」→ 打开好友/聊天面板（没有关闭按钮）。
        """
        if not self.panel_open():
            self.log("  ! 「调动」面板不在，拒绝点卡片（避免误触地图 UI）")
            return False
        x, y = CARD_X[idx], CARD_Y
        offsets = ((0, 0), (-12, 0), (12, 0), (0, -16), (0, 16))
        for i in range(max(1, min(tries, len(offsets)))):
            dx, dy = offsets[i]
            self.adb.tap(x + dx, y + dy)
            time.sleep(wait)
            img = self.adb.screencap()
            if not self.find_exit_box(img):
                self.log("  ! 点完卡片面板消失，停止点选")
                self._last_img = img
                return False
            if self.selected_card(img) == idx:
                self._last_img = img
                return True
            self.log(f"  · 部队{idx} 第{i + 1}次点选未命中，换位置再点")
        return False

    # -- 派兵 -------------------------------------------------------------
    @staticmethod
    def read_status_idle(status: str) -> bool:
        return "空闲" in status

    def _running(self, status: str) -> bool:
        return bool(status) and not self.read_status_idle(status)

    def _poll_status(self, idx: int, timeout: float = 1.2,
                     interval: float = 0.4) -> str:
        """点完出征后轮询卡面状态。

        模拟器/服务器有延迟，单次 OCR 容易把「已经出发」误判成失败，于是重复点击。
        这里在超时窗口内多读几次，读到非「空闲」立刻返回。
        """
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            st = self.read_status(idx)
            if st:
                last = st
                if not self.read_status_idle(st):
                    return st
            if time.monotonic() >= deadline:
                return last
            time.sleep(interval)

    def _mark_dispatched(self, troop: Troop, st: str,
                         cost_sec: int | None, fallback: str) -> None:
        troop.status = st
        troop.dispatched = True
        sec = _status_seconds(st)
        if sec is not None:
            troop.cost_text = _fmt_seconds(sec)
            troop.arrive_at = _add_seconds(self.clock.now(), sec)
        elif cost_sec is not None:
            troop.arrive_at = _add_seconds(self.clock.now(), cost_sec)
        else:
            troop.arrive_at = fallback

    def dispatch_one(self, idx: int, attempts: int = 3,
                     wait: float = 0.9) -> Troop | None:
        """选中第 idx 支部队并点出征；点完校验状态，失败先复位再换点重点。

        注意：从「城池 / 水寨 / 军港 / 民港」调动时，点「出征」成功后面板会**自动收起**
        （同盟大营不会）。所以「面板没了」不能当失败处理，要重新打开面板核对状态。
        """
        troop = Troop(index=idx)
        if not self.select_card(idx):
            troop.status = self.read_status(idx)
            self.log(f"  × 部队{idx} 点选不中（金框高亮没出现），"
                     f"状态「{troop.status or '未知'}」")
            return troop

        img = self._last_img or self.adb.screencap()
        cost_text, cost_sec = self.read_cost(img)
        server = self.clock.now()
        arrive = ""
        if cost_sec is None:
            self.log(f"  ! 部队{idx} 读不到耗时，仍尝试出征"
                     "（到达时间用出征后的卡面倒计时推算）")
        else:
            troop.cost_text = cost_text
            arrive = _add_seconds(server or "00:00:00", cost_sec)
            self.log(f"== 部队{idx} 耗时 {cost_text} 服务器时间 "
                     f"{server or '未读到'} -> 预计 {arrive} 到达")

        box, count = self._locate_exit(img)
        if box:
            x1, y1, x2, y2 = box
            self.log(f"  出征按钮 bbox=({x1},{y1},{x2},{y2}) 金色像素 {count}")
            if count < EXIT_DIM_PIXELS:
                self.log("  ! 出征按钮偏暗，该部队可能被限制不能调动")
        elif self._hint_visible(img):
            troop.status = "出征按钮不可用"
            self.log(f"  × 部队{idx} 出征按钮是禁用态（无可用路径/受限），不点击")
            return troop
        else:
            self.log("  ! 没定位到金色出征按钮，退化成固定点")

        cands = self.exit_point_candidates(img)
        for i, point in enumerate(cands[:max(1, attempts)]):
            self.adb.tap(*point)
            time.sleep(wait)
            # 出征可能先弹「未携带满战法，是否继续出征」确认框，必须先点掉
            self._confirm_dispatch()
            st = self._poll_status(idx, timeout=1.2)
            if st and not self.read_status_idle(st):
                self._mark_dispatched(troop, st, cost_sec, arrive)
                self.log(f"  ✓ 部队{idx} 已出发（点 {point}，状态「{st}」），"
                         f"预计 {troop.arrive_at or '未知'} 到达")
                return troop

            if not st and not self.panel_open():
                # 出征成功后面板自动收起（城池/港口），重开面板核对状态
                self.log("  · 出征后面板已收起，重新打开面板核对状态")
                if self.open_from_camp():
                    st2 = self.read_status(idx)
                    if st2 and not self.read_status_idle(st2):
                        self._mark_dispatched(troop, st2, cost_sec, arrive)
                        self.log(f"  ✓ 部队{idx} 已出发（状态「{st2}」），"
                                 f"预计 {troop.arrive_at or '未知'} 到达")
                        return troop
                if box:
                    # 点击点确实在按钮本体上 → 按「已提交」处理，不要再点
                    self._mark_dispatched(troop, "已出征(面板收起)", cost_sec, arrive)
                    self.log(f"  ✓ 部队{idx} 已提交出征（面板收起无法回读），"
                             f"预计 {troop.arrive_at or '未知'} 到达")
                    return troop
                troop.status = ""
                self.log(f"  × 部队{idx} 面板收起且回读不到状态")
                return troop

            self.log(f"  · 第{i + 1}次点 {point} 未生效（状态「{st or '未知'}」），重试")

            # 复位：面板可能被关掉、选卡可能丢失，下一次点之前都确认一遍
            img = self.adb.screencap()
            if not self.find_exit_box(img):
                self.log("  ! 出征按钮不见了，面板可能被关掉，重开")
                if not self.open_from_camp():
                    break
                img = self.adb.screencap()
            if self.selected_card(img) != idx and not self.select_card(idx):
                self.log(f"  ! 部队{idx} 重新点选失败")
                break

        troop.status = self.read_status(idx)
        self.log(f"  × 部队{idx} 出征失败，状态「{troop.status or '未知'}」")
        return troop

    # -- 主流程 -----------------------------------------------------------
    def mobilize(self, indexes: tuple[int, ...] = (1, 2, 3, 4),
                 skip_busy: bool = True,
                 min_strength: int = STRENGTH_FULL,
                 replenish: bool = True,
                 send_stationed: bool = False) -> MobilizeResult:
        """逐支派兵。

        send_stationed=True 时，已驻扎在据点的「调动 / 驻守」部队也会被派出去
        （正面战斗就是这种情况：先调动到据点，再从这里出征）；在途的
        「行军/出征/调动 + 倒计时」仍然跳过。
        """
        result = MobilizeResult()
        # 顶栏时间只有在面板打开前可见，先同步一次基准
        raw = self.clock.sync()
        self.log(f"  服务器时间基准: {raw}")
        self._mark("同步服务器时间", "分析")
        if not self.open_from_camp():
            return result
        self._mark("打开调动面板", "执行")

        statuses = self.read_statuses()
        # 有的卡 OCR 只读到「行军」没读到倒计时，补读一次
        needs = [k for k, v in statuses.items()
                 if any(x in v for x in ("行军", "调动")) and ":" not in v]
        if needs:
            time.sleep(0.4)
            again = self.read_statuses()
            for k in needs:
                if ":" in again.get(k, ""):
                    statuses[k] = again[k]
        self.log("  当前部队状态: " +
                 (", ".join(f"部队{k}={v}" for k, v in sorted(statuses.items()))
                  or "未识别"))
        self._mark("读取部队卡状态", "分析")
        for idx in indexes:
            if not self.panel_open():
                self.log("  · 调动面板不在了（出征后会自动收起），重新打开")
                if not self.open_from_camp():
                    self.log("  × 面板打不开，停止派兵")
                    break
            st = statuses.get(idx, "")
            in_transit = ":" in st and any(
                k in st for k in ("行军", "出征", "调动"))
            stationed = (not in_transit) and any(
                k in st for k in ("调动", "驻守"))
            if (skip_busy and st and "空闲" not in st
                    and not (send_stationed and stationed)):
                t = Troop(index=idx, status=st)
                m = _STATUS_RE.search(st)
                if m and m.group(2) and any(k in st for k in ("行军", "调动")):
                    # 已经在途：用倒计时反推到点时间
                    sec = _to_seconds(m.group(2))
                    if sec is not None:
                        t.arrive_at = _add_seconds(self.clock.now(), sec)
                        t.dispatched = True
                        t.cost_text = m.group(2)
                elif any(k in st for k in ("调动", "驻守")):
                    # 没有倒计时的「调动/驻守」= 已经到达并驻扎
                    t.arrived = True
                    t.dispatched = True
                    t.arrive_at = self.clock.now()
                self.log(f"  - 部队{idx} 状态「{st}」，跳过"
                         + ("（已到达）" if t.arrived else "")
                         + (f"，预计 {t.arrive_at} 到达"
                            if (t.arrive_at and not t.arrived) else ""))
                result.troops.append(t)
                continue
            # 兵力检查：不满先点白色补兵；补完仍 < min_strength 就跳过这支队
            st0, strength_ok, replenished = self.ensure_strength(
                idx, min_strength=min_strength, replenish=replenish)
            self._mark(f"部队{idx} 兵力检查/补兵", "执行",
                       note=f"兵力={st0} 补兵={replenished}")
            if not strength_ok:
                t = Troop(index=idx, status=f"兵力不足({st0})",
                          strength=st0, replenished=replenished,
                          skipped_reason="low-strength")
                self.log(f"  - 部队{idx} 跳过出征（补兵后兵力 {st0} < {min_strength}）")
                result.troops.append(t)
                continue
            troop = self.dispatch_one(idx)
            self._mark(f"部队{idx} 出征", "执行",
                       note=(troop.status if troop else ""))
            if troop:
                troop.strength = st0
                troop.replenished = replenished
                result.troops.append(troop)

        # 收尾核对：把「点完出征收起面板、没能回读状态」的部队再确认一次
        pending = [t for t in result.troops
                   if t.dispatched and not t.arrived and "已提交" in t.status]
        if pending:
            if self.panel_open() or self.open_from_camp():
                sts = self.read_statuses()
                for t in pending:
                    st = sts.get(t.index, "")
                    if st and "空闲" not in st:
                        sec = _status_seconds(st)
                        t.status = st
                        if sec is not None:
                            t.cost_text = _fmt_seconds(sec)
                            t.arrive_at = _add_seconds(self.clock.now(), sec)
                self.log("  收尾核对：" + ", ".join(
                    f"部队{t.index}={t.status}" for t in pending))
                self._mark("收尾核对状态", "分析")

        # 「最后到达」= 所有已派出部队里到达时间最晚的那支
        # （在途部队用倒计时精确推算；已驻扎部队用观测到的到达时刻近似）
        done = [t for t in result.troops if t.dispatched and t.arrive_at]
        if done:
            last = max(done, key=lambda t: t.arrive_at)
            result.last_arrival = last.arrive_at
            result.last_troop = last.index
            tail = "已到达" if last.arrived else f"剩余 {last.cost_text}"
            self.log(f"== 最后一支到达：部队{last.index} {last.arrive_at}（{tail}）")
            transit = [t.index for t in done if not t.arrived]
            arrived = [t.index for t in done if t.arrived]
            if arrived:
                self.log(f"   已在营地: 部队{arrived}")
            if transit:
                self.log(f"   仍在途: 部队{transit}")
        else:
            self.log("== 没有部队出发成功")
        self._mark("统计最后到达", "分析")
        return result
