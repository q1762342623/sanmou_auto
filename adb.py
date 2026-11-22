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
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
        return subprocess.run(
            [self.adb_path, "devices"], capture_output=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("utf-8", "ignore")

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

    def key_repeat(self, code: str | int, times: int = 1,
                   wait: float = 0.0) -> None:
        """连发同一个键（清空输入框用 DEL=67）：一次 shell 调用发完。"""
        n = max(0, int(times))
        if not n:
            return
        self.shell("input", "keyevent", *([str(code)] * n))
        if wait:
            time.sleep(wait)

    def input_text(self, text: str, wait: float = 0.0) -> None:
        """往当前焦点输入框打 ASCII 文本（坐标 x.y 用）。"""
        self.shell("input", "text", text)
        if wait:
            time.sleep(wait)

    # -- 多指（sendevent）-----------------------------------------------------
    _PINCH_DIR = None

    def _pinch_script(self, direction: str, steps: int, radius: float,
                      event: str) -> str:
        cx, cy = 450, 800
        r0 = int(300 * radius)
        r1 = 40 if direction == "out" else int(320 * radius)
        lines = ["#!/system/bin/sh", f"D={event}", "sendevent $D 1 330 1"]
        for slot, tid, x in ((0, 1001, cx - r0), (1, 1002, cx + r0)):
            lines += [f"sendevent $D 3 47 {slot}",
                      f"sendevent $D 3 57 {tid}",
                      f"sendevent $D 3 53 {x}",
                      f"sendevent $D 3 54 {cy}",
                      "sendevent $D 0 0 0", "sleep 0.03"]
        for i in range(1, steps + 1):
            r = int(r0 + (r1 - r0) * i / steps)
            lines += ["sendevent $D 3 47 0", f"sendevent $D 3 53 {cx - r}",
                      "sendevent $D 0 0 0", "sleep 0.02",
                      "sendevent $D 3 47 1", f"sendevent $D 3 53 {cx + r}",
                      "sendevent $D 0 0 0", "sleep 0.02"]
        for slot in (0, 1):
            lines += [f"sendevent $D 3 47 {slot}",
                      "sendevent $D 3 57 -1",
                      "sendevent $D 0 0 0", "sleep 0.03"]
        lines += ["sendevent $D 1 330 0", "sendevent $D 0 0 0"]
        return "\n".join(lines) + "\n"

    def pinch(self, direction: str = "out", steps: int = 16,
              radius: float = 0.9, event: str = "/dev/input/event4") -> None:
        """双指捏合：out=收拢（地图缩小），in=外扩（放大）。

        `adb shell input` 只支持单点，所以走 sendevent 注入 MT-B 事件：
        分指按下、每步一帧、逐指抬起。脚本 push 到 /data/local/tmp 只推一次。
        """
        import hashlib
        import os
        import tempfile

        script = self._pinch_script(direction, steps, radius, event)
        digest = hashlib.md5(script.encode("utf-8")).hexdigest()[:10]
        remote = f"/data/local/tmp/sanmou_pinch_{digest}.sh"
        if self._PINCH_DIR != digest:
            fd, local = tempfile.mkstemp(suffix=".sh")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(script)
                proc = subprocess.run(
                    [self.adb_path, "-s", self.device, "push", local, remote],
                    capture_output=True, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode != 0:
                    raise AdbError(proc.stderr.decode("utf-8", "ignore").strip()
                                   or "pinch push 失败")
                type(self)._PINCH_DIR = digest
            finally:
                try:
                    os.unlink(local)
                except OSError:
                    pass
        self.shell("sh", remote, timeout=30)

    # -- 应用 ---------------------------------------------------------------
    def force_stop(self, pkg: str = config.PKG) -> None:
        self.shell("am", "force-stop", pkg)

    def launch(self, pkg: str = config.PKG) -> None:
        self.shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
