"""地图导航：坐标跳转、定位面板找同盟大营、点击与截图。

实测要点：
- 邮件正文里的绿色坐标可直接点击 -> 弹「是否跳转到坐标(x,y)」-> 确定
- 右上「定位」面板页签 建筑 / 收藏 / 同盟；「同盟」页列出本盟建筑：
  名称一行 + 「城池(坐标)」一行，点击条目即跳转并选中该建筑
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import config, ocrengine, vision
from .adb import Adb

COORD_RE = re.compile(r"[(（]\s*(\d{2,4})\s*[,，.、]\s*(\d{2,4})\s*[)）]")
NOISE = ("升级中", "建造中", "拆除中")

# 误入的二级页标题关键字（出现这些说明不在世界地图、也不在城内视角）
WRONG_PAGE_HINTS = ("武将库", "亲卫部队", "部队总览", "战报", "同盟",
                    "城池", "设施", "编队", "邮件")
# 地块/据点详情浮层的右侧动作列（出现 ≥2 个就说明浮层盖住了底栏，邮件点不开）
DETAIL_PANEL_HINTS = ("行军", "调动", "驻守", "生产辎重", "整编", "奔袭")

# 跳转后地块操作按钮的搜索区域（全分辨率，窄条更利于 OCR 检测）
ACTION_BOX = config.ACTION_BOX

# 「好友 / 聊天」面板（误触左下「军报」或顶部箭头会打开，且没有关闭按钮）
CHAT_TITLE_BOX = config._p((20, 20, 420, 200), (0, 0, 620, 320))
CHAT_CLOSE = config._p((1408, 456), (868, 620))
CHAT_HINTS = ("联系人", "群聊", "屏蔽名单")

# 城内视角（回城后出现 君王殿/研究所/亲卫营，左下按钮变成「出城」）
# 地图左下「回城」和城内左下「出城」是同一个位置
CITY_VIEW_BOX = config._p((0, 40, 900, 430), (0, 260, 900, 1000))
CITY_VIEW_HINTS = ("君王殿", "研究所", "亲卫营", "出城", "城内", "城墙", "民居")
CITY_EXIT_POINTS = config._p(
    ((60, 772), (70, 790), (50, 752)),
    ((62, 1355), (75, 1440), (90, 1400)))

# 全屏二级弹窗（实测「部队未携带满战法，是否继续出征？」）会吃掉所有点击：
# 这时同盟页打不开、右侧按钮列 OCR 为空，必须先把弹窗点掉
MODAL_BOX = config._p((420, 290, 1200, 610), (200, 700, 760, 950))
MODAL_HINTS = ("继续出征", "是否继续出征", "未携带满战法")
MODAL_CONTINUE_HINT = "继续出征"
MODAL_CONTINUE_POINT = config._p((610, 543), (366, 845))

# 「确认退出游戏」系统弹窗（在战报/详情页按 BACK 会弹，点错会把游戏退掉）。
# 它是游戏 SDK 的独立 Activity（com.gsc.exit.ExitActivity），按钮可被 uiautomator 读到。
EXIT_CANCEL_POINT = config._p((716, 494), (366, 845))
EXIT_CANCEL_ID = "tv_gs_exit_cancel"

# 「调动 / 出征」面板残留：会盖住底栏，点了邮件图标也没反应（表现为「无法打开邮件页」）。
# 竖屏实测收起点 = 面板右上 ✕ @(860,1240)（底部左侧 3 个候选点全部无效）。
MOBILIZE_PANEL_HINT_BOX = config._p((300, 560, 1300, 620), (0, 1230, 900, 1600))
# 只用面板独有特征：地图底栏本身也有「编队 / 锁定视角」，不能用它们判面板。
MOBILIZE_PANEL_HINTS = ("请选择出征部队", "调动速度")
MOBILIZE_PANEL_HINTS_PAIR = ("不消耗", "耗时")   # 「体力 200(不消耗) + 耗时 hh:mm:ss」组合
# 竖屏实测：地图中央偏右的 ✕ @(528,585) 才是面板关闭键；
# (860,1240)/(115,1548)/(60,1440)/(160,1440) 全部无效。
MOBILIZE_CLOSE = config._p((1560, 95), (528, 585))


@dataclass
class CampItem:
    name: str
    coord: tuple[int, int]
    point: tuple[int, int]         # 点击该条目列表行的屏幕坐标

    def dist(self, other: tuple[int, int]) -> float:
        return math.dist(self.coord, other)

    def describe(self) -> str:
        return f"{self.name}({self.coord[0]},{self.coord[1]}) @{self.point}"


class MapNav:
    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.log = log

    # -- 截图 -------------------------------------------------------------
    def shoot(self, name: str, max_width: int = 1000) -> str:
        img = self.adb.screencap()
        if not name.lower().endswith((".jpg", ".jpeg")):
            name += ".jpg"
        path = vision.save_jpg(img, vision.shot_path(name), max_width, 72)
        self.log(f"  shot -> {path}")
        return path

    # -- 地图状态 ---------------------------------------------------------
    def on_map(self, img: Image.Image | None = None) -> bool:
        """是否在世界地图：底栏「武将/同盟 + 征战」可见。

        竖屏底栏 OCR 会抖动（「征战」常被读成「证战」），所以用三级判定：
        1) 武将/同盟 + 征战（证战）同时命中 → 地图；
        2) 只命中 武将/同盟，且不是城内视角、没有「返回」按钮 → 地图；
        3) 其余一律不算地图（宁可多走一次 ensure_map，也不要误判卡死）。
        """
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, config.MAP_BOTTOM_SLOT), 2.0)
        flat = text.replace(" ", "")
        has_left = ("武将" in flat) or ("同盟" in flat)
        has_right = ("征战" in flat) or ("证战" in flat)
        if has_left and has_right:
            # 底栏可见还不够：地块详情浮层会盖住左半底栏（邮件点不开），
            # 必须确认右侧没有「行军/调动/驻守…」动作列。
            return not self.detail_panel_visible(img)
        if not has_left:
            return False
        if self.back_button_visible(img):
            return False               # 二级页：底栏只是被遮住了一部分
        return not self.in_city_view(img)

    def chat_panel_visible(self, img: Image.Image | None = None) -> bool:
        """是否误触进了「好友 / 聊天」面板（它没有关闭按钮，必须靠 ‹ 收起）。"""
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, CHAT_TITLE_BOX), 2.5)
        return "好友" in text and any(k in text for k in CHAT_HINTS)

    def close_chat_panel(self, tries: int = 2) -> bool:
        for _ in range(max(1, tries)):
            if not self.chat_panel_visible():
                return True
            self.log("  ! 误触「好友/聊天」面板，点右侧 ‹ 收起")
            self.adb.tap(*CHAT_CLOSE)
            time.sleep(1.2)
        ok = not self.chat_panel_visible()
        if not ok:
            self.log("  × 聊天面板收不起来（该面板无关闭按钮），需要人工处理")
        return ok

    def mobilize_panel_visible(self, img: Image.Image | None = None) -> bool:
        """「调动/出征」面板是否还开着（会盖住底栏、吃掉邮件图标的点击）。"""
        img = img or self.adb.screencap()
        k = img.width / float(config.SCREEN[0])
        box = (max(0, round(MOBILIZE_PANEL_HINT_BOX[0] * k)),
               max(0, round(MOBILIZE_PANEL_HINT_BOX[1] * k)),
               min(img.width, round(MOBILIZE_PANEL_HINT_BOX[2] * k)),
               min(img.height, round(MOBILIZE_PANEL_HINT_BOX[3] * k)))
        text = "".join(s.text for s in ocrengine.ocr(vision.crop(img, box), 2.0)
                       ).replace(" ", "")
        if any(h in text for h in MOBILIZE_PANEL_HINTS):
            return True
        return all(h in text for h in MOBILIZE_PANEL_HINTS_PAIR)

    def close_mobilize_panel(self, wait: float = 1.3, tries: int = 2) -> bool:
        """点面板右上角收起 ✕ 关掉「调动/出征」面板。"""
        for i in range(max(1, tries)):
            self.log(f"  ! 检测到「调动/出征」面板残留 → 点收起 ✕@{MOBILIZE_CLOSE}")
            self.adb.tap(*MOBILIZE_CLOSE)
            time.sleep(wait)
            if not self.mobilize_panel_visible():
                return True
        return False

    def detail_panel_visible(self, img: Image.Image | None = None) -> bool:
        """是否停在地块/据点详情浮层（右侧动作列出现行军/调动/驻守…，底栏被盖住）。"""
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, ACTION_BOX), 2.0).replace(" ", "")
        hits = sum(1 for h in DETAIL_PANEL_HINTS if h in text)
        return hits >= 2

    def home_button_point(self, want: str = "") -> tuple[int, int] | None:
        """在左下按钮带里找「回城 / 出城」的真实文字位置（OCR 优先，失败返回 None）。"""
        box = config.HOME_BTN_BOX
        img = self.adb.screencap()
        for s in ocrengine.ocr(vision.crop(img, box), 2.0):
            t = s.text.replace(" ", "")
            if (want and want in t) or (not want and ("回城" in t or "出城" in t)):
                return (int(box[0] + s.cx), int(box[1] + s.cy))
        return None

    def in_city_view(self, img: Image.Image | None = None) -> bool:
        """是否在「城内视角」。

        三重判定，任一命中即认为是城内（回城之后偶尔只有按钮文字对得上）：
        1) 城内建筑文字：君王殿 / 研究所 / 亲卫营 / 城墙 / 民居 …；
        2) 底部左按钮是「出城」（地图上同位置是「回城」）；
        3) 右下「亲卫营」标签可见。
        """
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, CITY_VIEW_BOX), 2.0).replace(" ", "")
        if any(h in text for h in CITY_VIEW_HINTS):
            return True
        # 左下按钮文字兜底：城内是「出城」，地图是「回城」
        if "出城" in self.home_button_text(img):
            return True
        return False

    def leave_city(self, tries: int = 2, wait: float = 2.5) -> bool:
        """城内点左下「出城」回世界地图：先按 OCR 找到的「出城」文字点，再退回固定候选点。"""
        box = config.HOME_BTN_BOX
        for i in range(max(1, tries)):
            points: list[tuple[int, int]] = []
            found = self.home_button_point("出城")
            if found:
                points.append(found)
            points += [p for p in CITY_EXIT_POINTS if p not in points]
            for point in points:
                self.adb.tap(*point)
                time.sleep(wait)
                if self.on_map():
                    self.log("  ✓ 已出城回到世界地图")
                    return True
            self.log(f"  · 第{i + 1}次点「出城」没生效，重试")
        return self.on_map()

    def dismiss_modal(self, img: Image.Image | None = None) -> bool:
        """点掉「未携带满战法，是否继续出征？」这类全屏提示弹窗。"""
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, MODAL_BOX), 2.0)
        flat = text.replace(" ", "")
        if not any(h in flat for h in MODAL_HINTS):
            return False
        # 注意：正文里也有「是否继续出征?」，必须挑最短的那一段（按钮文字）
        point = MODAL_CONTINUE_POINT
        best = None
        for s in ocrengine.ocr(vision.crop(img, MODAL_BOX), 2.0):
            t = s.text.replace(" ", "").strip()
            if MODAL_CONTINUE_HINT not in t:
                continue
            if best is None or len(t) < len(best[0]):
                best = (t, (int(MODAL_BOX[0] + s.cx), int(MODAL_BOX[1] + s.cy)))
        if best is not None and len(best[0]) <= 6:
            point = best[1]
        self.log(f"  ! 弹窗「{flat[:18]}」→ 点「继续出征」@{point}")
        self.adb.tap(*point)
        time.sleep(1.5)
        return True

    def exit_dialog_visible(self) -> bool:
        """是否弹出「确认退出游戏」（com.gsc.exit.ExitActivity）。"""
        try:
            out = self.adb.shell("dumpsys", "window", "windows", timeout=15)
        except Exception:
            return False
        return "ExitActivity" in out

    def cancel_exit_dialog(self) -> bool:
        """点「取消」关掉「确认退出游戏」，绝不点「确定」。"""
        point = EXIT_CANCEL_POINT
        try:
            self.adb.shell("uiautomator", "dump", "/sdcard/ui_exit.xml", timeout=25)
            xml = self.adb.shell("cat", "/sdcard/ui_exit.xml", timeout=15)
            m = re.search(
                rf'{EXIT_CANCEL_ID}[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                xml)
            if m:
                x1, y1, x2, y2 = (int(v) for v in m.groups())
                point = ((x1 + x2) // 2, (y1 + y2) // 2)
        except Exception:               # noqa: BLE001
            pass
        self.log(f"  ! 检测到「确认退出游戏」弹窗 → 点「取消」@{point}")
        self.adb.tap(*point)
        time.sleep(1.5)
        return not self.exit_dialog_visible()

    # -- 底部按钮带 / 二级页返回 -------------------------------------------
    def back_button_text(self, img: Image.Image | None = None) -> str:
        """读「◀ 返回」按钮带（只有二级页才有；地图/城内同一位置是别的按钮）。"""
        img = img or self.adb.screencap()
        return ocrengine.ocr_text(
            vision.crop(img, config.BACK_BTN_BOX), 2.0).replace(" ", "")

    def home_button_text(self, img: Image.Image | None = None) -> str:
        """读左下「回城 / 出城」按钮带（地图=回城，城内=出城，同一坐标）。"""
        img = img or self.adb.screencap()
        return ocrengine.ocr_text(
            vision.crop(img, config.HOME_BTN_BOX), 2.0).replace(" ", "")

    def back_button_visible(self, img: Image.Image | None = None) -> bool:
        """是否真的存在「◀ 返回」按钮（只有这时才允许点 MAIL_BACK）。"""
        return "返回" in self.back_button_text(img)

    def press_back(self, wait: float = 1.2) -> None:
        """按 Android BACK；若弹出「确认退出游戏」立刻点「取消」，绝不点确定。"""
        self.adb.key(4)
        time.sleep(wait)
        if self.exit_dialog_visible():
            self.cancel_exit_dialog()

    def page_title(self, img: Image.Image | None = None) -> str:
        """读页面左上标题（竖屏顶部 110px）。"""
        img = img or self.adb.screencap()
        box = config._p((60, 10, 700, 70), (0, 0, 900, 115))
        return ocrengine.ocr_text(vision.crop(img, box), 2.0).replace(" ", "")

    def wrong_page_visible(self, img: Image.Image | None = None) -> str:
        """是否误入了二级页（武将库 / 亲卫部队 / 战报 / 同盟 / 城池 …），返回命中的标题。"""
        title = self.page_title(img)
        for hint in WRONG_PAGE_HINTS:
            if hint in title:
                return hint
        return ""

    def recover_to_city(self, tries: int = 4, wait: float = 1.4) -> bool:
        """从任意二级页（含误触打开的「武将库」「亲卫部队」）切回城内视角。

        只处理「二级页 → 城内」这一跳；如果当前其实在世界地图上，
        直接返回 False（回城是 `ensure_map` + 「回城」按钮的事，别在这里乱点）。
        """
        for _ in range(max(1, tries)):
            img = self.adb.screencap()
            if self.in_city_view(img):
                return True
            if self.exit_dialog_visible():
                self.cancel_exit_dialog()
                continue
            if self.on_map(img) and not self.back_button_visible(img):
                self.log("  · 当前在世界地图（不是城内），跳过二级页恢复")
                return False
            title = self.wrong_page_visible(img)
            if title or self.back_button_visible(img):
                self.log(f"  · 误入「{title or '二级页'}」→ 点底部「返回」切回")
                self.adb.tap(*config.MAIL_BACK)
                time.sleep(wait)
                continue
            self.press_back(wait)
        ok = self.in_city_view()
        self.log("  ✓ 已切回城内视角" if ok
                 else "  ! 还没切回城内视角（下一轮再试）")
        return ok

    def ensure_map(self, tries: int = 5, wait: float = 1.2) -> bool:
        """关掉所有叠层面板回到地图（左上返回箭头；在城里则先「出城」）。"""
        for i in range(tries):
            img = self.adb.screencap()
            # 调动/出征面板残留时，地图上半仍可见、底栏却被盖住，
            # 必须先收起面板，否则后续点邮件图标全部无效。
            if self.mobilize_panel_visible(img):
                self.close_mobilize_panel()
                continue
            if self.on_map(img):
                return True
            if self.exit_dialog_visible():
                self.cancel_exit_dialog()
                continue
            # 「是否跳转到坐标」弹框会吃掉所有点击（连邮件图标都点不开），
            # 必须优先点「确定」把它关掉，否则后面全是无效点击。
            if self.jump_dialog_visible(img):
                self.confirm_jump(1.6, tries=2)
                continue
            if self.dismiss_modal(img):
                continue
            if self.chat_panel_visible(img):
                self.close_chat_panel()
                continue
            if self.detail_panel_visible(img):
                # 地块/据点详情浮层：右侧「行军/调动/驻守…」动作列会盖住底栏，
                # 邮件图标点不开（历史事故的起点），先取消选中。
                # 实测「点空白点」(760,240) 会命中顶部活动图标、BACK 会弹退出确认，
                # 只有小拖拽能稳定取消选中（不触发新地块、不动镜头）。
                self.log("  ! 停在地块详情浮层 → 小拖拽取消选中")
                self.adb.swipe(450, 700, 520, 700, 200)
                time.sleep(wait)
                continue
            if self.in_city_view(img):
                if self.leave_city(tries=1):
                    return True
                continue
            # 只有真的看到「返回」按钮才点 MAIL_BACK；
            # 盲点会在地图上命中「武将」按钮，把界面推进「武将库」（历史事故）。
            if self.back_button_visible(img):
                self.adb.tap(*config.MAIL_BACK)
                time.sleep(wait)
                continue
            self.press_back(wait)
        return self.on_map()

    # -- 跳转确认弹窗 -----------------------------------------------------
    def jump_dialog_visible(self, img: Image.Image | None = None) -> bool:
        img = img or self.adb.screencap()
        scale = 1.6 if config.PORTRAIT else 2.0
        text = ocrengine.ocr_text(vision.crop(img, config.JUMP_DIALOG_BOX), scale)
        return "跳转到坐标" in text or "是否跳转" in text

    def _jump_button_point(self, img: Image.Image, label: str) -> tuple[int, int]:
        """在跳转弹窗里按文字定位按钮中心；OCR 找不到才回退固定坐标。"""
        box = config.JUMP_DIALOG_BOX
        fallback = config.CONFIRM_JUMP if label == "确定" else config.CANCEL_JUMP
        scale = 1.6 if config.PORTRAIT else 2.0
        for s in ocrengine.ocr(vision.crop(img, box), scale):
            if s.text.replace(" ", "").strip() == label:
                return (int(box[0] + s.cx), int(box[1] + s.cy))
        return fallback

    def confirm_jump(self, wait: float = 2.5, tries: int = 2) -> bool:
        """点「确定」完成跳转；点歪/弹框残留就重试（弹框会吃掉所有后续点击）。"""
        last = config.CONFIRM_JUMP
        for i in range(max(1, tries)):
            img = self.adb.screencap()
            last = self._jump_button_point(img, "确定")
            self.adb.tap(*last)
            time.sleep(wait)
            if not self.jump_dialog_visible():
                return True
            self.log(f"  ! 跳转弹框仍在（点 {last}），重试 {i + 1}/{tries}")
        return not self.jump_dialog_visible()

    def cancel_jump(self, wait: float = 1.0) -> None:
        img = self.adb.screencap()
        self.adb.tap(*self._jump_button_point(img, "取消"))
        time.sleep(wait)

    # -- 定位面板 ---------------------------------------------------------
    def locate_visible(self, img: Image.Image | None = None) -> bool:
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, config.LOCATE_TAB_BOX), 2.0)
        return ("建筑" in text) and ("收藏" in text or "同盟" in text)

    def open_locate(self, wait: float = 1.2) -> None:
        """展开定位面板（幂等：已展开则不再点击，避免开关反了）。"""
        if self.locate_visible():
            return
        self.adb.tap(*config.MAP_LOCATE_ICON)
        time.sleep(wait)

    def close_locate(self, wait: float = 1.0) -> None:
        if self.locate_visible():
            self.adb.tap(*config.MAP_LOCATE_ICON)
            time.sleep(wait)

    def select_locate_tab(self, name: str, wait: float = 1.2) -> None:
        x = config.LOCATE_TABS.get(name)
        if x is None:
            raise KeyError(f"未知定位页签: {name}")
        self.adb.tap(x, config.LOCATE_TAB_Y)
        time.sleep(wait)

    def _parse_locates(self, img: Image.Image) -> list[CampItem]:
        box = config.LOCATE_PANEL_BOX
        rows = ocrengine.ocr_rows(vision.crop(img, box), config.LOCATE_OCR_SCALE)
        flat = []
        for row in rows:
            text = "".join(s.text for s in row).strip()
            if text:
                flat.append((box[1] + row[0].cy, text))

        items: list[CampItem] = []
        for i, (y, text) in enumerate(flat):
            m = COORD_RE.search(text)
            if not m:
                continue
            coord = (int(m.group(1)), int(m.group(2)))
            # 名称：坐标行上方 15~75px 内、非噪声的最近一行
            name = ""
            for py, ptext in reversed(flat[:i]):
                if not (15 <= y - py <= 75):
                    continue
                if any(n in ptext for n in NOISE) or COORD_RE.search(ptext):
                    continue
                name = ptext
                break
            if not name:
                name = COORD_RE.sub("", text).strip()
            items.append(CampItem(name=name, coord=coord,
                                  point=(config.LOCATE_ITEM_X, int(y))))
        return items

    def scroll_panel_to_top(self, times: int = 4, wait: float = 0.6) -> None:
        """定位列表同样会记住滚动位置，扫描/点击前先回顶。"""
        for _ in range(times):
            self.adb.swipe(*config.LOCATE_SCROLL_DOWN, 380)
            time.sleep(wait)

    def scan_camps(self, keywords: tuple[str, ...] = ("大营",),
                   scrolls: int = 3) -> list[CampItem]:
        found: dict[tuple[str, tuple[int, int]], CampItem] = {}
        self.scroll_panel_to_top()
        for i in range(scrolls + 1):
            for it in self._parse_locates(self.adb.screencap()):
                if any(k in it.name for k in keywords):
                    found[(it.name, it.coord)] = it
            if i < scrolls:
                self.adb.swipe(*config.LOCATE_SCROLL, 420)
                time.sleep(0.9)
        return list(found.values())

    def nearest_camp(self, camps: list[CampItem],
                     target: tuple[int, int]) -> CampItem | None:
        if not camps:
            return None
        return min(camps, key=lambda c: c.dist(target))

    def select_camp(self, camp: CampItem, max_pages: int = 8) -> bool:
        """按名称+坐标重新在列表里找到该条目并点击（位置会随滚动变化）。"""
        self.scroll_panel_to_top()
        for _ in range(max_pages):
            for it in self._parse_locates(self.adb.screencap()):
                # 名称可能被 OCR 抖动，坐标才是稳定键
                if it.coord == camp.coord:
                    self.adb.tap(*it.point)
                    time.sleep(2.0)
                    return True
            self.adb.swipe(*config.LOCATE_SCROLL, 420)
            time.sleep(0.9)
        return False

    # -- 地块操作按钮 -----------------------------------------------------
    # -- 地图标签（不经定位面板，直接在地图上认标签） ----------------------
    def map_labels(self, img: Image.Image | None = None):
        img = img or self.adb.screencap()
        return ocrengine.ocr_tiled(img, config.MAP_BOX,
                                   config.MAP_TILE, config.MAP_TILE_OVERLAP,
                                   2.0)

    def find_coord_anchor(self, coord: tuple[int, int],
                          img: Image.Image | None = None):
        """在地图上找「都阳-都阳-都阳(714,773)」这类地块坐标标签，返回其位置。"""
        target = f"{coord[0]},{coord[1]}"
        for s in self.map_labels(img):
            t = s.text.replace(" ", "")
            for pat in COORD_RE.finditer(t):
                if f"{pat.group(1)},{pat.group(2)}" == target:
                    return (int(s.cx), int(s.cy))
        return None

    def find_camps_on_map(self, anchor: tuple[int, int],
                          img: Image.Image | None = None):
        """地图上所有含「大营」的标签，按与 anchor 的距离排序。"""
        out = []
        for s in self.map_labels(img):
            t = s.text.replace(" ", "")
            if "大营" not in t:
                continue
            point = (int(s.cx), int(s.cy))
            out.append((t, point, math.dist(point, anchor)))
        out.sort(key=lambda c: c[2])
        return out

    @staticmethod
    def _plate_ratio(frame: Image.Image,
                     strip: tuple[int, int, int, int]) -> float:
        """strip 内「深色按钮底板」像素占比（用来剔除地图上漂浮的聊天文字）。

        按钮文字一定压在深蓝黑色半透明圆角底板上（max<115 且低饱和）；
        世界聊天气泡是直接浮在地形上的白字，背景是亮色草地/城墙，占比很低。
        """
        x1, y1, x2, y2 = [int(v) for v in strip]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.width, x2), min(frame.height, y2)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return 0.0
        a = np.asarray(frame.crop((x1, y1, x2, y2)).convert("RGB"))
        mx = a.max(axis=2).astype(np.int16)
        mn = a.min(axis=2).astype(np.int16)
        dark = (mx < 115) & ((mx - mn) < 70)
        return float(dark.mean())

    def find_all_buttons(self, box: tuple[int, int, int, int] = ACTION_BOX,
                         img: Image.Image | None = None,
                         attempts: int = 2,
                         plate_min: float = 0.45) -> list[tuple[str, tuple[int, int]]]:
        """OCR 右侧竖排按钮列，返回 [(文字, 屏幕坐标)]，按 y 从上到下。

        面板动画/遮罩会让个别按钮漏检，因此默认连拍两次取并集；
        另外按「是否有深色底板」过滤掉地图上漂浮的聊天文字
        （实测聊天气泡会横穿按钮列，把「调动」挤掉）。
        """
        merged: dict[str, tuple[int, int]] = {}
        for i in range(attempts):
            frame = img if img is not None else self.adb.screencap()
            for s in ocrengine.ocr(vision.crop(frame, box), 2.5):
                t = s.text.replace(" ", "").strip()
                if not t or t in merged or len(t) > 6:
                    continue
                cx, cy = int(box[0] + s.cx), int(box[1] + s.cy)
                strip = (cx - 95, cy - 18, cx + 95, cy + 18)
                if self._plate_ratio(frame, strip) < plate_min:
                    continue                    # 漂浮聊天文字，不是按钮
                merged[t] = (cx, cy)
            if img is not None:
                break
            time.sleep(0.5)
        return sorted(merged.items(), key=lambda b: b[1][1])

    def find_button(self, names: tuple[str, ...],
                    box: tuple[int, int, int, int] = ACTION_BOX,
                    img: Image.Image | None = None):
        """在操作按钮列里找指定文字，返回 (命中文字, 屏幕坐标) 或 None。"""
        for t, point in self.find_all_buttons(box, img):
            for n in names:
                if n in t:
                    return t, point
        return None
