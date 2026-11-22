"""战报自动收录 · 屏幕状态 / 截图 / 目标预览 / 回地图。

用法（在 skill 根目录执行）：
    python scripts/screen.py shot --name main
    python scripts/screen.py state
    python scripts/screen.py state --compare work/last.png
    python scripts/screen.py preview
    python scripts/screen.py back-to-map
全部输出单行 JSON，`ok` 为唯一成功标志。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import common


def cmd_shot(config: dict, args) -> dict:
    adb = common.make_adb(config)
    adb.ensure_online()
    out = common.resolve_path(args.out) if args.out else common.WORK_DIR / f"shot_{args.name}_{common.timestamp()}.png"
    width, height = adb.screenshot(out)
    return {"ok": True, "path": str(out), "width": width, "height": height}


def cmd_state(config: dict, args) -> dict:
    adb = common.make_adb(config)
    adb.ensure_online()
    shot = common.WORK_DIR / "state_current.png"
    width, height = adb.screenshot(shot)
    state = common.page_state(shot, config)
    engine = common.inspect_screenshot(shot, args.compare)
    state["blocks"] = {key: engine.get(key) for key in
                       ("reportTabsVisible", "selectedReportTab", "combatTabSelected",
                        "blockingOverlay", "dialogOverlay", "mainScreen", "tabGoldRatios")}
    state.update({"ok": True, "path": str(shot), "screen": {"width": width, "height": height}})
    return state


def cmd_preview(config: dict, args) -> dict:
    """把比例目标画到实拍截图上，用于首次适配/校准。"""
    cv2 = common.load_engine("cv2")
    adb = common.make_adb(config)
    adb.ensure_online()
    shot = common.WORK_DIR / f"preview_{common.timestamp()}.png"
    width, height = adb.screenshot(shot)
    image = common.load_color(shot)
    targets = common.targets_for(config, width, height)
    points = {
        "reportEntry": targets["reportEntry"],
        "allianceTab": targets["allianceTab"],
        "combatTab": targets["combatTab"],
        "pageBack": targets["pageBack"],
    }
    for name, (x, y) in points.items():
        cv2.drawMarker(image, (x, y), (0, 255, 0), cv2.MARKER_CROSS, 28, 2)
        cv2.putText(image, name, (max(2, x - 40), max(20, y - 18)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 5, cv2.LINE_AA)
        cv2.putText(image, name, (max(2, x - 40), max(20, y - 18)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2, cv2.LINE_AA)
    x1, y1, x2, y2, ms = targets["swipe"]
    cv2.arrowedLine(image, (x1, y1), (x2, y2), (0, 200, 255), 3, tipLength=0.06)
    band = common.targets_for(config, width, height)["tabBand"]
    cv2.rectangle(image,
                  (int(band[0] * width), int(band[1] * height)),
                  (int(band[2] * width), int(band[3] * height)),
                  (255, 200, 0), 2)
    out = shot.with_name(shot.stem + "_targets.png")
    cv2.imencode(".png", image)[1].tofile(str(out))
    return {"ok": True, "screenshot": str(shot), "annotated": str(out),
            "screen": {"width": width, "height": height}, "targets": targets}


def cmd_back_to_map(config: dict, args) -> dict:
    """点页面自带返回箭头回世界地图（不按 BACK，避免「确认退出游戏」）。

    判据只用底栏 OCR（`common.map_visible`）：点到出现「武将/同盟/征战」为止。
    另外每轮先检查并取消 `ExitActivity`，防止卡在「确认退出游戏」弹窗上。
    """
    adb = common.make_adb(config)
    adb.ensure_online()
    shot = common.WORK_DIR / "back_current.png"
    width, height = adb.screenshot(shot)
    targets = common.targets_for(config, width, height)
    presses = 0
    if common.map_visible(shot, config):
        return {"ok": True, "presses": presses, "path": str(shot),
                "state": _brief(common.page_state(shot, config)),
                "via": "map"}

    for _ in range(args.max_presses):
        if adb.exit_dialog_visible():
            adb.cancel_exit_dialog()
            adb.screenshot(shot)
            continue
        adb.tap(*targets["pageBack"], wait_ms=800)
        presses += 1
        adb.screenshot(shot)
        if common.map_visible(shot, config):
            state = common.page_state(shot, config)
            return {"ok": True, "presses": presses, "path": str(shot),
                    "state": _brief(state), "via": "map"}

    state = common.page_state(shot, config)
    ok = common.map_visible(shot, config)
    return {"ok": ok, "presses": presses, "path": str(shot),
            "state": _brief(state),
            "note": "达到最大返回次数，状态见 state"}


def _brief(state: dict) -> dict:
    keys = ("orientation", "reportPage", "selectedTab", "combatTabSelected",
            "blockingOverlay", "dialogOverlay", "tabGold", "tabOcrText")
    return {key: state.get(key) for key in keys}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    shot = sub.add_parser("shot")
    shot.add_argument("--name", default="shot")
    shot.add_argument("--out", default="")
    shot.set_defaults(func=cmd_shot)

    state = sub.add_parser("state")
    state.add_argument("--compare", default="")
    state.set_defaults(func=cmd_state)

    preview = sub.add_parser("preview")
    preview.set_defaults(func=cmd_preview)

    back = sub.add_parser("back-to-map")
    back.add_argument("--max-presses", type=int, default=4)
    back.set_defaults(func=cmd_back_to_map)

    args = parser.parse_args()
    config = common.load_config(args.config or None)
    try:
        result = args.func(config, args)
        result.setdefault("ok", True)
    except Exception as error:  # noqa: BLE001 - 单行 JSON 契约
        result = {"ok": False, "reason": type(error).__name__, "error": str(error)}
    common.json_print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
