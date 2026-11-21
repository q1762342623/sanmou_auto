"""adb 封装：只做「外部截图 + 模拟触摸」，不注入、不读内存、不改包。"""
from __future__ import annotations

import io
import struct
import subprocess
import time

from PIL import Image

from . import config


def decode_raw_screencap(data: bytes) -> Image.Image | None:
    """解码 `adb exec-out screencap`（原始 RGBA，比 `-p` 快 3 倍）。

    头部布局：width / height / format / colorspace，共 16 字节（老设备 12 字节），
    用「总长 - w*h*4」直接算头部长度，避免版本差异。
    """
    if len(data) < 12:
        return None
    try:
        w, h, fmt = struct.unpack_from("<III", data, 0)
    except struct.error:
        return None
    if not (0 < w <= 5000 and 0 < h <= 5000):
        return None
    n = w * h * 4
    off = len(data) - n
    if off not in (12, 16):
        return None
    mode = {1: "RGBA", 2: "RGBX", 3: "RGB", 4: "BGRA"}.get(fmt)
    if not mode:
        return None
    try:
        img = Image.frombytes(mode, (w, h), data[off:off + n])
    except (ValueError, TypeError):
        return None
    return img.convert("RGB")


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, adb_path: str = config.ADB_PATH, device: str = config.DEVICE):
        self.adb_path = adb_path
        self.device = device

    # -- 基础 ---------------------------------------------------------------
    def _run(self, *args: str, binary: bool = False, timeout: int = 30):
        cmd = [self.adb_path, "-s", self.device, *args]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as e:
            raise AdbError(f"找不到 adb: {self.adb_path}") from e
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 超时: {' '.join(args)}") from e
        if r.returncode != 0 and not binary:
            err = r.stderr.decode("utf-8", "ignore").strip()
            if err:
                raise AdbError(err)
        return r.stdout if binary else r.stdout.decode("utf-8", "ignore")

    def shell(self, *args: str, timeout: int = 30) -> str:
        return self._run("shell", *args, timeout=timeout)

    def devices(self) -> str:
        return subprocess.run([self.adb_path, "devices"], capture_output=True,
                              check=False).stdout.decode("utf-8", "ignore")

    def ensure_online(self) -> None:
        if self.device not in self.devices():
            raise AdbError(f"设备 {self.device} 不在线，请先启动 MuMu 模拟器")

    # -- 截图 ---------------------------------------------------------------
    def screencap(self) -> Image.Image:
        """优先取原始帧（约 0.25s），失败再退回 PNG（约 0.8s）。"""
        data = self._run("exec-out", "screencap", binary=True, timeout=60)
        img = decode_raw_screencap(data)
        if img is not None:
            return img
        data = self._run("exec-out", "screencap", "-p", binary=True, timeout=60)
        if not data:
            raise AdbError("screencap 返回空数据")
        return Image.open(io.BytesIO(data)).convert("RGB")

    # -- 触摸 ---------------------------------------------------------------
    def tap(self, x: int, y: int, wait: float = 0.0) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))
        if wait:
            time.sleep(wait)

    def long_press(self, x: int, y: int, ms: int = 800, wait: float = 0.0) -> None:
        self.shell("input", "swipe", str(int(x)), str(int(y)),
                   str(int(x)), str(int(y)), str(int(ms)))
        if wait:
            time.sleep(wait)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400,
              wait: float = 0.0) -> None:
        self.shell("input", "swipe", str(int(x1)), str(int(y1)),
                   str(int(x2)), str(int(y2)), str(int(ms)))
        if wait:
            time.sleep(wait)

    def key(self, code: str | int, wait: float = 0.0) -> None:
        self.shell("input", "keyevent", str(code))
        if wait:
            time.sleep(wait)

    # -- 应用 ---------------------------------------------------------------
    def force_stop(self, pkg: str = config.PKG) -> None:
        self.shell("am", "force-stop", pkg)

    def launch(self, pkg: str = config.PKG) -> None:
        self.shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
