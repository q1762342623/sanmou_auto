"""地图沙盘快跳：缩放到沙盘页 → 填坐标 → 前往 → 点中心地块。

背景（实测 2026-11-22，竖屏 900x1600）：
- 世界地图双指收拢到一定程度（约 2~3 次捏合）会出现「沙盘页」：
  左侧地图筛选、右上「战局总览 / 沙盘回放」、底部
  `请输入搜索目标` + 坐标框(`x.y`) + 金色「前往」+「地图沙盘」。
- 坐标框可直接编辑：点一下 → 清空 → 输入 `696.781` → 键盘栏「确定」→「前往」，
  地图会**直接跳到该坐标**并回到普通缩放，屏幕中心就是目标地块；
  再点一下屏幕中心即可选中它（右侧出现 行军/调动/驻守…）。
- 这条路径实测 15~22s（含 2~3 次捏合），替代「打开同盟列表 → 逐屏 OCR 找行 →
  点坐标 → 确认跳转」的 80~380s 长链路（缓存候选失效时最慢）。

可靠性（默认重试 4 次）：
- 每次跳完读底部地块信息里的 `（x,y）` 校验是否真的到了目标坐标，
  不一致/读不到就重来（捏合→填坐标→前往）；
- `ensure_open()` 最多捏合 6 次，进不去就返回 False，由调用方回退列表链路。

注意：
- 捏合只能走 sendevent（adb input 不支持多点），见 `Adb.pinch()`；
- 这些坐标是竖屏物理坐标，横屏需另测（当前运行环境固定竖屏）。
"""
from __future__ import annotations

import re
import time

from . import ocrengine, vision
from .adb import Adb

# 竖屏 900x1600 实测坐标
P_SEARCH = (288, 1192)      # 「请输入搜索目标」输入框
P_COORD = (562, 1263)       # 坐标框（显示 1.1500 / 556.884 这种 x.y）
P_GO = (771, 1262)          # 金色「前往」
P_CENTER = (450, 800)       # 地图中心（跳转后的目标地块）
P_IME_OK = (863, 1550)      # 游戏内输入法栏「确定」

HINT_BOX = (0, 1160, 700, 1230)
GO_BOX = (700, 1225, 860, 1300)
# 底部「地点信息」条可能出现在两个位置（随右侧动作列/浮层高度变化）
SELECTED_BOXES = ((0, 830, 900, 975), (0, 1380, 900, 1480))
# 右括号经常被 OCR 吞掉（实测只读到「建业台（696,781」），所以闭合括号可选。
# 但坐标后面常粘着等级/数量（实测 (626,759) 读成 626,7591），
# 因此先用「带右括号的严格式」，失败再用宽松式并做范围校验/截断。
SELECTED_STRICT_RE = re.compile(
    r"[（(]\s*(\d{1,3})\s*[,，]\s*(\d{1,3})\s*[)）]")
SELECTED_RE = re.compile(r"[（(]?\s*(\d{1,3})\s*[,，]\s*(\d{1,4})")


