"""RapidOCR 封装（离线中文 OCR）。

依赖: pip install rapidocr-onnxruntime
首次调用会加载 onnx 模型（约 1-2 秒），之后复用单例。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from PIL import Image

_LOCK = threading.Lock()
_ENGINE = None


def _engine():
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR
            _ENGINE = RapidOCR()
        return _ENGINE


@dataclass
class Span:
    """一段 OCR 结果。"""
    text: str
    cx: float          # 中心点（相对传入图像的像素坐标）
    cy: float
    score: float

    @property
    def left(self) -> float:
        return self.cx

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.text}({self.cx:.0f},{self.cy:.0f})"


def ocr(img: Image.Image, scale: float = 2.0) -> list[Span]:
    """识别整张图，返回按 y 再 x 排序的文本片段。

    scale: 传入前放大倍数。游戏内字号偏小，2 倍放大明显更准。
    """
    import numpy as np

    if scale != 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    arr = np.array(img.convert("RGB"))[:, :, ::-1]  # RGB -> BGR
    result, _ = _engine()(arr)
    spans: list[Span] = []
    for item in result or []:
        box, text, score = item[0], item[1], float(item[2])
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        spans.append(Span(
            text=str(text).strip(),
            cx=(min(xs) + max(xs)) / 2 / scale,
            cy=(min(ys) + max(ys)) / 2 / scale,
            score=score,
        ))
    spans.sort(key=lambda s: (round(s.cy / 12), s.cx))
    return spans


def ocr_text(img: Image.Image, scale: float = 2.0, sep: str = "") -> str:
    return sep.join(s.text for s in ocr(img, scale))


def ocr_tiled(img: Image.Image, box: tuple[int, int, int, int],
              tile: tuple[int, int] = (400, 260), overlap: int = 40,
              scale: float = 2.0) -> list[Span]:
    """把大区域切成小块分别 OCR。

    RapidOCR 会把长边压到 ~736px，直接喂大地图会让小字全部丢失，
    因此地图类整屏识别必须切块。
    """
    x1, y1, x2, y2 = box
    tw, th = tile
    spans: list[Span] = []
    y = y1
    while y < y2:
        x = x1
        while x < x2:
            sub = img.crop((x, y, min(x + tw, x2), min(y + th, y2)))
            for s in ocr(sub, scale):
                spans.append(Span(text=s.text, cx=s.cx + x, cy=s.cy + y,
                                  score=s.score))
            x += tw - overlap
        y += th - overlap
    return spans


def ocr_rows(img: Image.Image, scale: float = 2.0,
             tol: float = 14.0) -> list[list[Span]]:
    """按行聚合：同一 y 带内的片段归为一行，行内按 x 排序。"""
    rows: list[list[Span]] = []
    for s in ocr(img, scale):
        if rows and abs(rows[-1][0].cy - s.cy) <= tol:
            rows[-1].append(s)
        else:
            rows.append([s])
    for row in rows:
        row.sort(key=lambda s: s.cx)
    return rows


def ocr_lines(img: Image.Image, scale: float = 2.0) -> list[str]:
    return ["".join(s.text for s in row) for row in ocr_rows(img, scale)]
