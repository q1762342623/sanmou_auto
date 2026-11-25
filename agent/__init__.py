"""外部 Agent 监控层（只读）：决策 100% 由规则调度器负责。

- [`deepseek.py`](deepseek.py)：零依赖 DeepSeek 客户端（密钥读 `secret.json` / 环境变量）
- [`monitor.py`](monitor.py)：定时巡检调度状态 → 规则体检 + LLM 中文诊断 → 落盘

注意：这里**没有**任何决策逻辑。战斗本体是
[`sanmou_auto.atomic_loop`](../atomic_loop.py)（纯规则、确定性）；
监控进程跑在另一个终端/进程里，只读 `atomic_state.json` 与指令流水，
不碰设备、不改状态。
"""
from __future__ import annotations

__all__ = ["BattleMonitor", "DeepSeekClient", "DeepSeekError"]


def __getattr__(name: str):
    if name in ("DeepSeekClient", "DeepSeekError"):
        from . import deepseek
        return getattr(deepseek, name)
    if name == "BattleMonitor":
        from . import monitor
        return getattr(monitor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
