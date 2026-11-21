"""自动拉线 · 节点四：**回家补兵**。

流程：
1. 回城：不在城内视角就先回地图，再点左下「回城」（地图/城内同一点位 ≈ (60,772)）
2. 打开「亲卫营」：城内右侧标签（OCR 定位，兜底固定点 (1298,290)）
3. 逐队检查：左侧「亲卫一 / 二 / 三」页签 → 右侧读「兵力 当前/上限」
   - 已满：跳过（页内会写「兵力已满」）
   - 不满：在页内找「补兵 / 征兵」按钮点掉（排除「建筑内自动补兵」开关，避免误关）
   - 读不到兵力：不动作，避免误点
4. 退出亲卫部队 → 城内 → 点左下「出城」回世界地图，给下一轮「邮件分析战场」让位

实测（1600x900，2026-11-21）：
- 城内右下「亲卫营」标签 ≈ (1298,272)；点它打开「亲卫部队」整页
- 亲卫部队页左侧页签 亲卫一/二/三 ≈ y=190/280/355
- 右侧「兵力 33000/33000」在 (1090,330)–(1600,395)
- 页内底部有「今日已补兵 x/30000」和「建筑内自动补兵 ✓」（本模块不会点它）
- 亲卫一 兵力 33000、亲卫二 30300、亲卫三 …（各队上限不同，按 OCR 读到的上限比较）

用法:
    python -m sanmou_auto.home            # 回城 → 检查/补兵 → 出城
    python -m sanmou_auto.home --keep     # 结束后留在城内
"""
from __future__ import annotations

import argparse
import json
import re
import time

from . import cli, ocrengine, vision
from .adb import Adb
from .mapnav import CITY_EXIT_POINTS, MapNav
from .timing import STAGE_HOME

# 城内「亲卫营」标签（OCR 兜底用固定点；场景固定，位置稳定）
QINWEI_LABEL_BOX = (1000, 170, 1600, 360)
QINWEI_ENTRY = (1298, 290)

# 亲卫部队面板：标题「亲卫部队」+ 左侧页签
PANEL_TITLE_BOX = (120, 5, 620, 70)
TAB_BOX = (20, 120, 210, 700)
QINWEI_TABS_FALLBACK = {
    "亲卫一": (85, 190),
    "亲卫二": (85, 280),
    "亲卫三": (85, 355),
}
QINWEI_TAB_NAMES = ("亲卫一", "亲卫二", "亲卫三")

# 右侧「兵力 33000/33000」
STRENGTH_BOX = (1080, 325, 1600, 400)
STRENGTH_RE = re.compile(r"(\d{3,6})\s*[/／]\s*(\d{3,6})")

# 页内补兵按钮（满兵时是「兵力已满」文字；不满时同一带里出现补兵/征兵）
REPLENISH_BOXES = ((1000, 590, 1520, 720), (1000, 700, 1520, 800),
                   (1000, 300, 1600, 470))
REPLENISH_HINTS = ("补兵", "征兵")
REPLENISH_SKIP = ("建筑内", "自动补兵", "今日已补兵", "未消耗")

# 补兵后的确认弹窗
CONFIRM_HINTS = ("确定", "确认", "立即征兵")
CONFIRM_BOX = (400, 300, 1260, 800)

# 亲卫部队页左上返回箭头（与全屏页统一）
PANEL_BACK = (78, 30)

# 弹窗（如「部队总览」/「一字阵」）用右上角 ✕ 关闭；左上返回箭头对弹窗无效
DIALOG_TITLE_BOX = (70, 55, 520, 132)
DIALOG_HINTS = ("部队总览", "阵")
DIALOG_CLOSE = (1500, 95)


