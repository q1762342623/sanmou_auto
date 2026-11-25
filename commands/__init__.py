"""原子指令包：把「三谋拉线与战报自动收集」拆成 7 条可独立执行的指令。

SKILL 只负责**调度**：按契约传输入、读输出、决定下一条指令；
每条指令自己完成一次完整的界面操作，不依赖别的指令的内部状态。

| key | 指令 | 输入 | 输出 |
| --- | --- | --- | --- |
| `camps` | 据点收集 | 无 | 我方据点坐标（城池/民港/水寨/军港/同盟大营） |
| `home` | 主城坐标收集 | 无 | 我的主城坐标 |
| `mobilize` | 据点调动 | 战场坐标 + 据点坐标 | 已调动部队 / 最后一支到达时间 / 需补兵部队 |
| `replenish` | 补给兵力 | 需补兵部队 + 主城坐标 + 据点坐标 | 无 |
| `attack` | 部队进攻 | 战场坐标 + 已调入据点的部队 | 参战部队 / 最晚到达时间 |
| `return` | 部队撤回 | 参战部队 + 到达时间 + 补兵部队 | 无 |
| `reports` | 战报收集 | 无 | 最高频次交战坐标（战场） |

用法：
    from sanmou_auto.commands import COMMANDS, SPECS, spec_table
    out = COMMANDS["camps"].run(adb, log=print)
"""
from __future__ import annotations

from . import (attack, collect_camps, collect_home, collect_reports,
               mobilize_troops, replenish_troops, return_troops)
from .base import CommandResult, CommandSpec, Timer, dump_camps

COMMANDS = {
    "camps": collect_camps,
    "home": collect_home,
    "mobilize": mobilize_troops,
    "replenish": replenish_troops,
    "attack": attack,
    "return": return_troops,
    "reports": collect_reports,
}

SPECS = [module.SPEC for module in COMMANDS.values()]
ORDER = ("camps", "home", "reports", "mobilize", "replenish", "attack", "return")


def spec_table() -> str:
    """把指令契约渲染成 Markdown 表格（SKILL 文档 / `--specs` 输出用）。"""
    lines = ["| key | 指令 | 输入 | 输出 | 前置条件 |",
             "| --- | --- | --- | --- | --- |"]
    for key in ORDER:
        spec: CommandSpec = COMMANDS[key].SPEC
        lines.append(
            f"| `{spec.key}` | {spec.title} | {' / '.join(spec.inputs) or '无'} | "
            f"{' / '.join(spec.outputs) or '无'} | "
            f"{' / '.join(spec.preconditions) or '-'} |")
    return "\n".join(lines)


__all__ = ["COMMANDS", "SPECS", "ORDER", "spec_table",
           "CommandResult", "CommandSpec", "Timer", "dump_camps"]
