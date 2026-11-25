"""指令 5 · 部队进攻：向战报热点「攻城 / 行军」（绝不「攻占」）。

前置条件：本轮执行过一次有效调动，且部队已调入最近据点。
输入：
- `war_coord`：最近的战场坐标（战报热点）
- `troops`：已成功调入最近据点的部队编号

输出：
- `marching`：**参与行军的部队编号**
- `lastArrival`：**部队行军到达的最晚时间**
- `action`：实际用的按钮（攻城 / 行军）
- `target`：实际目标地块（热点不可行军时会用相邻格）

行为要点（用户口径）：
- 优先「攻城」，没有攻城才用「行军」，**永不点「攻占」**（会触发自动铺路）；
- 地图只跳一次：第一次定位后屏幕中心就是目标地块，之后**只重新点该地块**
  取回按钮，不再回大地图重新定位；
- 连「行军」都是灰的 → 在热点周围逐个相邻格找能点的点（`Marcher` 已实现）。
"""
from __future__ import annotations

from ..mapnav import MapNav
from ..march import Marcher
from .base import CommandSpec, Timer

SPEC = CommandSpec(
    key="attack", title="部队进攻", module="sanmou_auto.commands.attack",
    inputs=("war_coord", "troops"),
    outputs=("marching", "lastArrival", "action", "target"),
    preconditions=("本轮已有效调动并到位",),
    notes="攻城 > 行军；绝不攻占；只跳一次坐标，后续点同一地块重取按钮",
)


def run(*, war_coord, troops, adb=None, log=print, nav: MapNav | None = None) -> dict:
    timer = Timer()
    out: dict = {"ok": False, "marching": [], "lastArrival": "",
                 "action": "", "target": list(war_coord) if war_coord else None}
    indexes = tuple(int(t) for t in (troops or []) if 1 <= int(t) <= 4)
    if not war_coord:
        out["reason"] = "no-war-coord"
        log("  × [部队进攻] 没有战场坐标")
        return out
    if not indexes:
        out["reason"] = "no-troop"
        log("  × [部队进攻] 没有可用部队")
        return out

    nav = nav or MapNav(adb, log=log)
    nav.ensure_map()
    marcher = Marcher(adb, log=log, nav=nav)
    coord = (int(war_coord[0]), int(war_coord[1]))
    log(f"== [部队进攻] 目标 {coord}，部队{list(indexes)}（攻城优先，绝不攻占）")
    if not marcher.select_coord(coord):
        out["reason"] = "jump-failed"
        log("  × [部队进攻] 定位战场坐标失败")
        return out
    result = marcher.dispatch_all(indexes)
    out["action"] = result.get("action") or ""
    out["target"] = result.get("tile") or result.get("target") or list(coord)
    out["marching"] = [row.get("index") for row in (result.get("troops") or [])
                       if isinstance(row, dict) and row.get("dispatched")]
    out["lastArrival"] = result.get("last_arrival") or ""
    out["failed"] = result.get("failed") or []
    out["ok"] = bool(out["marching"])
    if not out["ok"]:
        out["reason"] = result.get("reason") or "dispatch-failed"
    log(f"== [部队进攻] {out['action'] or '未点按钮'} → 出征 {out['marching']}，"
        f"最晚到达 {out['lastArrival'] or '未知'}"
        + (f"，失败 {out['failed']}" if out["failed"] else "")
        + ("" if out["ok"] else f"（{out['reason']}）"))
    out["seconds"] = timer.seconds
    return out
