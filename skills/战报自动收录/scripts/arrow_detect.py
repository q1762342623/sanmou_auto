"""战报列表「展开/收起」控件检测（1600x900 横屏实测）。

为什么不直接用上游 detect_expand_arrow 的横屏分支：
它的 ROI 默认只到 x=0.975，而 1600x900 的金色竖条在 x≈1540–1585（0.9625–0.9906），
会被 ROI 裁掉一半导致检不到。这里改成：
1. 在右边缘找**金色竖条**（颜色掩码 + 纵向闭运算 + 连通域）；
2. 用上游的 `arrow_template_score` 在整条 patch 上判方向（down/up）；
3. 点击点用模板**在实际竖条里定位出的箭头中心**，而不是固定偏移
   （竖条长度随卡片场次变化，固定 y+h-28 会落在箭头下方的留白上，点不中）。

down = 折叠（可点开），up = 已展开。
"""
from __future__ import annotations

import cv2
import numpy as np

import common


def _arrows():
    return common.load_engine("detect_expand_arrow")


def _load_template(name: str, scale: float) -> np.ndarray:
    """加载箭头模板：本地有 PNG 就用 PNG（方便现场替换），否则用内嵌数据。

    仓库不保存图片，模板以 zlib+base64 内嵌在 `engine/arrow_templates.py`，
    解码后的灰度数组与原 PNG 完全一致。
    """
    arrows = _arrows()
    path = common.TEMPLATES_DIR / name
    if path.is_file():
        return arrows.scale_template(arrows.load_gray(str(path)), scale)
    from engine import arrow_templates
    return arrows.scale_template(arrow_templates.load_gray(name), scale)


def _arrow_center(patch: np.ndarray, template: np.ndarray) -> tuple[int, int, float] | None:
    """在竖条 patch 内定位底部箭头 glyph，返回相对 patch 左上角的中心与匹配分数。"""
    if patch.size == 0 or template.size == 0:
        return None
    glyph = template[
        int(round(template.shape[0] * 0.72)): int(round(template.shape[0] * 0.95)),
        int(round(template.shape[1] * 0.16)): int(round(template.shape[1] * 0.84)),
    ]
    if glyph.size == 0:
        return None
    top = int(round(patch.shape[0] * 0.55))
    if patch.shape[0] - top >= glyph.shape[0]:
        search, offset_y = patch[top:, :], top
    else:
        search, offset_y = patch, 0
    if search.shape[0] < glyph.shape[0] or search.shape[1] < glyph.shape[1]:
        return None
    result = cv2.matchTemplate(search, glyph, cv2.TM_CCOEFF_NORMED)
    if result.size == 0:
        return None
    _, score, _, location = cv2.minMaxLoc(result)
    if score < 0.45:  # 匹配不可信时交回固定回退点
        return None
    center_x = int(round(location[0] + glyph.shape[1] / 2.0))
    center_y = int(round(offset_y + location[1] + glyph.shape[0] / 2.0))
    return center_x, center_y, float(score)


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
    down_template = _load_template("expand_down.png", scale)
    up_template = _load_template("expand_up.png", scale)
    down_threshold = float(expand["downThreshold"])
    min_margin = float(expand["minMargin"])
    min_width = int(expand.get("minWidth", 14))
    max_width = int(expand.get("maxWidth", 56))
    min_height = int(expand.get("minHeight", 150))
    max_height = int(expand.get("maxHeight", 240))
    # 竖条必须贴着屏幕最右侧：卡片自己的金色边框会落在 x≈1440（0.90 宽），
    # 它的高度也常在 150~170，误当成展开条去点就会点进战报详情页。
    right_edge = int(round(width * float(expand.get("rightEdgeRatio", 0.945))))
    bottom_inset = int(expand.get("clickBottomInset", 26))

    strips = []
    for index in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[index])
        if w < min_width or w > max_width or h < min_height or h > max_height:
            continue
        if x + left < right_edge:
            continue
        patch = cv2.cvtColor(roi[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
        down_score = arrows.arrow_template_score(patch, down_template)
        up_score = arrows.arrow_template_score(patch, up_template)
        label = ""
        if down_score >= down_threshold and down_score - up_score >= min_margin:
            label = "down"
        elif up_score >= down_threshold and up_score - down_score >= min_margin:
            label = "up"
        template = up_template if label == "up" else down_template
        # 点击点固定取「竖条水平中心 + 距底边 bottom_inset」：
        # 这里正好是双 V 箭头，且无论卡片被列表顶到哪里都仍在竖条内部。
        click_x = int(round(left + x + w / 2.0))
        bottom_point = top + y + h - bottom_inset
        center = _arrow_center(patch, template)
        if center and center[2] >= 0.55:
            click_y = min(max(top + y + center[1],
                              top + y + int(round(h * 0.62))),
                          bottom_point)
            click_source, click_score = "arrow", round(center[2], 4)
        else:
            # 模板没定位到箭头时退回竖条底部固定点（双 V 箭头处），绝不落到卡片本体
            click_y = bottom_point
            click_source = "fallback"
            click_score = round(float(up_score if label == "up" else down_score), 4)
        strips.append({
            "x": x + left, "y": y + top, "w": w, "h": h, "area": area,
            "downScore": round(float(down_score), 4),
            "upScore": round(float(up_score), 4),
            "label": label,
            "clickX": int(click_x),
            "clickY": int(click_y),
            "clickSource": click_source,
            "clickScore": click_score,
        })
    strips.sort(key=lambda item: (item["y"], item["x"]))
    return {"strips": strips,
            "targets": [item for item in strips if item["label"] == "down"],
            "roi": [left, top, right, bottom]}
