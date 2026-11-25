"""页面就绪快速轮询：用「截图小区域指纹」替代固定 `time.sleep`。

背景（2026-11-23 实测统计，63 轮自动拉线）：
- 「回城就位」平均 162s、「判定战场」132s、「选择歇兵城池」287s、
  「定位最近可调动据点」86s —— 其中很大一块是「点一下 → 固定 sleep 2~3s
  → 再整屏 OCR 判定」，而真实页面切换往往只要 0.3~1.2s。
- 游戏内页面切换（同盟页签、城池列表、回城、面板弹出）都有稳定的
  「静止 → 动画 → 静止」过程，用 32x18 灰度缩略图的帧间差异就能判定，
  比整屏 OCR 便宜 1~2 个数量级（截图 ~0.25s，缩略图比对 <1ms）。

对外只暴露四个函数：

    wait_settle(adb, box=None, timeout=2.0, min_wait=0.2) -> bool
    wait_until(adb, pred, timeout=2.5, interval=0.2) -> bool
    tap_settle(adb, point, box=None, timeout=2.0, min_wait=0.25) -> bool
    wait_text(adb, box, keywords, timeout=3.0, scale=2.0) -> tuple[bool, str]

全部带兜底：环境变量 `SANMOU_FASTWAIT=0` 时退化为原来的固定 sleep
（排查兼容性问题时用），异常时也不会抛到业务层。
"""
from __future__ import annotations

import os
import time

from PIL import Image

from . import ocrengine, vision

GRID = (32, 18)          # 指纹缩略图尺寸（够判「画面变了没」）
DEFAULT_INTERVAL = 0.18  # 轮询间隔（截图本身 ~0.25s，太密没意义）
STABLE_QUIET = 0.8       # 相邻两帧平均灰度差 < 该值视为「静止」
STABLE_FRAMES = 2        # 连续多少对「静止」算稳定


def enabled() -> bool:
    return os.environ.get("SANMOU_FASTWAIT", "1").strip() != "0"


def signature(img: Image.Image, box=None, grid: tuple[int, int] = GRID) -> bytes:
    """裁出 box（可选）后转灰度缩略图，作为「这一帧长什么样」的指纹。"""
    if box is not None:
        box = (max(0, int(box[0])), max(0, int(box[1])),
               min(img.width, int(box[2])), min(img.height, int(box[3])))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            box = None
    target = img.crop(box) if box else img
    return target.convert("L").resize(grid, Image.BILINEAR).tobytes()


def sig_diff(a: bytes, b: bytes) -> float:
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _grab(adb, box=None) -> tuple[Image.Image | None, bytes]:
    try:
        img = adb.screencap()
    except Exception:                 # noqa: BLE001 - 截图失败交给上层兜底
        return None, b""
    return img, signature(img, box)


def wait_settle(adb, box=None, timeout: float = 2.0, min_wait: float = 0.2,
                interval: float = DEFAULT_INTERVAL,
                quiet: float = STABLE_QUIET,
                stable_frames: int = STABLE_FRAMES) -> bool:
    """等画面「动完并静止」；返回是否在 timeout 内稳定（False = 超时）。"""
    if not enabled():
        time.sleep(max(min_wait, min(timeout, 0.8)))
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    start = time.monotonic()
    prev, prev_sig = _grab(adb, box)
    if prev is None:
        time.sleep(max(min_wait, 0.3))
        return False
    steady = 0
    while True:
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, interval))
        cur, cur_sig = _grab(adb, box)
        if cur is None:
            return False
        diff = sig_diff(prev_sig, cur_sig)
        prev_sig = cur_sig
        if diff < quiet:
            steady += 1
            if steady >= stable_frames and (time.monotonic() - start) >= min_wait:
                return True
        else:
            steady = 0


def wait_until(adb, pred, timeout: float = 2.5,
               interval: float = DEFAULT_INTERVAL,
               min_wait: float = 0.0, box=None) -> bool:
    """轮询直到 `pred(img)` 为真；pred 抛异常按 False 处理。"""
    if not enabled():
        time.sleep(max(min_wait, min(timeout, 0.8)))
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    start = time.monotonic()
    while True:
        img, _sig = _grab(adb, box)
        if img is not None:
            try:
                if pred(img):
                    if time.monotonic() - start >= min_wait:
                        return True
            except Exception:             # noqa: BLE001
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, interval))


def tap_settle(adb, point: tuple[int, int], box=None, timeout: float = 2.0,
               min_wait: float = 0.25) -> bool:
    """点一下然后等画面稳定（替代「tap + 固定 sleep」）。"""
    adb.tap(int(point[0]), int(point[1]))
    return wait_settle(adb, box=box, timeout=timeout, min_wait=min_wait)


def wait_text(adb, box, keywords: tuple[str, ...], timeout: float = 3.0,
              scale: float = 2.0, interval: float = 0.25,
              min_wait: float = 0.2) -> tuple[bool, str]:
    """轮询「某个区域 OCR 出现指定关键字」；帧没变就跳过 OCR 省钱。

    返回 (是否命中, 最后一次读到的文本)。
    """
    if not enabled():
        time.sleep(max(min_wait, min(timeout, 0.9)))
        return False, ""
    deadline = time.monotonic() + max(0.0, timeout)
    start = time.monotonic()
    last_sig = b""
    text = ""
    while True:
        img, sig = _grab(adb, box)
        if img is not None and sig != last_sig:
            last_sig = sig
            try:
                text = ocrengine.ocr_text(vision.crop(img, box), scale)
            except Exception:             # noqa: BLE001
                text = ""
            flat = text.replace(" ", "")
            if any(k in flat for k in keywords) and \
                    time.monotonic() - start >= min_wait:
                return True, text
        if time.monotonic() >= deadline:
            return False, text
        time.sleep(max(0.05, interval))


__all__ = ["enabled", "signature", "sig_diff", "wait_settle", "wait_until",
           "tap_settle", "wait_text"]
