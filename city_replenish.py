"""补兵（城池版）：把 1-4 队全部调动到「离主城最近的城池」自动征兵。

与 `city_rest.py` 的区别：
- `city_rest`：**只有补不满的低兵力部队**，按「离战场最近」选城，属于进攻顺带的歇兵；
- 本模块：**主动补兵**，按「离主城最近」选城池，把指定的 1-4 队一次性调动过去，
  城里按游戏机制自动征兵；征满后由 `city_rest.recall_ready()` 逐支召回主城。

口径（用户确认）：
- 选城只看「城池」（城 / 水寨 / 军港 / 民港），**排除同盟大营/堡垒**；
- 距离基准是**我的主城**（`homecity.py` 采到的坐标），不是战场；
- 送出的部队登记进 `screenshots/flow/city_rest.json`，复用同一套「征满召回」逻辑，
  所以「补兵功能要能找到这些据点里的部队」= `entries()` / `--status` / `--recall`。

用法：
    python -m sanmou_auto.city_replenish                 # 选城 + 1-4 队全调动
    python -m sanmou_auto.city_replenish --dry-run       # 只选城并报告距离
    python -m sanmou_auto.city_replenish --troops 1,2    # 只送指定队伍
    python -m sanmou_auto.city_replenish --status        # 列出已在城池补兵的部队
    python -m sanmou_auto.city_replenish --recall        # 检查并召回已征满部队
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from . import camp_cache
from .adb import Adb
from .alliance import AllianceFacilities, Camp
from .city_rest import CityRest, RestEntry, _arrive_epoch
from .homecity import HomeCity
from .mobilize import Mobilizer
from .timing import StepTimer


class CityReplenish(CityRest):
    """离主城最近的城池 → 1-4 队全调动（城里自动征兵）。"""

    def __init__(self, adb: Adb | None = None,
                 fac: AllianceFacilities | None = None, log=print,
                 timer: StepTimer | None = None,
                 state_path: str | Path | None = None,
                 homecity: HomeCity | None = None,
                 home_coord: tuple[int, int] | None = None):
        super().__init__(adb=adb, fac=fac, log=log, timer=timer,
                         state_path=state_path)
        self.homecity = homecity or HomeCity(self.adb, log=log)
        self._home_coord = home_coord
        self._sandbox_broken = False

    # -- 主城 -------------------------------------------------------------
    def home(self) -> dict | None:
        if self._home_coord:
            return {"coord": tuple(self._home_coord), "name": "",
                    "coordText": f"({self._home_coord[0]},{self._home_coord[1]})"}
        info = self.homecity.load_or_read(max_age=21600)
        if not info:
            info = self.homecity.collect(verify=False)
        if info:
            self._home_coord = tuple(info["coord"])
            try:
                HomeCity.save(info)
            except Exception:               # noqa: BLE001
                pass
        return info

    # -- 选城 -------------------------------------------------------------
    def candidate_cities(self, camps: list[Camp] | None = None,
                         exclude: Camp | None = None) -> list[Camp]:
        """按「距主城由近到远」排序的城池候选（优先城，其次水寨/军港/民港）。"""
        home = self.home()
        if not home:
            self.log("  × 读不到我的主城坐标，无法按主城选城")
            return []
        base = tuple(home["coord"])
        if not camps:
            camps = camp_cache.load_camps()[0]
        cities = self._filter_cities(camps, exclude)
        if not cities:
            self.log("  · 缓存里没有城池 → 补扫一次据点列表")
            camps = self.fac.scan_all()
            if camps:
                camp_cache.save_camps(camps, battlefield=base)
            cities = self._filter_cities(camps, exclude)
        if not cities:
            self.log("  × 没有可用的城/水寨/军港/民港")
            return []

        def _rank(c: Camp) -> tuple[int, float]:
            kind = c.kind or c.kind_text
            return (0 if "城" in kind else 1, c.dist(base))

        return sorted(cities, key=_rank)

    def nearest_city(self, camps: list[Camp] | None = None,
                     exclude: Camp | None = None) -> Camp | None:
        cands = self.candidate_cities(camps=camps, exclude=exclude)
        if not cands:
            return None
        best = cands[0]
        base = tuple(self._home_coord or (0, 0))
        if base != (0, 0):
            self.log(f"== 离主城最近的城池：{best.describe()}"
                     f"（距主城 {best.dist(base):.1f}）")
        return best

    # -- 选城：只用「沙盘坐标直跳」，不打开同盟城池列表 --------------------
    # 说明：城池列表（同盟 → 城池）是 OCR 时代的做法，又慢又容易点歪；
    # 我们手里已经有坐标，沙盘跳一次 15s 就能把地块选中并出现「调动」。
    def _select_city(self, city: Camp) -> bool:
        try:
            from .sandbox import SandboxJump
            sb = SandboxJump(self.adb, log=self.log)
            target = (int(city.coord[0]), int(city.coord[1]))
            # 已经选中这一格（右侧就有「调动」）时不要再缩放/跳转，
            # 否则会把用户/上一轮已经准备好的选中状态弄丢。
            if not sb.visible():
                got = sb.selected_coord()
                if self.fac.has_mobilize_button(attempts=1) and \
                        (got is None or got == target):
                    self.log(f"  ✓ 当前已选中 {got or target}，直接用")
                    return True
            if sb.visible():
                sb.close()
            if not sb.goto(target, tries=4):
                self.log(f"  ! 沙盘没跳到 {city.coord}（捏合/输入失败）")
                sb.close()
                return False
            if self.fac.has_mobilize_button(attempts=2):
                self.log(f"  ✓ 沙盘直跳选中 {city.describe()}")
                return True
            self.log(f"  ! 沙盘已到 {city.coord}，但右侧没有「调动」"
                     "（该城可能不可调动）")
            return False
        except Exception as e:              # noqa: BLE001
            self.log(f"  ! 沙盘选城异常：{e!r}")
            return False

    def _reselect_city(self, city: Camp) -> bool:
        """出征后「调动」面板自动收起时，沙盘重跳回同一格再打开面板。"""
        try:
            from .sandbox import SandboxJump
            sb = SandboxJump(self.adb, log=self.log)
            if sb.goto((int(city.coord[0]), int(city.coord[1])), tries=4) \
                    and self.fac.has_mobilize_button(attempts=2):
                self.log(f"  ✓ 沙盘重跳回 {city.describe()}")
                return True
        except Exception as e:              # noqa: BLE001
            self.log(f"  ! 沙盘重选城异常：{e!r}")
        return False

    # -- 调动 -------------------------------------------------------------
    def run(self, indexes: tuple[int, ...] = (1, 2, 3, 4),
            dry_run: bool = False, exclude: Camp | None = None,
            camps: list[Camp] | None = None) -> dict:
        """把 indexes 指定的部队全部调动到最近城池补兵。"""
        indexes = tuple(int(i) for i in indexes if 1 <= int(i) <= 4)
        result: dict = {"ok": False, "mode": "city-replenish",
                        "troops": list(indexes)}
        home = self.home()
        if home:
            result["home"] = {"name": home.get("name"),
                              "coord": list(home["coord"]),
                              "coordText": home.get("coordText")}
        cands = self.candidate_cities(camps=camps, exclude=exclude)
        if not cands:
            result["reason"] = "no-city"
            return result
        base = tuple(home["coord"]) if home else None
        city = None
        for cand in cands[:4]:
            if base:
                self.log(f"  候选 {cand.describe()} 距主城 {cand.dist(base):.1f}")
            if dry_run:
                city = cand
                break
            if self._select_city(cand):
                city = cand
                break
            self.log("  ! 该城选不中 / 没有「调动」，换下一个")
            try:
                self.fac.back_to_map()
            except Exception:               # noqa: BLE001
                pass
        if city is None:
            result["reason"] = "city-select-failed"
            self.log("  × 最近的几个城池都选不中，补兵取消")
            return result
        result["city"] = city.describe()
        result["city_coord"] = list(city.coord)
        if base:
            result["city_dist_home"] = round(
                math.dist(base, tuple(city.coord)), 1)
        if dry_run:
            result["ok"] = True
            result["reason"] = "dry-run"
            return result
        self._mark("选中补兵城池", "执行", note=city.describe())

        mob = Mobilizer(self.adb, log=self.log,
                        reselect=lambda: self._reselect_city(city),
                        timer=self.timer)
        try:
            mr = mob.mobilize(indexes=indexes, min_strength=0,
                              replenish=False, send_stationed=True)
        finally:
            mob.close()
            self.fac.back_to_map()

        # 登记到 city_rest.json：后续 recall_ready() 就靠这份记录找到这些部队
        entries = [e for e in self.entries() if e.troop not in indexes]
        sent: list[RestEntry] = []
        for troop in mr.troops:
            if not troop.dispatched:
                continue
            entry = RestEntry(
                troop=troop.index,
                city_name=city.name,
                city_coord=tuple(city.coord),
                city_kind=city.kind or city.kind_text,
                city_source=city.source,
                city_page=city.page,
                sent_at=time.time(),
                arrive_at=troop.arrive_at or "",
                arrive_epoch=_arrive_epoch(troop.arrive_at or "",
                                          mob.clock.now()),
                strength=troop.strength,
                status=troop.status,
            )
            sent.append(entry)
            entries.append(entry)
        self._save(entries)

        result.update({
            "ok": bool(sent),
            "sent": [e.as_dict() for e in sent],
            "failed": [t.index for t in mr.troops if not t.dispatched],
            "statuses": {t.index: t.status for t in mr.troops},
            "last_arrival": mr.last_arrival,
        })
        if sent:
            self.log("  ✓ 已调动到城池补兵: " + ", ".join(
                f"部队{e.troop}" for e in sent)
                + f" @{city.name or city.coord}")
        else:
            result["reason"] = "dispatch-failed"
            self.log("  × 没有部队成功调动到城池")
        self._mark("部队调动到城池补兵", "执行", note=city.describe())
        return result

    # -- 查询（补兵中的部队在哪） ------------------------------------------
    def status(self) -> dict:
        entries = self.entries()
        by_city: dict[tuple[int, int], list[int]] = {}
        for e in entries:
            by_city.setdefault(tuple(e.city_coord), []).append(e.troop)
        return {
            "ok": True,
            "count": len(entries),
            "cities": [{"coord": list(coord), "name": group_city(entries, coord),
                        "troops": troops}
                       for coord, troops in by_city.items()],
            "rests": [e.as_dict() for e in entries],
        }


def group_city(entries: list[RestEntry], coord: tuple[int, int]) -> str:
    for e in entries:
        if tuple(e.city_coord) == tuple(coord):
            return e.city_name
    return ""


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from . import cli

    cli.setup_console()
    ap = argparse.ArgumentParser(description="补兵：1-4 队调动到离主城最近的城池")
    ap.add_argument("--troops", default="1,2,3,4", help="部队序号，逗号分隔")
    ap.add_argument("--dry-run", action="store_true", help="只选城报告，不调动")
    ap.add_argument("--status", action="store_true", help="列出正在城池补兵的部队")
    ap.add_argument("--recall", action="store_true", help="检查并召回已征满部队")
    ap.add_argument("--home", default="", help="手动指定主城坐标 x,y（跳过采集）")
    ap.add_argument("--timing", action="store_true", help="打印步骤耗时")
    args = ap.parse_args(argv)

    home_coord = None
    if args.home:
        x, y = args.home.replace("，", ",").split(",")
        home_coord = (int(x), int(y))
    tm = StepTimer() if args.timing else None
    job = CityReplenish(timer=tm, home_coord=home_coord)

    if args.status:
        print(json.dumps(job.status(), ensure_ascii=False, indent=2))
        return 0
    if args.recall:
        print(json.dumps(job.recall_ready(), ensure_ascii=False, indent=2))
        return 0

    troops = tuple(int(t) for t in args.troops.split(",") if t.strip())
    res = job.run(indexes=troops, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if tm is not None:
        for step in tm.summary():
            print(f"  {step['name']}: {step['total']:.1f}s x{step['count']}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
