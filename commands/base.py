"""原子指令的公共设施：指令契约（输入/输出声明）与结果封装。

设计原则
--------
1. **一个指令只做一件事**，且可被单独调用（CLI / 单元测试 / 调度器）；
2. 指令之间不互相调用、不共享可变全局状态，所有上下文都由调度器
   （[`sanmou_auto.atomic_loop`](../atomic_loop.py)）显式传入；
3. 每个指令都返回 `dict`，键名固定（见各模块 `SPEC`），
   调度器只依赖这些键来推进调用链；
4. 指令内部允许“尽力而为”（OCR 读不到就降级），但必须把失败原因写进
   `reason`，让调度器决定下一步（而不是抛异常把长跑打断）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CommandSpec:
    """指令契约：名称 / 作用 / 输入 / 输出（用于文档、日志与自检）。"""
    key: str                     # 指令标识（调度器用它决定调用链）
    title: str                   # 中文名（技能文档里的一级标题）
    module: str                  # 实现模块（相对 sanmou_auto.commands）
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key, "title": self.title, "module": self.module,
            "inputs": list(self.inputs), "outputs": list(self.outputs),
            "preconditions": list(self.preconditions), "notes": self.notes,
        }


@dataclass
class CommandResult:
    """统一结果：ok / reason / 输入回显 / 输出 / 耗时。"""
    key: str
    ok: bool = False
    reason: str = ""
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "command": self.key, "ok": self.ok, "reason": self.reason,
            "inputs": self.inputs, "outputs": self.outputs,
            "seconds": round(self.seconds, 2),
        }


class Timer:
    """极简计时器（指令内部只想立刻拿到耗时）。"""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self._t0, 2)


def dump_camps(camps) -> list[dict]:
    """把 `Camp` 列表压成可序列化字典（给调度器存状态 / 写日志）。"""
    rows = []
    for camp in camps or []:
        coord = list(getattr(camp, "coord", ()) or ())
        rows.append({
            "name": getattr(camp, "name", ""),
            "coord": coord,
            "kind": getattr(camp, "kind_text", "") or getattr(camp, "kind", ""),
            "status": getattr(camp, "status", ""),
            "note": getattr(camp, "note", ""),
            "source": getattr(camp, "source", ""),
            "describe": camp.describe() if hasattr(camp, "describe") else "",
        })
    return rows


def write_json(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def read_json(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:              # noqa: BLE001 - 缺失/损坏都当空状态
        return {}


def seconds_until(hhmmss: str, clock_text: str) -> float | None:
    """服务器时间差：arrive - now（处理跨午夜），与 flow 里的实现一致。"""
    try:
        a = [int(p) for p in str(hhmmss).split(":")]
        b = [int(p) for p in str(clock_text).split(":")]
    except (ValueError, AttributeError):
        return None
    if len(a) != 3 or len(b) != 3:
        return None
    delta = ((a[0] * 3600 + a[1] * 60 + a[2])
             - (b[0] * 3600 + b[1] * 60 + b[2])) % (24 * 3600)
    if delta > 12 * 3600:
        delta -= 24 * 3600
    return float(delta)


def now_hhmmss() -> str:
    return time.strftime("%H:%M:%S")