class SandboxJump:
    """沙盘页坐标跳转。调用前需保证在世界地图（不是城内/弹窗）。"""

    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.log = log
        self._open = False

    # ------------------------------------------------------------------ 判定
    def visible(self, img=None) -> bool:
        """底部出现「搜索目标」或「前往」即认为沙盘页已打开。"""
        img = img or self.adb.screencap()
        hint = ocrengine.ocr_text(vision.crop(img, HINT_BOX), 2.0)
        if "搜索" in hint or "目标" in hint:
            return True
        go = ocrengine.ocr_text(vision.crop(img, GO_BOX), 2.0)
        return "前往" in go

    def close(self, max_pinches: int = 3, settle: float = 1.0) -> bool:
        """退出沙盘页（双指外扩放大回到普通地图）。

        `ensure_map` / 列表选城都依赖「世界地图」，停在沙盘页时底栏是
        「城池/战没/同盟/个人」，点同盟入口会打开排行榜，后续全部失败。
        """
        if not self.visible():
            return True
        for i in range(max(1, max_pinches)):
            self.log(f"  · 退出沙盘页（放大 {i + 1}/{max_pinches}）")
            self.adb.pinch("in", steps=16, radius=0.9)
            time.sleep(settle)
            if not self.visible():
                self._open = False
                return True
        self._open = False
        return not self.visible()

    def ensure_open(self, max_pinches: int = 6, settle: float = 1.1) -> bool:
        """不在沙盘页就双指收拢，直到出现（最多 max_pinches 次）。

        游戏每次捏合只推进一级缩放（实测单次大半径也到不了沙盘），
        所以「每批 2 次捏合 + 一次判定」，把昂贵的沙盘判定 OCR 次数压到 3 次以内。
        """
        if self.visible():
            self._open = True
            return True
        # 先清掉地块浮层 / 右侧动作列：浮层开着时捏合会被界面吃掉（实测 5 次都没反应）。
        # 不能点「空白点」（y≈240 会命中顶部活动图标），也不能按 BACK（弹退出确认），
        # 小拖拽一步只取消选中、不会触发新地块。
        self.adb.swipe(450, 700, 520, 700, 200)
        time.sleep(0.8)
        if self.visible():
            self._open = True
            return True
        done = 0
        while done < max(1, max_pinches):
            for _ in range(min(2, max_pinches - done)):
                done += 1
                self.log(f"  · 缩放地图（捏合 {done}/{max_pinches}）进入沙盘页")
                self.adb.pinch("out", steps=16, radius=0.9)
                time.sleep(settle)
            if self.visible():
                self._open = True
                return True
        return False

    # ------------------------------------------------------------------ 校验
    def selected_coord(self) -> tuple[int, int] | None:
        """读底部地块信息里的 `（x,y）`；读不到返回 None。"""
        img = self.adb.screencap()
        for box in SELECTED_BOXES:
            text = ocrengine.ocr_text(vision.crop(img, box), 2.0)
            flat = text.replace(" ", "")
            point = None
            m = SELECTED_STRICT_RE.search(flat)
            if m:
                point = self._valid_coord(m.group(1), m.group(2))
            else:
                m = SELECTED_RE.search(flat)
                if m:
                    point = self._valid_coord(m.group(1), m.group(2))
            if point:
                return point
        return None

    @staticmethod
    def _valid_coord(raw_x: str, raw_y: str) -> tuple[int, int] | None:
        """校验 OCR 坐标；处理「(626,759) 读成 626,7591」的粘连。"""
        try:
            x, y = int(raw_x), int(raw_y)
        except ValueError:
            return None
        if x > 2000:
            return None
        if y > 2000:
            # 常见：759 + 后面一两位等级/数量 → 取前 3 位
            text = str(raw_y)
            for cut in (3, 2):
                if len(text) > cut:
                    try:
                        y = int(text[:cut])
                    except ValueError:
                        continue
                    if y <= 2000:
                        break
        if 0 < x < 2000 and 0 < y < 2000:
            return x, y
        return None

    # ------------------------------------------------------------------ 跳转
    def _jump_once(self, coord: tuple[int, int], tap_center: bool,
                   wait: float) -> bool:
        x, y = coord
        if not self.ensure_open():
            return False
        self.adb.tap(*P_COORD)
        time.sleep(0.7)
        self.adb.key_repeat(67, 12)          # DEL 清空原坐标
        time.sleep(0.2)
        self.adb.input_text(f"{x}.{y}")
        time.sleep(0.5)
        self.adb.tap(*P_IME_OK)
        time.sleep(0.6)
        self.adb.tap(*P_GO)
        time.sleep(wait)
        if tap_center:
            self.adb.tap(*P_CENTER)
            time.sleep(1.2)
        return True

    def goto(self, coord: tuple[int, int], tap_center: bool = True,
             wait: float = 2.2, tries: int = 4) -> bool:
        """跳到坐标并（默认）点选中心地块；校验坐标不符会自动重试。

        返回 True 的条件：流程走完，且「底部地块信息坐标 == 目标」
        （OCR 没读到时按成功处理，交给调用方用「调动按钮」再判一次）。
        """
        target = (int(coord[0]), int(coord[1]))
        for attempt in range(1, max(1, tries) + 1):
            if not self._jump_once(target, tap_center, wait):
                self.log(f"  ! 没能进入沙盘页（第 {attempt}/{tries} 次），放弃坐标快跳")
                self._safe_close()
                return False
            got = self.selected_coord() if tap_center else None
            if got is None or got == target:
                self._open = False
                return True
            self.log(f"  ! 跳转落到 {got} ≠ 目标 {target}"
                     f"（第 {attempt}/{tries} 次），重试")
            # 落到别处说明输入/点击有偏差：先拖开浮层再重来
            self.adb.swipe(450, 700, 520, 700, 200)
            time.sleep(0.6)
        self._open = False
        # 失败时不要停在缩放/沙盘页：后续「回城」等操作都依赖世界地图
        self._safe_close()
        return False

    def _safe_close(self) -> None:
        try:
            self.close(max_pinches=3)
        except Exception as error:          # noqa: BLE001
            self.log(f"  ! 退出沙盘页失败：{error!r}")

    def select_center(self, wait: float = 1.2) -> None:
        self.adb.tap(*P_CENTER)
        time.sleep(wait)
