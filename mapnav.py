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

# 跳转后地块操作按钮的搜索区域（全分辨率，窄条更利于 OCR 检测）
ACTION_BOX = config.ACTION_BOX

# 「好友 / 聊天」面板（误触左下「军报」或顶部箭头会打开，且没有关闭按钮）
CHAT_TITLE_BOX = (20, 20, 420, 200)
CHAT_CLOSE = (1408, 456)        # 面板右侧「‹」收起箭头（实测可关掉）
CHAT_HINTS = ("联系人", "群聊", "屏蔽名单")

# 城内视角（回城后出现 君王殿/研究所/亲卫营，左下按钮变成「出城」）
# 地图左下「回城」和城内左下「出城」是同一个位置，实测图标中心 ≈ (60,772)
CITY_VIEW_BOX = (0, 40, 900, 430)
CITY_VIEW_HINTS = ("君王殿", "研究所", "亲卫营", "出城", "城内", "城墙", "民居")
CITY_EXIT_POINTS = ((60, 772), (70, 790), (50, 752))

# 全屏二级弹窗（实测「部队未携带满战法，是否继续出征？」）会吃掉所有点击：
# 这时同盟页打不开、右侧按钮列 OCR 为空，必须先把弹窗点掉
MODAL_BOX = (420, 290, 1200, 610)
MODAL_HINTS = ("继续出征", "是否继续出征", "未携带满战法")
MODAL_CONTINUE_HINT = "继续出征"
MODAL_CONTINUE_POINT = (610, 543)


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
        """底栏「武将/同盟/征战」可见即认为在地图界面。"""
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, (600, 780, 1460, 890)), 2.0)
        return ("武将" in text or "同盟" in text) and "征战" in text.replace("证战", "征战")

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

    def in_city_view(self, img: Image.Image | None = None) -> bool:
        """是否在「城内视角」（回城之后的样子：君王殿/研究所/亲卫营）。"""
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, CITY_VIEW_BOX), 2.0)
        return any(h in text for h in CITY_VIEW_HINTS)

    def leave_city(self, tries: int = 2, wait: float = 2.5) -> bool:
        """城内点左下「出城」回世界地图。"""
        for i in range(max(1, tries)):
            for point in CITY_EXIT_POINTS:
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

    def ensure_map(self, tries: int = 5, wait: float = 1.2) -> bool:
        """关掉所有叠层面板回到地图（左上返回箭头；在城里则先「出城」）。"""
        for i in range(tries):
            img = self.adb.screencap()
            if self.on_map(img):
                return True
            if self.dismiss_modal(img):
                continue
            if self.chat_panel_visible(img):
                self.close_chat_panel()
                continue
            if self.in_city_view(img):
                if self.leave_city(tries=1):
                    return True
                continue
            self.adb.tap(*config.MAIL_BACK)
            time.sleep(wait)
        return self.on_map()

    # -- 跳转确认弹窗 -----------------------------------------------------
    def jump_dialog_visible(self, img: Image.Image | None = None) -> bool:
        img = img or self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, config.JUMP_DIALOG_BOX), 2.0)
        return "跳转到坐标" in text or "是否跳转" in text

    def confirm_jump(self, wait: float = 2.5) -> None:
        self.adb.tap(*config.CONFIRM_JUMP)
        time.sleep(wait)

    def cancel_jump(self, wait: float = 1.0) -> None:
        self.adb.tap(*config.CANCEL_JUMP)
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
        rows = ocrengine.ocr_rows(vision.crop(img, box), 3.0)
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
                                  point=(1380, int(y))))
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
