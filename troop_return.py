"""右侧部队头像：读状态 / 判断 ready / 回城。

用户口径（2026-11-23）：
- 每一轮战报收集完成后，通过右侧部队头像判断是否存在 ready 部队；
  已 ready 的部队纳入下一次「调动 → 行军」。
- 点击右侧部队头像 → 镜头跳到该部队 → 屏幕中央出现一个大号「回城」按钮；
  点「回城」会弹确认框 → 勾选「今天登录不再提示」→ 点「确定」即可回城。
  勾选后下一次不再弹框，直接回城。
- 所有点击（头像 / 回城 / 确定）都必须先 OCR 命中再点文字/按钮中心，防止点偏卡死。

坐标（竖屏 900x1600 实测 2026-11-23）：
- 头像圆环中心 x≈815，5 个中心 y≈303 / 437 / 546 / 662 / 772；
- 点开头像后，地图中央「回城」按钮中心 ≈ (450,748)。

注意：
- 头像顺序按「1-4 队 + 亲卫/待命」假定为自上而下；若实机顺序不同，
  以 `read_avatars()` 读到的状态文本为准，不要盲点。
- `ready` 的判定是「状态文本命中 READY_HINTS」或「头像右下出现绿色可交互标识」，
  同时排除「行军/出征/调动 + 倒计时」这种仍在途状态。
"""
from __future__ import annotations

import re
import time

from PIL import Image

from . import config, fastwait, ocrengine, vision
from .adb import Adb
from .mapnav import MapNav

COUNTDOWN_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
MOVING_HINTS = ("行军", "出征", "调动中", "返回", "回城")
STATIONED_HINTS = ("驻守", "调动", "待命", "空闲", "就绪")


def _norm(text: str) -> str:
    return (text or "").replace(" ", "").replace("：", ":").strip()