class HomeReplenish:
    """回家补兵：回城 → 亲卫营检查/补兵 → 出城。"""

    def __init__(self, adb: Adb | None = None, log=print, timer=None):
        self.adb = adb or Adb()
        self.nav = MapNav(self.adb, log=log)
        self.log = log
        self.timer = timer

    def _mark(self, name: str, kind: str = "执行", note: str = "") -> None:
        if self.timer is not None:
            self.timer.mark(name, kind=kind, note=note, stage=STAGE_HOME)

    # -- 面板状态 ---------------------------------------------------------
    def panel_open(self, img=None) -> bool:
        """「亲卫部队」页是否打开（标题 OCR 常丢首字「亲」，用「卫部队」+ 页签双重判定）。"""
        img = img or self.adb.screencap()
        title = ocrengine.ocr_text(vision.crop(img, PANEL_TITLE_BOX), 2.0)
        title = title.replace(" ", "")
        if "卫部队" in title or "亲卫部队" in title:
            return True
        tabs = ocrengine.ocr_text(vision.crop(img, TAB_BOX), 1.8)
        return "亲卫" in tabs and "亲卫三" in tabs

    def at_home(self, img=None) -> bool:
        return self.nav.in_city_view(img)

    # -- 回城 -------------------------------------------------------------
    def close_dialog(self) -> bool:
        """关掉「部队总览」这类弹窗（左上返回箭头对弹窗无效，得点右上 ✕）。"""
        img = self.adb.screencap()
        text = ocrengine.ocr_text(vision.crop(img, DIALOG_TITLE_BOX), 2.0)
        if not any(h in text for h in DIALOG_HINTS):
            return False
        self.log(f"  · 关闭弹窗「{text.strip()}」@{DIALOG_CLOSE}")
        self.adb.tap(*DIALOG_CLOSE)
        time.sleep(1.2)
        return True

    def go_home(self, tries: int = 2) -> bool:
        """回城到城内视角（已经在家就直接成功）。"""
        if self.panel_open():
            self.close_panel()              # 上一轮停在亲卫部队页
        self.close_dialog()
        if self.at_home():
            self.log("  · 已在城内，无需回城")
            return True
        self.nav.ensure_map()
        self.close_dialog()
        for _ in range(max(1, tries)):
            for point in CITY_EXIT_POINTS:
                self.log(f"== 点击「回城」@{point}")
                self.adb.tap(*point)
                time.sleep(3.0)
                if self.at_home():
                    self.log("  ✓ 已回城（城内视角）")
                    return True
            self.log("  · 回城没生效，重试")
        return self.at_home()

    # -- 亲卫营 -----------------------------------------------------------
    def _find_qinwei_label(self, img):
        for s in ocrengine.ocr(vision.crop(img, QINWEI_LABEL_BOX), 2.0):
            if "亲卫营" in s.text.replace(" ", ""):
                return (int(QINWEI_LABEL_BOX[0] + s.cx),
                        int(QINWEI_LABEL_BOX[1] + s.cy))
        return None

    def open_qinwei(self, tries: int = 2) -> bool:
        for i in range(tries):
            img = self.adb.screencap()
            if self.panel_open(img):
                return True
            point = self._find_qinwei_label(img) or QINWEI_ENTRY
            self.log(f"== 点击「亲卫营」@{point}")
            self.adb.tap(*point)
            time.sleep(2.2)
        ok = self.panel_open()
        if not ok:
            self.log("  ! 没能打开「亲卫部队」面板")
        return ok

    def tabs(self, img=None) -> list[tuple[str, tuple[int, int]]]:
        """左侧页签（OCR 优先，缺的用实测固定点补）。"""
        img = img or self.adb.screencap()
        out: list[tuple[str, tuple[int, int]]] = []
        for s in ocrengine.ocr(vision.crop(img, TAB_BOX), 2.0):
            t = s.text.replace(" ", "")
            for name in QINWEI_TAB_NAMES:
                if name in t and all(name != n for n, _ in out):
                    out.append((name, (int(TAB_BOX[0] + s.cx),
                                       int(TAB_BOX[1] + s.cy))))
        for name, point in QINWEI_TABS_FALLBACK.items():
            if all(name != n for n, _ in out):
                out.append((name, point))
        out.sort(key=lambda x: x[1][1])
        return out

    # -- 兵力 / 补兵 ------------------------------------------------------
    def read_strength(self, img=None) -> tuple[int, int] | None:
        """读右侧「兵力 当前/上限」；读不到返回 None。"""
        img = img or self.adb.screencap()
        box = vision.crop(img, STRENGTH_BOX)
        for scale in (2.5, 3.0, 2.0):
            text = ocrengine.ocr_text(box, scale).replace(" ", "")
            m = STRENGTH_RE.search(text)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    def _read_panel_note(self, img=None) -> str:
        img = img or self.adb.screencap()
        return ocrengine.ocr_text(vision.crop(img, (1000, 580, 1600, 880)), 2.0)

    def find_replenish(self, img=None):
        """在亲卫部队页里找「补兵 / 征兵」按钮，返回 (点击点, 文字) 或 (None, '')。"""
        img = img or self.adb.screencap()
        for box in REPLENISH_BOXES:
            for s in ocrengine.ocr(vision.crop(img, box), 2.0):
                t = s.text.replace(" ", "")
                if any(x in t for x in REPLENISH_SKIP):
                    continue                       # 「建筑内自动补兵」开关，别点
                if any(h in t for h in REPLENISH_HINTS):
                    return (int(box[0] + s.cx), int(box[1] + s.cy)), t
        return None, ""

    def _confirm_dialog(self, img=None) -> str:
        """补兵确认弹窗：点「确定/确认/立即征兵」。"""
        img = img or self.adb.screencap()
        for s in ocrengine.ocr(vision.crop(img, CONFIRM_BOX), 2.0):
            t = s.text.replace(" ", "")
            if any(h in t for h in CONFIRM_HINTS):
                point = (int(CONFIRM_BOX[0] + s.cx), int(CONFIRM_BOX[1] + s.cy))
                self.log(f"  · 补兵确认「{t}」@{point}")
                self.adb.tap(*point)
                time.sleep(1.2)
                return t
        return ""

    def replenish_one(self, name: str) -> bool:
        """某队兵力不满时：找补兵按钮点掉。找不到就放弃（不猜坐标）。"""
        point, text = self.find_replenish()
        if not point:
            self.log(f"  ! {name} 兵力不满，但页内没找到「补兵/征兵」按钮"
                     "（建筑内自动补兵可能已开启）")
            return False
        self.log(f"== {name} 点「{text}」补兵 @{point}")
        self.adb.tap(*point)
        time.sleep(1.8)
        self._confirm_dialog()
        time.sleep(0.8)
        return True

    # -- 退出 -------------------------------------------------------------
    def close_panel(self, tries: int = 2) -> bool:
        for i in range(max(1, tries)):
            if not self.panel_open():
                return True
            self.adb.tap(*PANEL_BACK)
            time.sleep(1.5)
        return not self.panel_open()

    # -- 主流程 -----------------------------------------------------------
    def run(self, leave: bool = True) -> dict:
        result: dict = {"ok": False, "reason": "", "strengths": {},
                        "replenished": [], "shots": []}
        self.adb.ensure_online()

        if not self.go_home():
            result["reason"] = "not-at-home"
            self._mark("回城就位", "执行", note="失败")
            return result
        self._mark("回城就位", "执行")

        if not self.open_qinwei():
            result["reason"] = "no-qinwei-panel"
            self._mark("打开亲卫营", "执行", note="失败")
            if leave:
                self.nav.ensure_map()
            return result
        self._mark("打开亲卫营", "执行")

        checked: dict[str, list[int]] = {}
        for name, point in self.tabs():
            self.log(f"== 检查 {name}")
            self.adb.tap(*point)
            time.sleep(1.2)
            st = self.read_strength()
            if st is None:
                self.log(f"  ! {name} 读不到兵力，跳过")
                result["strengths"][name] = None
                continue
            cur, cap = st
            checked[name] = [cur, cap]
            result["strengths"][name] = [cur, cap]
            self.log(f"  {name} 兵力 {cur}/{cap}"
                     + ("（已满）" if cur >= cap else "（不满，需补兵）"))
        self._mark("检查亲卫兵力", "分析",
                   note=" ".join(f"{k}={v[0]}/{v[1]}" for k, v in checked.items()))

        for name in [n for n, v in checked.items() if v[0] < v[1]]:
            self.adb.tap(*QINWEI_TABS_FALLBACK.get(name, QINWEI_ENTRY))
            time.sleep(1.0)
            if self.replenish_one(name):
                result["replenished"].append(name)
                st = self.read_strength()
                if st:
                    self.log(f"  ✓ {name} 补兵后 {st[0]}/{st[1]}")
                    result["strengths"][name] = [st[0], st[1]]
        self._mark("亲卫补兵", "执行", note=f"{len(result['replenished'])} 队")

        self.close_panel()
        if leave:
            self.nav.leave_city()
        self._mark("退出亲卫营/出城", "执行")

        result["ok"] = True
        result["note"] = ("三队兵力均已达上限" if len(result["replenished"]) == 0
                          else f"补兵 {len(result['replenished'])} 队")
        self.log(f"== 回家补兵完成：{result['note']}")
        return result


def main(argv: list[str] | None = None) -> int:
    cli.setup_console()
    ap = argparse.ArgumentParser(description="自动拉线节点四：回家补兵")
    ap.add_argument("--keep", action="store_true", help="结束后留在城内（不自动出城）")
    args = ap.parse_args(argv)

    res = HomeReplenish().run(leave=not args.keep)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
