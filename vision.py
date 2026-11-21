"""图像工具：低画质落盘、裁剪、放大。避免把大图读进模型上下文。"""
from __future__ import annotations

import os

from PIL import Image

from . import config


def save_jpg(img: Image.Image, path: str, max_width: int = 1000,
             quality: int = 70) -> str:
    out = img
    if out.width > max_width:
        h = round(out.height * max_width / out.width)
        out = out.resize((max_width, h), Image.LANCZOS)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    out.save(path, "JPEG", quality=quality, optimize=True)
    return path


def crop(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    return img.crop(box)


def upscale(img: Image.Image, factor: float = 2.0) -> Image.Image:
    if factor == 1.0:
        return img
    return img.resize((round(img.width * factor), round(img.height * factor)),
                      Image.LANCZOS)


def shot_path(name: str) -> str:
    os.makedirs(config.SHOT_DIR, exist_ok=True)
    return os.path.join(config.SHOT_DIR, name)
