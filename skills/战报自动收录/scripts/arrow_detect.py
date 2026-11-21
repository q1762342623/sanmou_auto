"""战报列表「展开/收起」控件检测（1600x900 横屏实测）。

为什么不直接用上游 detect_expand_arrow 的横屏分支：
它的 ROI 默认只到 x=0.975，而 1600x900 的金色竖条在 x≈1540–1585（0.9625–0.9906），
会被 ROI 裁掉一半导致检不到。这里改成：
1. 在右边缘找**金色竖条**（颜色掩码 + 纵向闭运算 + 连通域）；
2. 用上游的 `arrow_template_score` 在整条 patch 上判方向（down/up）；
3. 点击点取竖条底部箭头位置（x+w/2, y+h-28）。

down = 折叠（可点开），up = 已展开。
"""
from __future__ import annotations

import cv2
import numpy as np

import common


def _arrows():
    return common.load_engine("detect_expand_arrow")


def detect_strips(image, config: dict) -> dict:
    expand = config["expand"]
    height, width = image.shape[:2]
    left = int(width * float(expand["roiX1"]))
    right = int(width * float(expand["roiX2"]))
    top = int(height * float(expand["roiY1"]))
    bottom = int(height * float(expand["roiY2"]))
    roi = image[top:bottom, left:right]
    if roi.size == 0:
        return {"strips": [], "targets": [], "roi": [left, top, right, bottom]}

    blue, green, red = cv2.split(roi)
    red16 = red.astype(np.int16)
    blue16 = blue.astype(np.int16)
    mask = ((red >= 100) & (green >= 65) & (blue <= 165) & ((red16 - blue16) >= 15))
    mask = mask.astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 1), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)

    arrows = _arrows()
    scale = float(expand["templateScale"])
    down_template = arrows.scale_template(
        arrows.load_gray(str(common.TEMPLATES_DIR / "expand_down.png")), scale)
    up_template = arrows.scale_template(
        arrows.load_gray(str(common.TEMPLATES_DIR / "expand_up.png")), scale)
    down_threshold = float(expand["downThreshold"])
    min_margin = float(expand["minMargin"])
    min_width = int(expand.get("minWidth", 14))
    max_width = int(expand.get("maxWidth", 90))
    min_height = int(expand.get("minHeight", 150))
    max_height = int(round((bottom - top) * 0.55))

    strips = []
    for index in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[index])
        if w < min_width or w > max_width or h < min_height or h > max_height:
            continue
        patch = cv2.cvtColor(roi[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
        down_score = arrows.arrow_template_score(patch, down_template)
        up_score = arrows.arrow_template_score(patch, up_template)
        label = ""
        if down_score >= down_threshold and down_score - up_score >= min_margin:
            label = "down"
        elif up_score >= down_threshold and up_score - down_score >= min_margin:
            label = "up"
        strips.append({
            "x": x + left, "y": y + top, "w": w, "h": h, "area": area,
            "downScore": round(float(down_score), 4),
            "upScore": round(float(up_score), 4),
            "label": label,
            "clickX": int(round(left + x + w / 2)),
            "clickY": int(round(top + y + h - 28)),
        })
    strips.sort(key=lambda item: (item["y"], item["x"]))
    return {"strips": strips,
            "targets": [item for item in strips if item["label"] == "down"],
            "roi": [left, top, right, bottom]}
