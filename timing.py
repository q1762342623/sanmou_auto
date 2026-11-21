"""步骤计时器：给「自动拉线」每一轮记录各阶段耗时。

用法：
    tm = StepTimer()
    ... 执行某个阶段 ...
    tm.mark("读取邮件", kind="分析", stage="邮件分析战场")

- 每个 mark 记录「距上一次 mark 的秒数」，因此阶段边界要成对埋点。
- kind 用于把步骤归到「分析 / 执行 / 准备 / 等待」等大类。
- stage 用于把步骤归到「自动拉线」的四个节点：
  邮件分析战场 / 调动据点 / 寻找敌方进攻 / 回家补兵。
- seconds 为真实墙钟时间（含 adb 截图、OCR、sleep 等）。
"""
from __future__ import annotations

import time

# 「自动拉线」四个节点（顺序即流程顺序）
STAGE_MAIL = "邮件分析战场"
STAGE_MOBILIZE = "调动据点"
STAGE_ATTACK = "寻找敌方进攻"
STAGE_HOME = "回家补兵"
STAGES = (STAGE_MAIL, STAGE_MOBILIZE, STAGE_ATTACK, STAGE_HOME)
STAGE_OTHER = "其他"


class StepTimer:
    """轻量阶段计时器（线程不安全，单流程使用即可）。"""

    def __init__(self, log=None):
        self.log = log
        self._last = time.monotonic()
        self._start = self._last
        self.steps: list[dict] = []

    def mark(self, name: str, kind: str = "执行", note: str = "",
             stage: str = "") -> float:
        """结束上一个阶段并登记：返回本阶段耗时（秒）。"""
        now = time.monotonic()
        dur = now - self._last
        self._last = now
        self.steps.append({
            "name": name,
            "kind": kind,
            "stage": stage,
            "seconds": round(dur, 3),
            "note": note,
        })
        return dur

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self._start, 3)

    def as_list(self) -> list[dict]:
        return [dict(s) for s in self.steps]

    # -------------------------------------------------------------- 汇总
    def totals_by_kind(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in self.steps:
            out[s["kind"]] = round(out.get(s["kind"], 0.0) + s["seconds"], 3)
        return out

    def totals_by_stage(self) -> dict[str, float]:
        """按「自动拉线」四大节点汇总耗时（没打 stage 的归到「其他」）。"""
        out: dict[str, float] = {}
        for s in self.steps:
            key = s.get("stage") or STAGE_OTHER
            out[key] = round(out.get(key, 0.0) + s["seconds"], 3)
        return out

    def summary(self) -> list[dict]:
        """按阶段名聚合：次数 / 总耗时 / 平均 / 最短 / 最长。"""
        agg: dict[str, dict] = {}
        for s in self.steps:
            a = agg.setdefault(s["name"], {
                "name": s["name"], "kind": s["kind"],
                "stage": s.get("stage") or STAGE_OTHER, "count": 0,
                "total": 0.0, "min": None, "max": 0.0,
            })
            a["count"] += 1
            a["total"] = round(a["total"] + s["seconds"], 3)
            a["min"] = s["seconds"] if a["min"] is None else min(a["min"], s["seconds"])
            a["max"] = max(a["max"], s["seconds"])
        out = []
        for a in agg.values():
            out.append({
                "name": a["name"], "kind": a["kind"], "stage": a["stage"],
                "count": a["count"],
                "total": a["total"],
                "avg": round(a["total"] / a["count"], 3),
                "min": a["min"], "max": a["max"],
            })
        out.sort(key=lambda x: x["total"], reverse=True)
        return out


def fmt_seconds(sec: float) -> str:
    """0.0 -> '0.0s'；83.2 -> '1m23s'。"""
    sec = float(sec)
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}m{s:02d}s"
