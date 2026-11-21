"""Measure MuMu battle-report UI state from screenshots.

The automation uses these measurements as feedback for navigation and swipes.
They intentionally describe stable page-level signals rather than OCR text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import detect_expand_arrow as expand_detector


def load_color(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"failed to read image: {path}")
    return image


def load_gray(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise SystemExit(f"failed to read image: {path}")
    return image


def crop_ratio(
    image: np.ndarray, x1: float, y1: float, x2: float, y2: float
) -> np.ndarray:
    height, width = image.shape[:2]
    left = max(0, min(width - 1, int(round(width * x1))))
    right = max(left + 1, min(width, int(round(width * x2))))
    top = max(0, min(height - 1, int(round(height * y1))))
    bottom = max(top + 1, min(height, int(round(height * y2))))
    return image[top:bottom, left:right]


def gold_ratio(image: np.ndarray) -> float:
    blue, green, red = cv2.split(image)
    red16 = red.astype(np.int16)
    blue16 = blue.astype(np.int16)
    mask = (
        (red >= 90)
        & (green >= 65)
        & (blue <= 105)
        & ((red16 - blue16) >= 20)
    )
    return float(mask.mean())


def pink_ratio(image: np.ndarray) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    mask = (hue >= 155) & (saturation >= 100) & (value >= 160)
    return float(mask.mean())


def laplacian_variance(image: np.ndarray) -> float:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def visual_difference(before: np.ndarray, after: np.ndarray) -> dict[str, float]:
    if before.shape[:2] != after.shape[:2]:
        return {"mean": 255.0, "changedRatio": 1.0}
    portrait = before.shape[0] > before.shape[1]
    y1, y2 = (0.10, 0.88) if portrait else (0.16, 0.95)
    before_roi = crop_ratio(before, 0.02, y1, 0.98, y2)
    after_roi = crop_ratio(after, 0.02, y1, 0.98, y2)
    before_gray = cv2.cvtColor(before_roi, cv2.COLOR_BGR2GRAY)
    after_gray = cv2.cvtColor(after_roi, cv2.COLOR_BGR2GRAY)
    difference = cv2.absdiff(before_gray, after_gray)
    return {
        "mean": round(float(difference.mean()), 4),
        "changedRatio": round(float((difference >= 20).mean()), 6),
    }


MAIN_SCREEN_TEMPLATE_SCALES = tuple(
    round(0.70 + 0.05 * index, 2) for index in range(13)
)

# The report-entry icon can be covered by a red unread badge ("99+") or be
# redrawn for an event season. Both lower the normalised correlation without
# moving the icon, so a matched slot is accepted at a lower score than a
# blind template hit elsewhere on the screen.
MAIN_SCREEN_MIN_SCORE = 0.70
MAIN_SCREEN_SLOT_X_RATIO = 0.404
MAIN_SCREEN_SLOT_Y_RATIO = 0.8235
MAIN_SCREEN_SLOT_X_TOLERANCE = 0.015
MAIN_SCREEN_SLOT_Y_TOLERANCE = 0.012


def portrait_main_screen_match(
    screenshot: np.ndarray, template: np.ndarray | None
) -> dict[str, Any]:
    """Match the report-entry icon at its fixed main-screen slot.

    The template was captured on a 1080-wide portrait device. Emulator
    resolutions such as 900x1600 render the same icon at a smaller pixel size,
    so matching at a fixed scale fails even though the slot is identical.
    Search a small scale range and keep the best score.

    Besides the acceptance flag this returns the icon centre in screenshot
    pixels so the automation can calibrate its report-entry tap instead of
    relying on a fixed ratio.
    """
    if template is None or screenshot.shape[0] <= screenshot.shape[1]:
        return {
            "matched": False,
            "score": 0.0,
            "location": None,
            "center": None,
            "tapRatio": None,
            "positionMatched": False,
            "scale": 1.0,
            "minScore": MAIN_SCREEN_MIN_SCORE,
        }
    gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    left = round(width * 0.38)
    right = round(width * 0.50)
    top = round(height * 0.80)
    bottom = round(height * 0.87)
    roi = gray[top:bottom, left:right]
    if roi.shape[0] < 8 or roi.shape[1] < 8:
        return {
            "matched": False,
            "score": 0.0,
            "location": None,
            "center": None,
            "tapRatio": None,
            "positionMatched": False,
            "scale": 1.0,
            "minScore": MAIN_SCREEN_MIN_SCORE,
        }
    best_score = -1.0
    best_location = [0, 0]
    best_scale = 1.0
    best_size = [0, 0]
    for scale in MAIN_SCREEN_TEMPLATE_SCALES:
        resized = expand_detector.scale_template(template, scale)
        if resized.shape[0] > roi.shape[0] or resized.shape[1] > roi.shape[1]:
            continue
        result = cv2.matchTemplate(roi, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        if float(score) > best_score:
            best_score = float(score)
            best_scale = scale
            best_location = [int(left + location[0]), int(top + location[1])]
            best_size = [int(resized.shape[1]), int(resized.shape[0])]
    expected_location = [
        round(width * MAIN_SCREEN_SLOT_X_RATIO),
        round(height * MAIN_SCREEN_SLOT_Y_RATIO),
    ]
    position_matched = (
        abs(best_location[0] - expected_location[0])
        <= round(width * MAIN_SCREEN_SLOT_X_TOLERANCE)
        and abs(best_location[1] - expected_location[1])
        <= round(height * MAIN_SCREEN_SLOT_Y_TOLERANCE)
    )
    if best_size[0] > 0 and best_size[1] > 0:
        center = [
            int(best_location[0] + best_size[0] / 2.0),
            int(best_location[1] + best_size[1] / 2.0),
        ]
        tap_ratio = [round(center[0] / width, 4), round(center[1] / height, 4)]
    else:
        center = None
        tap_ratio = None
    return {
        # The icon can gain a badge or receive seasonal artwork changes. A lower
        # score is accepted only at its fixed main-screen slot.
        "matched": bool(best_score >= MAIN_SCREEN_MIN_SCORE and position_matched),
        "score": round(best_score, 4),
        "location": best_location,
        "center": center,
        "tapRatio": tap_ratio,
        "positionMatched": bool(position_matched),
        "scale": best_scale,
        "minScore": MAIN_SCREEN_MIN_SCORE,
    }


def _boxes_are_near(a: dict[str, int], b: dict[str, int], padding: int = 5) -> bool:
    return not (
        a["x"] + a["w"] + padding < b["x"]
        or b["x"] + b["w"] + padding < a["x"]
        or a["y"] + a["h"] + padding < b["y"]
        or b["y"] + b["h"] + padding < a["y"]
    )


def detect_report_cards(screenshot: np.ndarray) -> list[dict[str, Any]]:
    """Find complete report cards from their shared top-right action marker."""
    height, width = screenshot.shape[:2]
    if height <= width:
        return []
    list_top = int(round(height * 0.045))
    list_bottom = int(round(height * 0.870))
    roi_left = int(round(width * 0.900))
    roi_right = int(round(width * 0.980))
    roi = screenshot[list_top:list_bottom, roi_left:roi_right]
    blue, green, red = cv2.split(roi)
    high = np.maximum.reduce((blue, green, red)).astype(np.int16)
    low = np.minimum.reduce((blue, green, red)).astype(np.int16)
    white = (
        (blue >= 135)
        & (green >= 135)
        & (red >= 135)
        & ((high - low) < 85)
    )
    gold = (
        (red >= 105)
        & (green >= 70)
        & (blue < 145)
        & ((red.astype(np.int16) - blue.astype(np.int16)) > 15)
    )
    mask = (white | gold).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    components: list[dict[str, int]] = []
    for index in range(1, count):
        x, y, box_width, box_height, area = (
            int(value) for value in stats[index]
        )
        if (
            area < 20
            or box_width < 3
            or box_height < 3
            or box_width > 55
            or box_height > 45
        ):
            continue
        components.append(
            {
                "x": x + roi_left,
                "y": y + list_top,
                "w": box_width,
                "h": box_height,
                "area": area,
            }
        )

    groups: list[list[dict[str, int]]] = []
    for component in components:
        matching_groups = [
            group
            for group in groups
            if any(_boxes_are_near(component, member) for member in group)
        ]
        if not matching_groups:
            groups.append([component])
            continue
        primary = matching_groups[0]
        primary.append(component)
        for extra in matching_groups[1:]:
            primary.extend(extra)
            groups.remove(extra)

    markers: list[dict[str, int]] = []
    for group in groups:
        left = min(item["x"] for item in group)
        top = min(item["y"] for item in group)
        right = max(item["x"] + item["w"] for item in group)
        bottom = max(item["y"] + item["h"] for item in group)
        box_width = right - left
        box_height = bottom - top
        area = sum(item["area"] for item in group)
        if (
            left < int(round(width * 0.900))
            or box_width < 30
            or box_width > 52
            or box_height < 18
            or box_height > 42
            or area < 100
            or area > 1000
        ):
            continue
        markers.append(
            {"x": left, "y": top, "w": box_width, "h": box_height, "area": area}
        )

    markers.sort(key=lambda item: (item["y"], -item["area"]))
    deduplicated: list[dict[str, int]] = []
    for marker in markers:
        if deduplicated and abs(marker["y"] - deduplicated[-1]["y"]) < 60:
            if marker["area"] > deduplicated[-1]["area"]:
                deduplicated[-1] = marker
            continue
        deduplicated.append(marker)

    top_padding = max(10, int(round(height * 0.012)))
    card_tops = [max(list_top, marker["y"] - top_padding) for marker in deduplicated]
    cards: list[dict[str, Any]] = []
    for index, top in enumerate(card_tops):
        bottom = (
            card_tops[index + 1] - 8
            if index + 1 < len(card_tops)
            else list_bottom
        )
        card_height = bottom - top
        if card_height < int(round(height * 0.155)):
            continue
        marker = deduplicated[index]
        cards.append(
            {
                "label": "report-card",
                "x": int(round(width * 0.020)),
                "y": int(top),
                "w": int(round(width * 0.920)),
                "h": int(card_height),
                "box_cx": int(round(width * 0.480)),
                "box_cy": int(round(top + card_height / 2)),
                "click_x": int(round(width * 0.940)),
                "click_y": int(top),
                "marker_x": marker["x"],
                "marker_y": marker["y"],
            }
        )
    return cards


def inspect(
    screenshot: np.ndarray,
    compare: np.ndarray | None = None,
    main_template: np.ndarray | None = None,
) -> dict[str, Any]:
    height, width = screenshot.shape[:2]
    portrait = height > width
    if portrait:
        tab_y1, tab_y2 = 0.87, 0.925
        tab_ranges = {
            "all": (0.010, 0.260),
            "expand": (0.265, 0.495),
            "combat": (0.500, 0.745),
        }
        selected_threshold = 0.045
        selected_margin = 0.025
        report_band = crop_ratio(screenshot, 0.0, 0.87, 1.0, 0.925)
        report_band_dark_ratio = float((report_band.max(axis=2) < 80).mean())
    else:
        tab_y1, tab_y2 = 0.09, 0.155
        tab_ranges = {
            "all": (0.010, 0.152),
            "expand": (0.153, 0.295),
            "combat": (0.296, 0.439),
        }
        selected_threshold = 0.10
        selected_margin = 0.08
        report_band_dark_ratio = 1.0
    tab_gold = {
        key: gold_ratio(crop_ratio(screenshot, x1, tab_y1, x2, tab_y2))
        for key, (x1, x2) in tab_ranges.items()
    }
    selected_tab = max(tab_gold, key=tab_gold.get)
    report_tabs_visible = (
        tab_gold[selected_tab] >= selected_threshold
        and report_band_dark_ratio >= 0.75
    )
    combat_selected = (
        report_tabs_visible
        and selected_tab == "combat"
        and tab_gold["combat"] >= max(tab_gold["all"], tab_gold["expand"]) + selected_margin
    )

    center = crop_ratio(screenshot, 0.38, 0.15, 0.62, 0.85)
    left = crop_ratio(screenshot, 0.03, 0.15, 0.33, 0.85)
    right = crop_ratio(screenshot, 0.67, 0.15, 0.97, 0.85)
    sides = np.hstack((left, right))
    center_sharpness = laplacian_variance(center)
    side_sharpness = laplacian_variance(sides)
    left_edge = crop_ratio(screenshot, 0.0, 0.10, 0.04, 0.90)
    left_edge_gray = cv2.cvtColor(left_edge, cv2.COLOR_BGR2GRAY)
    left_edge_mean = float(left_edge_gray.mean())
    left_edge_sharpness = laplacian_variance(left_edge)
    close_gold = gold_ratio(crop_ratio(screenshot, 0.91, 0.10, 0.95, 0.17))
    dialog_gray = cv2.cvtColor(
        crop_ratio(screenshot, 0.37, 0.35, 0.63, 0.65), cv2.COLOR_BGR2GRAY
    )
    dialog_bright_ratio = float((dialog_gray >= 220).mean())
    center_modal = (
        side_sharpness < 20.0
        and center_sharpness >= 40.0
        and center_sharpness >= max(5.0, side_sharpness * 5.0)
    )
    announcement_overlay = (
        close_gold >= 0.06
        and left_edge_mean < 35.0
        and left_edge_sharpness < 5.0
        and center_sharpness >= 100.0
    )
    dialog_overlay = dialog_bright_ratio >= 0.25
    blocking_overlay = center_modal or announcement_overlay or dialog_overlay
    main_screen = portrait_main_screen_match(screenshot, main_template)
    exit_button_pink_ratio = (
        pink_ratio(crop_ratio(screenshot, 0.50, 0.50, 0.82, 0.59))
        if portrait
        else 0.0
    )
    exit_confirmation = (
        portrait
        and dialog_overlay
        # A badged icon lowers the template score, but the exit prompt must be a
        # high-confidence main screen before its Cancel button is tapped.
        and main_screen["matched"]
        and float(main_screen["score"]) >= 0.78
        and exit_button_pink_ratio >= 0.20
    )

    result: dict[str, Any] = {
        "width": int(width),
        "height": int(height),
        "orientation": "portrait" if portrait else "landscape",
        "reportTabsVisible": bool(report_tabs_visible),
        "selectedReportTab": selected_tab if report_tabs_visible else "",
        "combatTabSelected": bool(combat_selected),
        "blockingOverlay": bool(blocking_overlay),
        "mainScreen": bool(
            main_screen["matched"]
            and not blocking_overlay
            and not report_tabs_visible
            and not combat_selected
        ),
        "mainScreenScore": main_screen["score"],
        "mainScreenMatchLocation": main_screen["location"],
        "mainScreenEntryCenter": main_screen.get("center"),
        "mainScreenEntryTapRatio": main_screen.get("tapRatio"),
        "mainScreenEntryPositionMatched": main_screen["positionMatched"],
        "mainScreenMatchedScale": main_screen.get("scale", 1.0),
        "mainScreenMinScore": main_screen.get("minScore", MAIN_SCREEN_MIN_SCORE),
        "tabGoldRatios": {key: round(value, 6) for key, value in tab_gold.items()},
        "reportBandDarkRatio": round(report_band_dark_ratio, 6),
        "centerSharpness": round(center_sharpness, 4),
        "sideSharpness": round(side_sharpness, 4),
        "announcementOverlay": bool(announcement_overlay),
        "dialogOverlay": bool(dialog_overlay),
        "exitConfirmation": bool(exit_confirmation),
        "exitCancelTarget": {
            "x": int(round(width * 0.345)),
            "y": int(round(height * 0.546)),
        },
        "exitButtonPinkRatio": round(exit_button_pink_ratio, 6),
        "closeGoldRatio": round(close_gold, 6),
        "dialogBrightRatio": round(dialog_bright_ratio, 6),
        "leftEdgeMean": round(left_edge_mean, 4),
        "leftEdgeSharpness": round(left_edge_sharpness, 4),
        "reportCards": detect_report_cards(screenshot) if combat_selected else [],
    }
    if compare is not None:
        result["visualDifference"] = visual_difference(compare, screenshot)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--main-template", type=Path)
    parser.add_argument("--detect-expand", action="store_true")
    parser.add_argument("--down-template", type=Path)
    parser.add_argument("--up-template", type=Path)
    parser.add_argument("--template-scale", type=float, default=1.0)
    parser.add_argument("--include-up", action="store_true")
    args = parser.parse_args()

    screenshot = load_color(args.screenshot)
    compare = load_color(args.compare) if args.compare else None
    main_template = load_gray(args.main_template) if args.main_template else None
    result = inspect(screenshot, compare, main_template)
    if args.detect_expand:
        if args.down_template is None or args.up_template is None:
            parser.error("--detect-expand requires --down-template and --up-template")
        down_template = expand_detector.scale_template(
            expand_detector.load_gray(args.down_template), args.template_scale
        )
        up_template = expand_detector.scale_template(
            expand_detector.load_gray(args.up_template), args.template_scale
        )
        result["expandDetection"] = expand_detector.detect(
            screenshot,
            down_template,
            up_template,
            roi_x2=1.0,
            include_up=args.include_up,
        )
    result["screenshot"] = str(args.screenshot)
    if args.compare:
        result["compare"] = str(args.compare)
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
