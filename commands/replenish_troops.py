"""指令 4 · 不满兵部队补给兵力：把部队调到「离主城最近的城池」，城里自动征兵。

输入：
- `troops`：不满编的部队编号（指令 3 的 `needReplenish`）
- `home_coord`：主城坐标（指令 2 的输出）
- `camps`：我方据点坐标（指令 1 的输出）

输出：无（内部把补兵记录写进 `screenshots/flow/city_rest.json`，
后续 `CityRest.recall_ready()` / 指令 6 就靠它判断「是否已补满」）
"""
from __future__ import annotations

from ..alliance import Camp
from ..city_replenish import CityReplenish
from .base import CommandSpec, Timer

SPEC = CommandSpec(
    key="replenish", title="不满兵部队补给兵力",
    module="sanmou_auto.commands.replenish_troops",
    inputs=("troops", "home_coord", "camps"), outputs=(),
    notes="按主城距离选最近城池；部队进城后自动征兵",
)


def run(*, troops, home_coord, camps: list[Camp] | None = None, adb=None,
        log=print) -> dict:
    """把 `troops` 送到离主城最近的城池补兵。"""
    timer = Timer()
    indexes = tuple(int(t) for t in (troops or []) if 1 <= int(t) <= 4)
    if not indexes:
        log("  · [补给兵力] 没有需要补兵的部队，跳过")
        return {"ok": True, "skipped": True, "troops": [], "seconds": timer.seconds}
    if not home_coord:
        log("  × [补给兵力] 没有主城坐标，无法按主城选城")
        return {"ok": False, "reason": "no-home", "troops": list(indexes),
                "seconds": timer.seconds}
    log(f"== [补给兵力] 部队{list(indexes)} → 离主城最近的城池")
    replenish = CityReplenish(adb, log=log, home_coord=tuple(home_coord))
    try:
        result = replenish.run(indexes=indexes, camps=camps)
    except Exception as error:     # noqa: BLE001
        log(f"  ! [补给兵力] 异常：{error!r}")
        return {"ok": False, "reason": repr(error), "troops": list(indexes),
                "seconds": timer.seconds}
    sent = [row.get("troop") for row in (result.get("sent") or [])
            if isinstance(row, dict)]
    log(f"== [补给兵力] 已送入补兵城 {sent}"
        + (f"（{result.get('city')}）" if result.get("city") else "")
        + (f"，失败 {result.get('failed')}" if result.get("failed") else ""))
    return {"ok": bool(result.get("ok")), "troops": list(indexes),
            "sent": sent, "city": result.get("city"),
            "cityCoord": result.get("city_coord"),
            "failed": result.get("failed"),
            "lastArrival": result.get("last_arrival"),
            "reason": result.get("reason"),
            "seconds": timer.seconds}
