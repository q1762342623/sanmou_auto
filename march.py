"""向「战报热点」行军 / 攻城：跳一次坐标，然后把所有部队一次性派出去。

用户口径（2026-11-23）：
- 找到离热点最近的己方据点、完成「调动」并等全部到达后，再到热点坐标触发战斗；
- 按钮优先级 **攻城 > 行军**，**绝不用「攻占」**（攻占会触发自动铺路，偏离目标）；
- 如果热点地块的「行军」是灰色（无法行军），就在附近逐个试相邻地块，
  找到能点「行军」的位置再出征；
- **不需要反复从大地图定位坐标**：沙盘/坐标跳一次，之后所有部队都在同一块地
  的出征面板里连续派出（面板被收起时才重新选中这块地，不重新定位）；
- 行军时不重新判断兵力（兵力在「调动前」已经检查/补兵过）。

实现：
- `SandboxJump.goto()` 跳到热点并点选中心地块；
- OCR 右侧动作列，按 攻城 / 行军 优先级找按钮，并做一次「按钮是否变灰」判定；
- 找不到可用按钮时，围绕屏幕中心逐个点相邻格，找到可用的「行军」再派兵；
- 派兵复用 `Mobilizer.dispatch_one()`（同款出征面板 + 金色按钮中心点击）。
"""
from __future__ import annotations

import re
import time

from PIL import Image

from . import config, fastwait, ocrengine, vision
from .adb import Adb
from .mapnav import MapNav
from .mobilize import (MobilizeResult, Mobilizer, TARGET_INVALID_BOX,
                       TARGET_INVALID_HINTS)
from .sandbox import SandboxJump

# 只允许这两个动作；「攻占/占领/攻击/攻打/集结」一律不点
MARCH_BUTTON_PRIORITY = ("攻城", "行军")
MARCH_BUTTON_EXCLUDE = ("攻占", "占领", "攻击", "攻打", "集结", "驻军",
                        "总览", "整编", "奔袭", "结义")
# 地图中心（跳转后目标格所在位置）：与沙盘 P_CENTER 一致
MAP_CENTER = config._p((800, 430), (450, 800))
# 附近格子的尝试顺序（相对屏幕中心的像素偏移，base 分辨率）
NEAR_OFFSETS = (
    (0, 0),
    (0, -70), (70, 0), (-70, 0), (0, 70),
    (70, -70), (-70, -70), (70, 70), (-70, 70),
    (0, -130), (130, 0), (-130, 0), (0, 130),
    (130, -130), (-130, -130), (130, 130), (-130, 130),
)
# 按钮文字「亮/灰」判定：白字启用时 max(r,g,b) 通常 ≥200；灰字约 120~160
ENABLED_MAX_LUMA = 190
ENABLED_MIN_PIXELS = 18
CIRCLED = re.compile(r"[^\u4e00-\u9fa5]")


def _clean(text: str) -> str:
    return CIRCLED.sub("", (text or "").replace(" ", ""))


