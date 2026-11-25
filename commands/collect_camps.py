"""指令 1 · 据点收集：收集我方据点坐标（城池 / 民港 / 水寨 / 军港 / 同盟大营）。

输入：无
输出：
- `camps`：`Camp` 对象列表（内存用）
- `campDicts`：可序列化坐标列表
- `count` / `cached` / `scannedAt`

过滤规则（按用户口径）：
- 进驻状态 = 「无法入驻」的据点没有「调动」按钮 → 排除（`NO_MOBILIZE_HINTS`）；
- **建造中的同盟大营不要加进来**（note/status 里出现 建造中 / 建造 / 创建中 /
  未完成 / 施工 都排除）。
"""
from __future__ import annotations

import time

from .. import camp_cache
from ..alliance import NO_MOBILIZE_HINTS, AllianceFacilities, Camp
from .base import CommandSpec, Timer, dump_camps

SPEC = CommandSpec(
    key="camps", title="据点收集", module="sanmou_auto.commands.collect_camps",
    inputs=(), outputs=("camps", "campDicts", "count", "cached", "scannedAt"),
    notes="同盟建筑（大营/堡垒）+ 同盟城池列表；排除建造中/无法入驻",
)

# 建造中的据点提示（同盟大营在建时会在状态/备注里带这些字）
BUILDING_HINTS = ("建造中", "建造", "创建中", "未完成", "施工", "升级中")

# 默认有效期：用户要求每 1.5 小时刷新一次
FRESH_SECONDS = 90 * 60


def usable(camp: Camp) -> bool:
    """这个据点能不能用来「调动」。"""
    note = camp.note or ""
    status = camp.status or ""
    kind = f"{camp.kind or ''} {camp.kind_text}"
    if any(hint in note for hint in NO_MOBILIZE_HINTS):
        return False
    haystack = f"{note} {status} {kind}"
    if any(hint in haystack for hint in BUILDING_HINTS):
        return False
    return True


def run(adb=None, *, refresh: bool = True, max_age: float = FRESH_SECONDS,
        log=print, fac: AllianceFacilities | None = None) -> dict:
    """扫一次我方据点；6 小时内有缓存且未超 `max_age` 时直接用缓存。"""
    timer = Timer()
    camps, meta = camp_cache.load_camps()
    age = meta.get("ageSeconds")
    stale = meta.get("stale") or age is None or float(age) > float(max_age)
    if camps and not refresh:
        filtered = [c for c in camps if usable(c)]
        log(f"== [据点收集] 用缓存 {len(filtered)} 个（{meta.get('scannedAtIso')}）")
        return {"ok": bool(filtered), "camps": filtered,
                "campDicts": dump_camps(filtered), "count": len(filtered),
                "cached": True, "scannedAt": meta.get("scannedAtIso"),
                "seconds": timer.seconds}
    if camps and not stale and not refresh:
        filtered = [c for c in camps if usable(c)]
        log(f"== [据点收集] 缓存仍新鲜，复用 {len(filtered)} 个")
        return {"ok": bool(filtered), "camps": filtered,
                "campDicts": dump_camps(filtered), "count": len(filtered),
                "cached": True, "scannedAt": meta.get("scannedAtIso"),
                "seconds": timer.seconds}

    log("== [据点收集] 开始扫描（同盟建筑 + 城池列表，约 2~5 分钟）")
    facilities = fac or AllianceFacilities(adb, log=log)
    scanned = facilities.scan_all() or []
    filtered = [c for c in scanned if usable(c)]
    dropped = len(scanned) - len(filtered)
    if filtered:
        camp_cache.save_camps(filtered)
    log(f"  ✓ [据点收集] 解析 {len(scanned)} 个，可用 {len(filtered)} 个"
        f"（过滤掉 {dropped} 个：建造中/无法入驻）")
    return {"ok": bool(filtered), "camps": filtered,
            "campDicts": dump_camps(filtered), "count": len(filtered),
            "cached": False,
            "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "dropped": dropped, "seconds": timer.seconds}
