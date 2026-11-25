"""sanmou_auto —— 三国谋定天下（com.bilibili.nslg）外部自动化。

设计约束（见 README）：
- 只做外部截图 + adb 模拟触摸，不注入、不读内存、不改包
- 每个功能单独设计、单独验证
"""

from . import _nowin  # noqa: F401  导入即启用「子进程不弹 CMD 框」

__all__ = ["config", "adb", "vision", "ocrengine", "intent", "mapnav",
           "report_collector", "hotspot", "troop_return", "march",
           "atomic_loop", "_nowin"]