class Marcher:
    """热点行军/攻城：选地、找按钮、一次性派兵。"""

    def __init__(self, adb: Adb | None = None, log=print,
                 nav: MapNav | None = None):
        self.adb = adb or Adb()
        self.nav = nav or MapNav(self.adb, log=log)
        self.log = log
        self.target: tuple[int, int] | None = None
        self.selected_tile: tuple[int, int] | None = None
        self.last_action = ""
        self._chosen_action = ""
        self._chosen_tile_point: tuple[int, int] | None = None
        self._prefer_march = False

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _scaled_box(img: Image.Image, box):
        k = img.width / float(config.SCREEN[0])
        return (max(0, round(box[0] * k)), max(0, round(box[1] * k)),
                min(img.width, round(box[2] * k)),
                min(img.height, round(box[3] * k)))

    @staticmethod
    def _is_enabled(img: Image.Image, point: tuple[int, int]) -> bool:
        """按钮文字中心附近是否足够亮（白字=可用，灰字=禁用）。"""
        k = img.width / float(config.SCREEN[0])
        cx, cy = round(point[0] * k), round(point[1] * k)
        half_w, half_h = round(64 * k), round(20 * k)
        box = (max(0, cx - half_w), max(0, cy - half_h),
               min(img.width, cx + half_w), min(img.height, cy + half_h))
        if box[2] - box[0] < 6 or box[3] - box[1] < 6:
            return False
        px = img.load()
        bright = 0
        for y in range(box[1], box[3], 2):
            for x in range(box[0], box[2], 2):
                r, g, b = px[x, y]
                if max(r, g, b) >= ENABLED_MAX_LUMA:
                    bright += 1
        return bright >= ENABLED_MIN_PIXELS

    @staticmethod
    def _canonical(text: str) -> str:
        """OCR 容忍：'沂军'→行军、'攻域'→攻城；攻占/攻击等一律不接受。"""
        clean = _clean(text)
        if not clean or any(bad in clean for bad in MARCH_BUTTON_EXCLUDE):
            return ""
        if "攻城" in clean or (
                len(clean) <= 3 and "攻" in clean
                and "占" not in clean and "击" not in clean and "打" not in clean):
            return "攻城"
        if "行军" in clean or "军" in clean and len(clean) <= 3:
            return "行军"
        return ""

    def action_buttons(self, img: Image.Image | None = None) -> list[dict]:
        """OCR 右侧动作列，返回 `[{text,point,enabled,march}]`。"""
        img = img or self.adb.screencap()
        out: list[dict] = []
        for text, point in self.nav.find_all_buttons(img=img, attempts=1):
            if point[0] < config.ACTION_BUTTON_MIN_X:
                continue                    # 地图漂浮标签，不是右侧动作按钮
            canonical = self._canonical(text)
            if not canonical:
                continue
            out.append({
                "text": canonical,
                "raw": _clean(text),
                "point": point,
                "enabled": self._is_enabled(img, point),
            })
        return out

    def target_invalid(self, img: Image.Image | None = None) -> bool:
        """攻城面板顶部「被攻击的建筑没有连接地 / 敌情难测」= 该目标不可打。"""
        img = img or self.adb.screencap()
        box = self._scaled_box(img, TARGET_INVALID_BOX)
        text = ocrengine.ocr_text(vision.crop(img, box), 2.0).replace(" ", "")
        return any(hint in text for hint in TARGET_INVALID_HINTS)

    def pick_action(self, img: Image.Image | None = None):
        """按 攻城 > 行军 取第一个可用按钮；返回 (文字, 点) 或 (None,None)。

        OCR 偶尔被聊天气泡/面板动画挡掉，连续读 3 次（每次间隔 0.8s）。
        """
        for attempt in range(3):
            buttons = self.action_buttons(img)
            if buttons:
                self.log("  动作按钮: " + ", ".join(
                    f"{b['text']}{'' if b['enabled'] else '(灰)'}"
                    for b in buttons))
            for wanted in MARCH_BUTTON_PRIORITY:
                for b in buttons:
                    if wanted in b["text"] and b["enabled"]:
                        return b["text"], b["point"]
            if img is not None:
                break
            time.sleep(0.8)
        return None, None

    # ------------------------------------------------------------ 选地
    def select_coord(self, coord: tuple[int, int]) -> bool:
        """沙盘跳到热点并点选中心地块（只跳一次）。"""
        self.target = (int(coord[0]), int(coord[1]))
        try:
            sb = SandboxJump(self.adb, log=self.log)
            if not sb.goto(self.target, tries=4):
                self.log(f"  ! 沙盘没跳到热点 {self.target}，退出沙盘页")
                sb.close()
                self.nav.ensure_map()
                return False
        except Exception as error:          # noqa: BLE001
            self.log(f"  ! 沙盘跳热点异常：{error!r}")
            return False
        fastwait.wait_settle(self.adb, box=config.ACTION_BOX,
                             timeout=2.4, min_wait=0.4)
        self.selected_tile = self.target
        return True

    def _tap_tile(self, point: tuple[int, int]) -> None:
        k = self.adb.screencap().width / float(config.SCREEN[0])
        self.adb.tap(round(point[0] * k), round(point[1] * k))
        fastwait.wait_settle(self.adb, box=config.ACTION_BOX,
                             timeout=2.0, min_wait=0.35)

    def find_marchable_tile(self) -> tuple[str, tuple[int, int]]:
        """热点及附近格子中找能点「攻城/行军」的位置，返回 (动作, 点)。

        找不到时返回 `("", (0, 0))`，调用方用「动作文字为空」判失败。
        """
        name, point = self.pick_action()
        if name:
            self.log(f"  ✓ 热点 {self.target} 可直接「{name}」@{point}")
            return name, point
        self.log(f"  · 热点 {self.target} 的攻城/行军不可用（灰），试相邻地块")
        base = MAP_CENTER
        for i, (dx, dy) in enumerate(NEAR_OFFSETS[1:], start=1):
            p = (base[0] + dx, base[1] + dy)
            self.log(f"    · 相邻候选#{i} 偏移 ({dx},{dy})")
            self._tap_tile(p)
            name, point = self.pick_action()
            if name:
                self.selected_tile = None      # 未知真实坐标，仅记录屏幕点
                self.log(f"  ✓ 相邻地块可「{name}」@{point}（偏移 {dx},{dy}）")
                return name, point
        self.log("  × 热点与相邻地块都没有可用的攻城/行军按钮")
        return "", (0, 0)

    # ------------------------------------------------------------ 出征面板
    def click_action(self, name: str, point: tuple[int, int]) -> bool:
        """OCR 命中后点按钮文字中心；确认出征面板出现。"""
        self.log(f"== 点击「{name}」@{point}")
        self.adb.tap(*point)
        fastwait.wait_settle(self.adb, box=config.ACTION_BOX,
                             timeout=2.4, min_wait=0.45)
        # 面板打开判据复用 Mobilizer（金色出征按钮 / 提示行 / 底部耗时）
        mob = Mobilizer(self.adb, log=self.log)
        if mob.panel_open():
            return True
        self.log("  ! 点完动作按钮没看到出征面板")
        return False

    def _sorted_actions(self) -> list[dict]:
        buttons = [b for b in self.action_buttons() if b["enabled"]]
        order = (("行军", "攻城") if self._prefer_march
                 else MARCH_BUTTON_PRIORITY)
        buttons.sort(key=lambda b: order.index(b["text"])
                     if b["text"] in order else 99)
        return buttons

    def find_specific(self, name: str):
        for b in self.action_buttons():
            if b["text"] == name and b["enabled"]:
                return b["text"], b["point"]
        return "", (0, 0)

    def iter_action_candidates(self):
        """按「当前热点 → 相邻格」顺序产出可用动作（攻城优先，可切换为行军优先）。"""
        self._chosen_tile_point = MAP_CENTER
        for b in self._sorted_actions():
            yield b
        base = MAP_CENTER
        for i, (dx, dy) in enumerate(NEAR_OFFSETS[1:], start=1):
            p = (base[0] + dx, base[1] + dy)
            self.log(f"    · 相邻候选#{i} 偏移 ({dx},{dy})")
            self._tap_tile(p)
            self._chosen_tile_point = p
            for b in self._sorted_actions():
                yield b

    def reselect_tile(self) -> bool:
        """面板被收起时重新选中同一块地并点上次成功的动作（不重新定位坐标）。"""
        # 出征/调动成功后动作列通常还在：先直接找上次成功的动作，不要先点地块
        # （对已选中地块再点一下会取消选中，反而把动作列弄没）。
        if self._chosen_action:
            name, point = self.find_specific(self._chosen_action)
            if name:
                return self.click_action(name, point)
            name, point = self.pick_action()
            if name:
                return self.click_action(name, point)
        # 动作列不在：重选「真正被派兵的那块地」（相邻格不是 MAP_CENTER）
        if self._chosen_tile_point is not None:
            self._tap_tile(self._chosen_tile_point)
        elif self.target is not None:
            self._tap_tile(MAP_CENTER)
        if self._chosen_action:
            name, point = self.find_specific(self._chosen_action)
            if name:
                return self.click_action(name, point)
        name, point = self.pick_action()
        if not name:
            return False
        return self.click_action(name, point)

    def dispatch_all(self, indexes: tuple[int, ...],
                     wait: float = 0.9) -> dict:
        """把 indexes 里的部队一次性派往热点/相邻地块。

        逐个候选尝试：攻城（优先）→ 行军；攻城面板提示「没有连接地/敌情难测」
        时关掉面板换下一个候选，并把后续轮次切换为「行军优先」。
        """
        result: dict = {"ok": False, "action": self.last_action,
                        "troops": [], "failed": [], "target": self.target,
                        "tile": self.selected_tile, "tries": []}
        if not indexes:
            result["reason"] = "no-troop"
            return result
        for cand in self.iter_action_candidates():
            name, point = cand["text"], cand["point"]
            result["tries"].append({"action": name, "point": list(point)})
            if not self.click_action(name, point):
                continue
            if self.target_invalid():
                self.log(f"  × 「{name}」目标不可打（没有连接地/敌情难测），换下一个")
                self._prefer_march = True
                try:
                    Mobilizer(self.adb, log=self.log).close()
                except Exception:           # noqa: BLE001
                    pass
                continue
            self.last_action = name
            self._chosen_action = name
            result["action"] = name
            mr = MobilizeResult()
            mob = Mobilizer(self.adb, log=self.log,
                            reselect=self.reselect_tile)
            try:
                for idx in indexes:
                    if not mob.panel_open():
                        self.log(f"  · 出征面板已收起，重新选中地块后再派部队{idx}")
                        if not self.reselect_tile():
                            self.log("  × 无法重新打开出征面板，停止派兵")
                            break
                    troop = mob.dispatch_one(int(idx), wait=wait)
                    if troop is None:
                        mr.troops.append(type("T", (), {
                            "index": int(idx), "status": "",
                            "dispatched": False}))
                        result["failed"].append(int(idx))
                        continue
                    mr.troops.append(troop)
                    if not troop.dispatched:
                        result["failed"].append(int(idx))
            except Exception as error:      # noqa: BLE001
                self.log(f"  ! 行军派兵异常：{error!r}")
                result["error"] = repr(error)
            finally:
                try:
                    mob.close()
                except Exception:           # noqa: BLE001
                    pass
            done = [t for t in mr.troops if getattr(t, "dispatched", False)]
            mr.camp = (f"hotspot({self.target[0]},{self.target[1]})"
                       if self.target else "")
            result["troops"] = [
                {"index": t.index, "status": getattr(t, "status", ""),
                 "dispatched": bool(getattr(t, "dispatched", False)),
                 "arrived": bool(getattr(t, "arrived", False)),
                 "arrive_at": getattr(t, "arrive_at", ""),
                 "cost": getattr(t, "cost_text", "")}
                for t in mr.troops]
            arrives = [getattr(t, "arrive_at", "") for t in done
                       if getattr(t, "arrive_at", "")]
            result["last_arrival"] = max(arrives) if arrives else ""
            result["last_troop"] = next(
                (t.index for t in done
                 if getattr(t, "arrive_at", "") == result["last_arrival"]), 0)
            result["ok"] = bool(done)
            if not result["ok"]:
                result["reason"] = "dispatch-failed"
            self._prefer_march = False
            return result
        result["reason"] = "no-marchable-tile"
        return result


__all__ = ["Marcher", "MARCH_BUTTON_PRIORITY", "NEAR_OFFSETS"]
