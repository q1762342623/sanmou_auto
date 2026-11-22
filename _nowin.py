"""Windows：让本进程所有 subprocess 调用默认不弹控制台窗口。

导入本模块即生效（幂等）。必要性：
- 长跑进程以 `DETACHED_PROCESS` 启动后**没有控制台**，
  任何未带 `CREATE_NO_WINDOW` 的子进程（adb / tasklist / python…）
  都会新建一个 CMD 窗口闪一下；
- 这里统一给 `subprocess.run` / `subprocess.Popen` 补上标志，
  已经显式传 `DETACHED_PROCESS` / `CREATE_NEW_CONSOLE` 的调用保持原样。

用法：`from sanmou_auto import _nowin`（`sanmou_auto/__init__.py` 已自动导入）。
"""
from __future__ import annotations

import subprocess
import sys

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_DETACHED = 0x00000008
_NEW_CONSOLE = 0x00000010
_APPLIED = False


def apply() -> None:
    global _APPLIED
    if _APPLIED or sys.platform != "win32":
        return
    _APPLIED = True

    orig_run = subprocess.run
    orig_popen = subprocess.Popen

    def run(*args, **kwargs):
        kwargs.setdefault("creationflags", NO_WINDOW)
        return orig_run(*args, **kwargs)

    def Popen(*args, **kwargs):          # noqa: N802 - 与标准库同名
        flags = int(kwargs.get("creationflags") or 0)
        if not flags & (_DETACHED | _NEW_CONSOLE):
            kwargs["creationflags"] = flags | NO_WINDOW
        return orig_popen(*args, **kwargs)

    subprocess.run = run
    subprocess.Popen = Popen


apply()
