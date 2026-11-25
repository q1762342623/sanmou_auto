"""指令 3 · 据点调动：找离战场最近的据点，把满兵部队调动过去。

输入：
- `war_coord`：最近的战场坐标（战报收集输出的热点）
- `camps`：我方据点坐标列表（指令 1 的输出）
- （可选）`troops`：参与调动的部队编号，默认 1~4

输出：
- `sent`：**成功调动的部队编号**
- `lastArrival`：**最后一支部队到达的时间**（HH:MM:SS，服务器时钟）
- `needReplenish`：**需要补兵的部队编号**（兵力 < 33000，且点过补兵仍不满）
- `alreadyThere`：已经在据点里的部队（上一轮就调动过，不重复调动）
- `camp` / `campCoord`：本轮选中的据点

行为要点（用户口径）：
- 「最近」严格按离战场坐标的欧氏距离排序，不用上次成功点插队；
- **调动前先点左下角「补兵」，再判断是否满兵**（`Mobilizer.mobilize()` 内部
  `ensure_strength(replenish=True)` 已实现）；
- 勾「指令队列」后一个面板连续派完（见 `mobilize.ensure_queue_mode`）。
"""
from __future__ import annotations

import math

from .. import camp_cache
from ..alliance import AllianceFacilities, Camp
from ..march import Marcher as _Marcher
from ..mobilize import STRENGTH_FULL, Mobilizer
from ..sandbox import SandboxJump
from .base import CommandSpec, Timer
from .collect_camps import usable

SPEC = CommandSpec(
    key="mobilize", title="据点调动", module="sanmou_auto.commands.mobilize_troops",
    inputs=("war_coord", "camps"),
    outputs=("sent", "lastArrival", "needReplenish", "alreadyThere",
             "camp", "campCoord"),
    notes="离战场最近的据点；满兵(33000)才参战，不满的先点补兵",
)


def _select_camp(adb, fac: AllianceFacilities, camp: Camp, log=print) -> bool:
    """跳一次据点坐标；右侧出现「调动」即成功，否则回退同盟列表选点。"""
    try:
        sb = SandboxJump(adb, log=log)
        if sb.goto((int(camp.coord[0]), int(camp.coord[1])), tries=4):
            if fac.has_mobilize_button(attempts=2):
                log(f"  ✓ 已选中 {camp.describe()}")
                camp_cache.set_last_ok(camp.coord)
                return True
            log(f"  ! 跳转到 {camp.describe()} 后没有「调动」")
        sb.close()
    except Exception as error:      # noqa: BLE001
        log(f"  ! 沙盘选据点异常：{error!r}")
    log("  · 回退同盟列表选点")
    return fac.select(camp)


def _reselect_camp(adb, fac: AllianceFacilities, camp: Camp, nav, log=print) -> bool:
    """出征后面板被收起时，重跳同一据点（回调给 Mobilizer）。"""
    try:
        nav.ensure_map()
    except Exception as error:      # noqa: BLE001
        log(f"  ! 恢复地图失败（继续重选据点）：{error!r}")
    try:
        sb = SandboxJump(adb, log=log)
        if sb.goto((int(camp.coord[0]), int(camp.coord[1])), tries=4) \
                and fac.has_mobilize_button(attempts=2):
            return True
    except Exception as error:      # noqa: BLE001
        log(f"  ! 重选据点异常：{error!r}")
    return fac.reselect(camp)


def pick_nearest(camps: list[Camp], war_coord: tuple[int, int],
                 limit: int = 4) -> list[Camp]:
    """按离战场坐标的距离排序（只用可用据点）。"""
    usable_camps = [c for c in (camps or []) if usable(c)]
    ordered = sorted(usable_camps, key=lambda c: math.dist(c.coord, war_coord))
    last_ok = camp_cache.get_last_ok()
    if last_ok:                    # 同距离时优先上次成功的点，不改变距离顺序
        ordered.sort(key=lambda c: (round(math.dist(c.coord, war_coord), 3),
                                    0 if tuple(c.coord) == tuple(last_ok) else 1))
    return ordered[:max(1, limit)]


def run(*, war_coord, camps, troops=(1, 2, 3, 4), adb=None,
        min_strength: int = STRENGTH_FULL, try_replenish_first: bool = True,
        log=print, fac: AllianceFacilities | None = None, max_candidates: int = 4) -> dict:
    """把满兵部队调动到离 `war_coord` 最近的据点。"""
    timer = Timer()
    out: dict = {"ok": False, "camp": "", "campCoord": None,
                 "sent": [], "lastArrival": "", "needReplenish": [],
                 "alreadyThere": [], "statuses": {}, "strengths": {}}
    if not war_coord:
        out["reason"] = "no-war-coord"
        log("  × [据点调动] 没有战场坐标")
        return out
    war = (int(war_coord[0]), int(war_coord[1]))
    cands = pick_nearest(camps or [], war, limit=max_candidates)
    if not cands:
        out["reason"] = "no-camp"
        log("  × [据点调动] 没有可用据点（先跑一次据点收集）")
        return out
    for cand in cands:
        log(f"  候选据点 {cand.describe()} 距战场 {cand.dist(war):.1f}")

    facilities = fac or AllianceFacilities(adb, log=log)
    from ..mapnav import MapNav
    nav = MapNav(adb, log=log)

    camp = None
    for cand in cands:
        if _select_camp(adb, facilities, cand, log=log):
            camp = cand
            break
        try:
            facilities.back_to_map()
        except Exception:          # noqa: BLE001
            pass
    if camp is None:
        out["reason"] = "camp-select-failed"
        log("  × [据点调动] 最近的几个据点都选不中")
        return out
    out["camp"] = camp.describe()
    out["campCoord"] = list(camp.coord)

    mob = Mobilizer(adb, log=log,
                    reselect=lambda: _reselect_camp(adb, facilities, camp, nav, log=log))
    try:
        statuses = mob.read_statuses() if mob.panel_open() else {}
        mr = mob.mobilize(indexes=tuple(troops), min_strength=min_strength,
                          replenish=try_replenish_first, send_stationed=True)
        out["statuses"] = dict(statuses)
        out["strengths"] = {t.index: t.strength for t in mr.troops
                            if t.strength is not None}
        out["sent"] = [t.index for t in mr.troops
                       if t.dispatched and not t.arrived]
        out["alreadyThere"] = [t.index for t in mr.troops if t.arrived]
        out["needReplenish"] = [t.index for t in mr.troops
                                if t.skipped_reason == "low-strength"]
        out["lastArrival"] = mr.last_arrival or ""
        out["troops"] = mr.as_dict().get("troops") if hasattr(mr, "as_dict") else []
        out["ok"] = bool(out["sent"] or out["alreadyThere"])
        if not out["ok"]:
            out["reason"] = "no-troop-sent"
    except Exception as error:     # noqa: BLE001 - 单条指令失败不打断长跑
        out["reason"] = repr(error)
        log(f"  ! [据点调动] 异常：{error!r}")
    finally:
        try:
            mob.close()
        except Exception:          # noqa: BLE001
            pass
        try:
            facilities.back_to_map()
        except Exception:          # noqa: BLE001
            pass

    log(f"== [据点调动] 已派出 {out['sent']}，最后一支到达 "
        f"{out['lastArrival'] or '未知'}；需补兵 {out['needReplenish']}"
        + (f"；已在据点 {out['alreadyThere']}" if out["alreadyThere"] else ""))
    out["seconds"] = timer.seconds
    return out


__all__ = ["run", "pick_nearest", "SPEC", "_Marcher"]