class TroopReturn:
    """右侧部队头像状态读取与「点头像 → 回城」动作。"""

    def __init__(self, adb: Adb | None = None, log=print,
                 nav: MapNav | None = None):
        self.adb = adb or Adb()
        self.nav = nav or MapNav(self.adb, log=log)
        self.log = log

    # ------------------------------------------------------------ 坐标换算
    @staticmethod
    def _scale(img: Image.Image) -> float:
        return img.width / float(config.SCREEN[0])

    def _box(self, img: Image.Image, base_box) -> tuple[int, int, int, int]:
        k = self._scale(img)
        return (max(0, round(base_box[0] * k)),
                max(0, round(base_box[1] * k)),
                min(img.width, round(base_box[2] * k)),
                min(img.height, round(base_box[3] * k)))

    def _row_box(self, img: Image.Image, idx: int):
        x = int(config.RIGHT_AVATAR_X)
        y = int(config.RIGHT_AVATAR_Y.get(idx, config.RIGHT_AVATAR_Y[1]))
        dx1, dx2 = config.RIGHT_AVATAR_STATUS_DX
        dy = int(config.RIGHT_AVATAR_STATUS_DY)
        base = (x + dx1, y - 8, x + dx2, y + dy)
        return self._box(img, base)

    @staticmethod
    def _pixel_ratio(img: Image.Image, box, pred) -> float:
        x1, y1, x2, y2 = box
        if x2 - x1 < 4 or y2 - y1 < 4:
            return 0.0
        px = img.load()
        total = 0
        hit = 0
        for y in range(y1, y2, 2):
            for x in range(x1, x2, 2):
                total += 1
                try:
                    r, g, b = px[x, y]
                except (IndexError, TypeError):
                    continue
                if pred(r, g, b):
                    hit += 1
        return hit / max(1, total)

    # ------------------------------------------------------------ 读状态
    def read_avatars(self, img: Image.Image | None = None) -> list[dict]:
        """逐个头像读状态；返回 `[{index,center,text,ready,moving,strength?}]`。"""
        img = img or self.adb.screencap()
        k = self._scale(img)
        out: list[dict] = []
        for idx in config.RIGHT_AVATAR_ORDER:
            box = self._row_box(img, idx)
            try:
                spans = ocrengine.ocr(vision.crop(img, box), 2.4)
            except Exception:               # noqa: BLE001 - OCR 失败按未知处理
                spans = []
            text = _norm("".join(s.text for s in spans))
            moving = bool(COUNTDOWN_RE.search(text)) or any(
                h in text for h in MOVING_HINTS)
            # 右下角的绿色箭头是「可再次出征」的第二判据（避开血条绿色）
            x = int(config.RIGHT_AVATAR_X)
            y = int(config.RIGHT_AVATAR_Y.get(idx, config.RIGHT_AVATAR_Y[1]))
            green_box = self._box(img, (x + 26, y + 24, x + 80, y + 70))
            green = self._pixel_ratio(
                img, green_box,
                lambda r, g, b: g >= 145 and g - r >= 35 and g - b >= 25)
            # 「队列」是覆盖在头像上的排队标识：有它说明该队已被占用；
            # 没有状态文字（空闲/待命时原本就不显示文字）视为可用。
            queued = "队列" in text
            ready = (not moving) and (not queued)
            out.append({
                "index": idx,
                "center": (round(x * k), round(y * k)),
                "text": text,
                "moving": moving,
                "queued": queued,
                "ready": bool(ready),
                "green": round(green, 3),
            })
        return out

    def read_avatars_clean(self) -> list[dict]:
        """先取消地块选中再读头像（右侧动作列会盖住第 4 个头像并污染 OCR）。"""
        try:
            if self.nav.detail_panel_visible():
                self.adb.swipe(450, 700, 520, 700, 200)
                fastwait.wait_settle(self.adb, box=config.RIGHT_AVATAR_ROW_BOX,
                                     timeout=1.2, min_wait=0.3)
        except Exception:                   # noqa: BLE001 - 只是观察，失败不阻断
            pass
        return self.read_avatars()

    def ready_troops(self) -> list[int]:
        rows = self.read_avatars_clean()
        ready = [r["index"] for r in rows if r["ready"]]
        text = ", ".join(f"{r['index']}:{r['text'] or '?'}" for r in rows)
        if ready:
            self.log(f"  [头像] ready 部队 {ready}（{text}）")
        else:
            self.log(f"  [头像] 暂无可出征 ready（{text}）")
        return ready

    def status_line(self) -> str:
        rows = self.read_avatars()
        return " | ".join(
            f"{r['index']}={'READY' if r['ready'] else (r['text'] or '未知')}"
            for r in rows)

    # ------------------------------------------------------------ 点击
    def _clear_selection(self) -> None:
        """取消地块选中：有中央「回城」/右侧动作列时，点头像会打开配将助手。

        实测：选中据点/营地时直接点头像，会进入「配将助手/部队总览」，
        而不是跳到部队所在地。必须先小拖拽取消选中。
        """
        for _ in range(2):
            try:
                has_return, _ = self.return_button()
                detail = self.nav.detail_panel_visible()
            except Exception:               # noqa: BLE001
                return
            if not has_return and not detail:
                return
            self.adb.swipe(450, 700, 520, 700, 200)
            time.sleep(0.8)

    def tap_avatar(self, idx: int) -> bool:
        """OCR 确认头像行存在后，点头像中心。"""
        if idx not in config.RIGHT_AVATAR_Y:
            return False
        img = self.adb.screencap()
        k = self._scale(img)
        x = round(int(config.RIGHT_AVATAR_X) * k)
        y = round(int(config.RIGHT_AVATAR_Y[idx]) * k)
        box = self._row_box(img, idx)
        text = _norm(ocrengine.ocr_text(vision.crop(img, box), 2.4))
        self.log(f"  · 点击部队{idx}头像 @({x},{y})"
                 + (f"（识别「{text}」）" if text else "（未读到状态文字）"))
        self.adb.tap(x, y)
        fastwait.wait_settle(self.adb, box=config.RETURN_HOME_BOX,
                             timeout=2.4, min_wait=0.4)
        return True

    def return_button(self, img: Image.Image | None = None):
        """OCR 找地图中央「回城」按钮，返回 (文字中心点, 文本) 或 (None, '')。"""
        img = img or self.adb.screencap()
        box = self._box(img, config.RETURN_HOME_BOX)
        k = self._scale(img)
        best = None
        for s in ocrengine.ocr(vision.crop(img, box), 2.2):
            t = _norm(s.text)
            if "回城" in t:
                point = (int((box[0] + s.cx) / k), int((box[1] + s.cy) / k))
                if best is None or len(t) < len(best[1]):
                    best = (point, t)
        return best if best else (None, "")

    def _dialog_spans(self, img: Image.Image | None = None):
        img = img or self.adb.screencap()
        box = self._box(img, config.RETURN_DIALOG_BOX)
        k = self._scale(img)
        spans = []
        for s in ocrengine.ocr(vision.crop(img, box), 2.0):
            spans.append({
                "text": _norm(s.text),
                "point": (int((box[0] + s.cx) / k),
                          int((box[1] + s.cy) / k)),
                "base": (int((box[0] + s.cx - 40) / k),
                         int((box[1] + s.cy) / k)),
            })
        return spans

    def confirm_return_dialog(self, img: Image.Image | None = None) -> bool:
        """勾选「今天登录不再提示」并点「确定」；没有弹框返回 False。"""
        spans = self._dialog_spans(img)
        joined = "".join(s["text"] for s in spans)
        has_dialog = any(h in joined for h in config.RETURN_DIALOG_CHECK_HINTS) \
            or any(h in joined for h in config.RETURN_DIALOG_OK_HINTS)
        if not has_dialog:
            return False
        check = next((s for s in spans
                      if any(h in s["text"]
                             for h in config.RETURN_DIALOG_CHECK_HINTS)), None)
        if check:
            # 复选框在文字左侧：点文字左边缘再往左 40px
            self.log(f"  · 回城弹框：勾选「{check['text']}」@{check['base']}")
            self.adb.tap(*check["base"])
            time.sleep(0.4)
        else:
            self.log(f"  · 回城弹框：没读到「不再提示」，按兜底点 "
                     f"{config.RETURN_DIALOG_CHECK_FALLBACK}")
            self.adb.tap(*config.RETURN_DIALOG_CHECK_FALLBACK)
            time.sleep(0.4)
        ok = next((s for s in spans
                   if s["text"] in config.RETURN_DIALOG_OK_HINTS), None)
        if ok is None:
            ok = next((s for s in spans
                       if any(h in s["text"]
                              for h in config.RETURN_DIALOG_OK_HINTS)), None)
        point = ok["point"] if ok else config.RETURN_DIALOG_OK_FALLBACK
        self.log(f"  · 回城弹框：点「确定」@{point}")
        self.adb.tap(*point)
        time.sleep(1.2)
        return True

    def return_home(self, idx: int, tries: int = 2) -> bool:
        """点第 idx 个部队头像 → 点回城 → 处理「今天登录不再提示」弹框。"""
        for attempt in range(max(1, tries)):
            # 每次重试前都先确保处在「无选中」的世界地图
            try:
                if not self.nav.on_map():
                    self.nav.ensure_map()
            except Exception:               # noqa: BLE001
                pass
            self._clear_selection()
            if not self.tap_avatar(idx):
                return False
            found, text = fastwait.wait_text(
                self.adb, config.RETURN_HOME_BOX, ("回城",),
                timeout=3.6, scale=2.2, min_wait=0.4)
            point, label = self.return_button()
            if point is None:
                self.log(f"  ! 部队{idx} 点开头像后没看到「回城」按钮"
                         f"（{found=}，读到「{text or label}」）")
                continue
            # OCR 只用于确认「回城」按钮存在；点击点 = OCR 文字中心上移一点
            # （文字在圆形按钮偏下；固定中心会因镜头/地块变化而落到按钮外）。
            k = self._scale(self.adb.screencap())
            center = (round(point[0] * k),
                      round(max(0, point[1] - config.RETURN_HOME_TEXT_OFFSET_Y) * k))
            self.log(f"== 部队{idx} 回城：OCR 命中「{label}」@{point}，"
                     f"点按钮中心 @{center}")
            self.adb.tap(*center)
            time.sleep(1.0)
            # 第一次会弹「今天登录不再提示」，勾选并确定；之后直接回城
            self.confirm_return_dialog()
            fastwait.wait_settle(self.adb, box=self._box(
                self.adb.screencap(), config.RETURN_HOME_BOX),
                timeout=2.0, min_wait=0.5)
            # 回城成功的判据：地图中央不再有「回城」按钮，或已进入城内视角
            still, _ = self.return_button()
            row = self.read_avatars()
            state = next((r for r in row if r["index"] == idx), {})
            if still is None or self.nav.in_city_view():
                self.log(f"  ✓ 部队{idx} 已回城（头像状态「{state.get('text') or '未知'}」）")
                return True
            self.log(f"  ! 部队{idx} 第{attempt + 1}次回城没生效"
                     f"（中央仍有「回城」，头像「{state.get('text') or '未知'}」）")
        return False

    def return_all(self, indexes=(1, 2, 3, 4)) -> dict:
        result: dict = {"ok": False, "returned": [], "failed": []}
        # 先回到干净的世界地图：避免停在「部队总览 / 配将助手」等二级页时，
        # 点右侧头像只会点到页面内容（实测踩过）。
        try:
            self.nav.ensure_map()
        except Exception as error:          # noqa: BLE001
            self.log(f"  ! 回城前恢复地图失败：{error!r}")
        # 取消地块选中，避免右侧动作列遮挡下方的部队头像
        try:
            if self.nav.detail_panel_visible():
                self.adb.swipe(450, 700, 520, 700, 200)
                time.sleep(0.6)
        except Exception:                   # noqa: BLE001
            pass
        for idx in indexes:
            if self.return_home(int(idx)):
                result["returned"].append(int(idx))
                # 回城会把镜头带到城内；下一支部队必须先在世界上点头像
                try:
                    if self.nav.in_city_view():
                        self.nav.leave_city()
                except Exception:           # noqa: BLE001
                    pass
            else:
                result["failed"].append(int(idx))
        result["ok"] = not result["failed"]
        return result


__all__ = ["TroopReturn"]
