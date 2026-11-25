"""指令 2 · 主城坐标收集：右上角箭头 → 建筑 → 第一条 = 我的主城。

输入：无
输出：`home`（`{"name","coord","coordText",...}`）
"""
from __future__ import annotations

from ..homecity import HomeCity
from ..mapnav import MapNav
from .base import CommandSpec, Timer

SPEC = CommandSpec(
    key="home", title="主城坐标收集", module="sanmou_auto.commands.collect_home",
    inputs=(), outputs=("home",),
    notes="地图右上「定位」→ 建筑 → 第一行；用底栏坐标校验",
)


def run(adb=None, *, log=print, verify: bool = True,
        nav: MapNav | None = None, use_cache: bool = False,
        max_age: float = 6 * 3600) -> dict:
    """读一次我的主城坐标。`use_cache=True` 时 6 小时内可直接复用文件。"""
    timer = Timer()
    if use_cache:
        cached = HomeCity.load()
        age = None
        if cached and cached.get("collectedAt"):
            import time
            age = time.time() - float(cached["collectedAt"])
        if cached and (age is None or age <= max_age):
            log(f"== [主城坐标] 复用缓存：{cached.get('name')}"
                f"{cached.get('coordText') or ''}")
            return {"ok": True, "home": cached, "cached": True,
                    "seconds": timer.seconds}

    nav = nav or MapNav(adb, log=log)
    hc = HomeCity(adb, log=log, nav=nav)
    info = hc.collect(verify=verify)
    if info:
        HomeCity.save(info)
        log(f"== [主城坐标] {info.get('name')}{info.get('coordText') or ''}"
            f" → {info.get('coord')}")
    else:
        log("  × [主城坐标] 没读到我的主城")
    return {"ok": bool(info), "home": info, "cached": False,
            "seconds": timer.seconds}
