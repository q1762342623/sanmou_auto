"""Detect collapsed expand controls in a game client screenshot.

The safe target is the full right-side gold expand strip, not the small arrow
glyph alone. The arrow icon is visually similar to nearby share/map icons, so
the detector first proves that a full strip exists, then classifies that strip
as down/up by comparing both templates at the same location.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def load_image(path: Path, flags: int) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, flags)
    if image is None:
        raise SystemExit(f"failed to read image: {path}")
    return image


def load_gray(path: Path) -> np.ndarray:
    return load_image(path, cv2.IMREAD_GRAYSCALE)


def load_color(path: Path) -> np.ndarray:
    return load_image(path, cv2.IMREAD_COLOR)


def scale_template(template: np.ndarray, scale: float) -> np.ndarray:
    if scale <= 0:
        raise ValueError("template scale must be positive")
    if abs(scale - 1.0) < 1e-9:
        return template
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
    return cv2.resize(template, None, fx=scale, fy=scale, interpolation=interpolation)


def crop_roi(
    image: np.ndarray,
    x1_ratio: float,
    x2_ratio: float,
    y1_ratio: float,
    y2_ratio: float,
) -> tuple[np.ndarray, tuple[int, int]]:
    h, w = image.shape[:2]
    x1 = max(0, min(w - 1, int(round(w * x1_ratio))))
    x2 = max(x1 + 1, min(w, int(round(w * x2_ratio))))
    y1 = max(0, min(h - 1, int(round(h * y1_ratio))))
    y2 = max(y1 + 1, min(h, int(round(h * y2_ratio))))
    return image[y1:y2, x1:x2], (x1, y1)


def overlap_ratio(a: dict[str, Any], b: dict[str, Any]) -> float:
    ax1, ay1, ax2, ay2 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1, bx2, by2 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area = min((ax2 - ax1) * (ay2 - ay1), (bx2 - bx1) * (by2 - by1))
    return inter / area if area else 0.0


def template_result(image: np.ndarray, template: np.ndarray) -> np.ndarray:
    if image.shape[0] < template.shape[0] or image.shape[1] < template.shape[1]:
        return np.empty((0, 0), dtype=np.float32)
    return cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)


def iter_local_maxima(
    result: np.ndarray,
    template: np.ndarray,
    threshold: float,
    label: str,
) -> list[dict[str, Any]]:
    if result.size == 0:
        return []

    h, w = template.shape[:2]
    kernel_w = max(5, int(round(w * 0.60)))
    kernel_h = max(5, int(round(h * 0.35)))
    kernel = np.ones((kernel_h, kernel_w), dtype=np.uint8)
    maxima = result == cv2.dilate(result, kernel)
    ys, xs = np.where(maxima & (result >= threshold))

    matches: list[dict[str, Any]] = []
    for x, y in zip(xs.tolist(), ys.tolist()):
        score = float(result[y, x])
        matches.append(
            {
                "label": label,
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
                "box_cx": int(round(x + w / 2)),
                "box_cy": int(round(y + h / 2)),
                "score": round(score, 4),
            }
        )
    return sorted(matches, key=lambda item: item["score"], reverse=True)


def nms(matches: list[dict[str, Any]], overlap: float) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for match in sorted(matches, key=lambda item: item["score"], reverse=True):
        if all(overlap_ratio(match, existing) < overlap for existing in kept):
            kept.append(match)
    return sorted(kept, key=lambda item: (item["box_cy"], -item["score"]))


def best_at_center(
    result: np.ndarray,
    template: np.ndarray,
    center_x: float,
    center_y: float,
) -> tuple[float, int, int]:
    if result.size == 0:
        return -1.0, -1, -1
    h, w = template.shape[:2]
    x = int(round(center_x - (w / 2)))
    y = int(round(center_y - (h / 2)))
    x1 = max(0, x - 4)
    x2 = min(result.shape[1], x + 5)
    y1 = max(0, y - 8)
    y2 = min(result.shape[0], y + 9)
    if y1 >= y2 or x1 >= x2:
        return -1.0, -1, -1
    _, maxv, _, maxloc = cv2.minMaxLoc(result[y1:y2, x1:x2])
    return float(maxv), int(x1 + maxloc[0]), int(y1 + maxloc[1])


def group_by_center(matches: list[dict[str, Any]], max_dx: int, max_dy: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for match in sorted(matches, key=lambda item: item["score"], reverse=True):
        cx = float(match["box_cx"])
        cy = float(match["box_cy"])
        placed = False
        for group in groups:
            gx = sum(float(item["box_cx"]) for item in group) / len(group)
            gy = sum(float(item["box_cy"]) for item in group) / len(group)
            if abs(cx - gx) <= max_dx and abs(cy - gy) <= max_dy:
                group.append(match)
                placed = True
                break
        if not placed:
            groups.append([match])
    return groups


def gold_frame_metrics(color_roi: np.ndarray, x: int, y: int, w: int, h: int) -> dict[str, Any]:
    if x < 0 or y < 0 or x + w > color_roi.shape[1] or y + h > color_roi.shape[0]:
        return {"valid": False, "both_rows": 0, "gold_ratio": 0.0, "lower_gold_ratio": 0.0}

    patch = color_roi[y : y + h, x : x + w]
    b, g, r = cv2.split(patch)
    gold = (
        (r >= 95)
        & (g >= 60)
        & (b <= 115)
        & ((r.astype(np.int16) - b.astype(np.int16)) >= 28)
        & (r.astype(np.int16) >= g.astype(np.int16) - 8)
    )
    edge = max(4, min(8, w // 5))
    left_rows = gold[:, :edge].sum(axis=1)
    right_rows = gold[:, -edge:].sum(axis=1)
    both_rows = int(((left_rows >= 1) & (right_rows >= 1)).sum())
    lower = gold[int(round(h * 0.70)) :, :]
    return {
        "valid": True,
        "both_rows": both_rows,
        "gold_ratio": round(float(gold.mean()), 4),
        "lower_gold_ratio": round(float(lower.mean()), 4) if lower.size else 0.0,
    }


def gold_mask(image: np.ndarray) -> np.ndarray:
    blue, green, red = cv2.split(image)
    red16 = red.astype(np.int16)
    green16 = green.astype(np.int16)
    blue16 = blue.astype(np.int16)
    return (
        (red >= 95)
        & (green >= 60)
        & (blue <= 115)
        & ((red16 - blue16) >= 28)
        & (red16 >= green16 - 8)
    )


def arrow_template_score(patch: np.ndarray, template: np.ndarray) -> float:
    """Match the arrow/footer while ignoring responsive space above it."""
    glyph = template[
        int(round(template.shape[0] * 0.72)) : int(round(template.shape[0] * 0.95)),
        int(round(template.shape[1] * 0.16)) : int(round(template.shape[1] * 0.84)),
    ]
    search = patch[
        int(round(patch.shape[0] * 0.62)) :,
        int(round(patch.shape[1] * 0.08)) : int(round(patch.shape[1] * 0.92)),
    ]
    if glyph.size == 0 or search.size == 0:
        return -1.0

    base_scale = patch.shape[1] / max(1, template.shape[1])
    best = -1.0
    for width_adjustment in (0.85, 1.0, 1.15, 1.30):
        for height_adjustment in (0.85, 1.0, 1.15, 1.30):
            width_scale = base_scale * width_adjustment
            height_scale = base_scale * height_adjustment
            target_width = max(2, int(round(glyph.shape[1] * width_scale)))
            target_height = max(2, int(round(glyph.shape[0] * height_scale)))
            if target_width > search.shape[1] or target_height > search.shape[0]:
                continue
            resized = cv2.resize(
                glyph,
                (target_width, target_height),
                interpolation=cv2.INTER_CUBIC
                if max(width_scale, height_scale) > 1.0
                else cv2.INTER_AREA,
            )
            result = template_result(search, resized)
            if result.size:
                best = max(best, float(cv2.minMaxLoc(result)[1]))
    return best


def detect_responsive_strips(
    color_roi: np.ndarray,
    down_template: np.ndarray,
    up_template: np.ndarray,
    *,
    threshold: float,
    min_margin: float,
    min_frame_rows: int,
    include_up: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect portrait strips whose middle grows without scaling the arrow glyph."""
    mask = gold_mask(color_roi).astype(np.uint8) * 255
    close_height = max(9, int(round(color_roi.shape[0] * 0.009)))
    connected = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, close_height)),
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(connected)

    template_width = max(down_template.shape[1], up_template.shape[1])
    minimum_width = max(12, int(round(template_width * 0.55)))
    maximum_width = max(minimum_width + 1, int(round(template_width * 1.80)))
    minimum_height = max(70, int(round(min_frame_rows * 0.70)))
    maximum_height = max(minimum_height + 1, int(round(color_roi.shape[0] * 0.45)))
    minimum_right_edge = int(round(color_roi.shape[1] * 0.70))

    targets: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index in range(1, count):
        x, y, w, h, _ = (int(value) for value in stats[index])
        if (
            w < minimum_width
            or w > maximum_width
            or h < minimum_height
            or h > maximum_height
            or (x + w) < minimum_right_edge
        ):
            continue
        metrics = gold_frame_metrics(color_roi, x, y, w, h)
        if not metrics["valid"] or metrics["both_rows"] < minimum_height:
            continue

        patch = cv2.cvtColor(color_roi[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY)
        down_score = arrow_template_score(patch, down_template)
        up_score = arrow_template_score(patch, up_template)
        label = "unknown"
        if down_score >= threshold and (down_score - up_score) >= min_margin:
            label = "down"
        elif include_up and up_score >= threshold and (up_score - down_score) >= min_margin:
            label = "up"

        candidate = make_target(
            label=label,
            x=x,
            y=y,
            w=w,
            h=h,
            down_score=down_score,
            up_score=up_score,
            metrics=metrics,
        )
        if label == "unknown":
            candidate["reject_reason"] = "ambiguous_direction"
            rejected.append(candidate)
        else:
            targets.append(candidate)
    return targets, rejected


def absolutize(match: dict[str, Any], off_x: int, off_y: int) -> dict[str, Any]:
    out = dict(match)
    out["x"] += off_x
    out["y"] += off_y
    out["box_cx"] += off_x
    out["box_cy"] += off_y
    if "cx" in out:
        out["cx"] += off_x
    if "cy" in out:
        out["cy"] += off_y
    if "click_x" in out:
        out["click_x"] += off_x
    if "click_y" in out:
        out["click_y"] += off_y
    return out


def make_target(
    *,
    label: str,
    x: int,
    y: int,
    w: int,
    h: int,
    down_score: float,
    up_score: float,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    click_x = int(round(x + (w * 0.50)))
    click_y = int(round(y + (h * 0.86)))
    score = down_score if label == "down" else up_score if label == "up" else max(down_score, up_score)
    return {
        "label": label,
        "x": int(x),
        "y": int(y),
        "w": int(w),
        "h": int(h),
        "box_cx": int(round(x + (w / 2))),
        "box_cy": int(round(y + (h / 2))),
        "cx": click_x,
        "cy": click_y,
        "click_x": click_x,
        "click_y": click_y,
        "score": round(float(score), 4),
        "down_score": round(float(down_score), 4),
        "up_score": round(float(up_score), 4),
        "margin": round(float(down_score - up_score), 4),
        "gold_frame_rows": metrics["both_rows"],
        "gold_ratio": metrics["gold_ratio"],
        "lower_gold_ratio": metrics["lower_gold_ratio"],
    }


def draw_debug(
    screenshot: np.ndarray,
    path: Path,
    roi: dict[str, int],
    targets: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> None:
    debug = screenshot.copy()
    cv2.rectangle(
        debug,
        (roi["x"], roi["y"]),
        (roi["x"] + roi["w"], roi["y"] + roi["h"]),
        (80, 80, 80),
        1,
    )
    for item in rejected:
        x, y, w, h = item["x"], item["y"], item["w"], item["h"]
        cv2.rectangle(debug, (x, y), (x + w, y + h), (0, 0, 180), 1)
    for target in targets:
        x, y, w, h = target["x"], target["y"], target["w"], target["h"]
        color = (0, 220, 0) if target["label"] == "down" else (255, 120, 0)
        cv2.rectangle(debug, (x, y), (x + w, y + h), color, 2)
        cv2.drawMarker(
            debug,
            (target["click_x"], target["click_y"]),
            color,
            markerType=cv2.MARKER_CROSS,
            markerSize=18,
            thickness=2,
        )
        cv2.putText(
            debug,
            f"{target['label']} {target['score']:.2f}",
            (x - 80, max(20, y + 20)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )
    ok, encoded = cv2.imencode(".png", debug)
    if ok:
        encoded.tofile(str(path))


def detect(
    screenshot_color: np.ndarray,
    down_template: np.ndarray,
    up_template: np.ndarray,
    *,
    threshold: float = 0.72,
    up_threshold: float = 0.72,
    min_margin: float = 0.06,
    overlap: float = 0.35,
    roi_x1: float = 0.880,
    roi_x2: float = 0.975,
    roi_y1: float = 0.080,
    roi_y2: float = 0.960,
    offset_x: int = 0,
    offset_y: int = 0,
    min_frame_rows: int = 150,
    include_up: bool = False,
) -> dict[str, Any]:
    screenshot_gray = cv2.cvtColor(screenshot_color, cv2.COLOR_BGR2GRAY)
    roi_gray, (off_x, off_y) = crop_roi(screenshot_gray, roi_x1, roi_x2, roi_y1, roi_y2)
    roi_color, _ = crop_roi(screenshot_color, roi_x1, roi_x2, roi_y1, roi_y2)
    off_x += offset_x
    off_y += offset_y

    if screenshot_color.shape[0] > screenshot_color.shape[1]:
        responsive_targets, responsive_rejected = detect_responsive_strips(
            roi_color,
            down_template,
            up_template,
            threshold=threshold,
            min_margin=min_margin,
            min_frame_rows=min_frame_rows,
            include_up=include_up,
        )
        targets = sorted(
            (absolutize(item, off_x, off_y) for item in responsive_targets),
            key=lambda item: (item["y"], item["label"]),
        )
        rejected = [absolutize(item, off_x, off_y) for item in responsive_rejected]
        roi_out = {
            "x": off_x,
            "y": off_y,
            "w": int(roi_gray.shape[1]),
            "h": int(roi_gray.shape[0]),
        }
        return {
            "mode": "responsive-strip",
            "roi": roi_out,
            "threshold": threshold,
            "up_threshold": up_threshold,
            "min_margin": min_margin,
            "down_raw": [],
            "up_raw": [],
            "rejected": rejected,
            "targets": targets,
        }

    down_result = template_result(roi_gray, down_template)
    up_result = template_result(roi_gray, up_template)
    down_peaks = nms(iter_local_maxima(down_result, down_template, threshold, "down"), overlap)
    up_peaks = nms(iter_local_maxima(up_result, up_template, up_threshold, "up"), overlap)

    targets: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for group in group_by_center(down_peaks + up_peaks, max_dx=10, max_dy=16):
        anchor = max(group, key=lambda item: float(item["score"]))
        center_x = float(anchor["box_cx"])
        center_y = float(anchor["box_cy"])
        down_score, down_x, down_y = best_at_center(down_result, down_template, center_x, center_y)
        up_score, up_x, up_y = best_at_center(up_result, up_template, center_x, center_y)
        if down_score >= up_score:
            w, h = down_template.shape[1], down_template.shape[0]
            x, y = down_x, down_y
        else:
            w, h = up_template.shape[1], up_template.shape[0]
            x, y = up_x, up_y
        metrics = gold_frame_metrics(roi_color, x, y, w, h)
        candidate = make_target(
            label="unknown", x=x, y=y, w=w, h=h,
            down_score=down_score, up_score=up_score, metrics=metrics,
        )
        if not metrics["valid"] or metrics["both_rows"] < min_frame_rows:
            candidate["reject_reason"] = "weak_gold_frame"
            rejected.append(absolutize(candidate, off_x, off_y))
            continue
        if down_score >= threshold and (down_score - up_score) >= min_margin:
            targets.append(make_target(
                label="down", x=x, y=y, w=w, h=h,
                down_score=down_score, up_score=up_score, metrics=metrics,
            ))
        elif include_up and up_score >= up_threshold and (up_score - down_score) >= min_margin:
            targets.append(make_target(
                label="up", x=x, y=y, w=w, h=h,
                down_score=down_score, up_score=up_score, metrics=metrics,
            ))
        else:
            candidate["reject_reason"] = "ambiguous_direction"
            rejected.append(absolutize(candidate, off_x, off_y))

    targets = sorted(
        (absolutize(item, off_x, off_y) for item in targets),
        key=lambda item: (item["y"], item["label"]),
    )
    roi_out = {"x": off_x, "y": off_y, "w": int(roi_gray.shape[1]), "h": int(roi_gray.shape[0])}
    return {
        "mode": "full-strip",
        "roi": roi_out,
        "threshold": threshold,
        "up_threshold": up_threshold,
        "min_margin": min_margin,
        "down_raw": [absolutize(match, off_x, off_y) for match in down_peaks],
        "up_raw": [absolutize(match, off_x, off_y) for match in up_peaks],
        "rejected": rejected,
        "targets": targets,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--down-template", type=Path, required=True)
    parser.add_argument("--up-template", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.72)
    parser.add_argument("--up-threshold", type=float, default=0.72)
    parser.add_argument("--min-margin", type=float, default=0.06)
    parser.add_argument("--overlap", type=float, default=0.35)
    parser.add_argument("--roi-x1", type=float, default=0.880)
    parser.add_argument("--roi-x2", type=float, default=0.975)
    parser.add_argument("--roi-y1", type=float, default=0.080)
    parser.add_argument("--roi-y2", type=float, default=0.960)
    parser.add_argument("--offset-x", type=int, default=0)
    parser.add_argument("--offset-y", type=int, default=0)
    parser.add_argument("--min-frame-rows", type=int, default=150)
    parser.add_argument("--template-scale", type=float, default=1.0)
    parser.add_argument("--include-up", action="store_true")
    parser.add_argument("--debug-image", type=Path)
    args = parser.parse_args()

    screenshot_color = load_color(args.screenshot)
    down_template = scale_template(load_gray(args.down_template), args.template_scale)
    up_template = scale_template(load_gray(args.up_template), args.template_scale)
    output = detect(
        screenshot_color, down_template, up_template,
        threshold=args.threshold, up_threshold=args.up_threshold,
        min_margin=args.min_margin, overlap=args.overlap,
        roi_x1=args.roi_x1, roi_x2=args.roi_x2,
        roi_y1=args.roi_y1, roi_y2=args.roi_y2,
        offset_x=args.offset_x, offset_y=args.offset_y,
        min_frame_rows=args.min_frame_rows, include_up=args.include_up,
    )
    output["screenshot"] = str(args.screenshot)
    output["template_scale"] = args.template_scale
    if args.debug_image:
        draw_debug(screenshot_color, args.debug_image, output["roi"], output["targets"], output["rejected"])
        output["debug_image"] = str(args.debug_image)

    print(json.dumps(output, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
