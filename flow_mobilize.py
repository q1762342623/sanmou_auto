"""功能「自动拉线」：一轮 = 邮件分析战场 → 调动据点 → 寻找敌方进攻 → 回家补兵。

四个节点（计时器 `timing.StepTimer` 的 stage 字段）：
1. **邮件分析战场**：读「同盟」页签最近 N 封邮件，解析关键字（集结/保护/蹲坑/占领/
   攻占/梭哈/拉线…，黑名单「虫子」跳过），取最新一条可执行指令的坐标作为战场
2. **调动据点**：找离战场最近、且能「调动」的盟归属据点（同盟建筑 + 城池列表），
   逐支把 部队1-4 派过去（出征前读「兵力 N」，不满先点白色补兵，补完仍 <门槛就跳过）
3. **寻找敌方进攻**：按邮件动作点 集结/攻占/行军 → 出征；邮件执行不了就在地图上找
   红色敌对目标，视野没有红敌则用「战报」绿色坐标；等战斗结束
4. **回家补兵**：回城（城内视角）→ 亲卫营 → 逐队读「兵力 当前/上限」→ 不满就补兵
   → 出城回地图，给下一轮让位（`sanmou_auto/home.py`）

实测优化（2026-11-21）：
- 「定位最近可调动据点」原本每轮都全量扫同盟建筑 + 14 屏城池列表（≈340s）；
  现在缓存据点列表：战场坐标变动 ≤ 60 格时直接复用，选不中才重扫
- 回城按钮真实中心是 (60,772)，原 (99,798) 点在图标外的空白处
- 回家补兵整段约 30s（三队读兵力 + 出城）

结果写入 screenshots/flow/auto_line_<时间戳>.json（含每一轮明细与阶段耗时）。

用法:
    python -m sanmou_auto.flow_mobilize                 # 跑一轮（默认部队 1-4 + 战斗）
    python -m sanmou_auto.flow_mobilize --troops 1,2,3  # 只调动 1-3 队
    python -m sanmou_auto.flow_mobilize --no-battle     # 只调动 + 回家补兵
    python -m sanmou_auto.flow_mobilize --limit 10 --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import time

from . import cli, config, intent, vision
from .adb import Adb
from .alliance import NO_MOBILIZE_HINTS, AllianceFacilities, Camp
from .battle import BattleCommander, BattleFlow
from .home import HomeReplenish
from .mail import MailBody, MailReader
from .mapnav import MapNav
from .mobilize import STRENGTH_FULL, Mobilizer
from .timing import STAGE_MAIL, STAGE_MOBILIZE, StepTimer

CLUSTER_RADIUS = 6      # 判定“同一个战场”的坐标距离（格）
CAMP_CACHE_RADIUS = 60  # 战场移动不超过该距离时复用上一轮的据点列表（避免重复扫描）


class MobilizeFlow:
    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.reader = MailReader(self.adb, log=log)
        self.nav = MapNav(self.adb, log=log)
        self.log = log
        # 据点列表缓存：{(Camp…)} + 当时扫描的战场坐标
        self._camps: list[Camp] = []
        self._camps_at: tuple[int, int] | None = None

    # ------------------------------------------------------------------
    def read_mails(self, limit: int = 10, tabs: tuple[str, ...] = ("同盟",)):
        self.log(f"== 读取邮件（{list(tabs)}，上限 {limit} 封）")
        mails = self.reader.read_latest(limit=limit, tabs=list(tabs))
        self.reader.back()
        return mails

    def battlefield(self, mails: list[MailBody]):
        """战场 = 最新一条白名单指令的坐标；返回 (坐标, 同战场指令, 代表邮件)。

        没有可执行指令时，只要邮件里带坐标也借用它作为战场坐标——
        战斗阶段再去地图上找红色敌对目标对撞（对应「邮件意义不明」的情况）。
        """
        actionable = [m for m in mails if m.intent and m.intent.actionable]
        if actionable:
            head = actionable[0]
            coord = head.intent.coord
            group = [m for m in actionable
                     if m.intent.coord
                     and math.dist(m.intent.coord, coord) <= CLUSTER_RADIUS]
            self.log(f"== 战场：{head.intent.describe()}")
            if len(group) > 1:
                self.log(f"   （附近 {len(group)} 条指令指向同一片战场："
                         + "、".join(m.title for m in group) + "）")
            return coord, group, head

        with_coord = [m for m in mails if m.intent and m.intent.coord]
        if with_coord:
            head = with_coord[0]
            self.log("== 没有可执行指令，借用邮件坐标 "
                     f"{head.intent.coord} 作为战场（战斗阶段改为红色敌对兜底）")
            return head.intent.coord, [], head
        return None, [], None

    # ------------------------------------------------------------------
    def locate_camp(self, fac: AllianceFacilities,
                    coord: tuple[int, int]) -> Camp | None:
        """找最近可调动据点：优先复用上一轮的据点列表，避免每轮重扫 5 分钟。"""
        if (self._camps and self._camps_at is not None
                and math.dist(coord, self._camps_at) <= CAMP_CACHE_RADIUS):
            ordered = sorted(self._camps, key=lambda c: c.dist(coord))
            for c in ordered[:2]:
                self.log(f"  · 复用缓存候选 {c.describe()} 距目标 {c.dist(coord):.1f}")
                if fac.select(c) and fac.has_mobilize_button():
                    self.log(f"== 最近可调动据点（缓存命中）：{c.describe()}"
                             f"（距目标 {c.dist(coord):.1f}）")
                    return c
            self.log("  · 缓存候选都选不中，重新扫描同盟据点")

        camps = fac.scan_all()
        camps = [c for c in camps
                 if not any(h in c.note for h in NO_MOBILIZE_HINTS)]
        self._camps = camps
        self._camps_at = coord
        if not camps:
            return None
        return fac.goto_nearest(coord, camps=camps)

    # ------------------------------------------------------------------
    def run(self, limit: int = 10, troops: tuple[int, ...] = (1, 2, 3, 4),
            dry_run: bool = False, battle: bool = True,
            wait_return: bool = True, battle_timeout: float = 900.0,
            min_strength: int = STRENGTH_FULL, replenish: bool = True,
            enemy_fallback: bool = True, timer: StepTimer | None = None,
            home: bool = True) -> dict:
        # 调动范围固定为 1-4 队
        troops = tuple(int(t) for t in troops if 1 <= int(t) <= 4) or (1, 2, 3, 4)
        tm = timer or StepTimer()
        self.adb.ensure_online()
        tm.mark("连接设备", "准备", stage=STAGE_MAIL)
        self.nav.ensure_map()
        tm.mark("回到地图", "执行", stage=STAGE_MAIL)
        mails = self.read_mails(limit=limit)
        tm.mark("读取/解析邮件", "分析", stage=STAGE_MAIL)
        for m in mails:
            self.log("   - " + (m.intent.describe() if m.intent else m.title))

        coord, group, head = self.battlefield(mails)
        tm.mark("判定战场", "分析", stage=STAGE_MAIL)
        if coord is None:
            self.log("· 邮件里没有战场坐标，改用「战报」坐标继续")
            coord = BattleCommander(self.adb, log=self.log).read_report_coord_only()
            tm.mark("读取战报坐标", "分析", stage=STAGE_MAIL)
            head, group = None, []
            if coord is None:
                self.log("× 邮件与战报都没有可用坐标")
                return {"ok": False, "reason": "no-actionable-mail",
                        "timing": tm.as_list(),
                        "timing_by_kind": tm.totals_by_kind(),
                        "timing_by_stage": tm.totals_by_stage(),
                        "elapsed": tm.elapsed}
            self.log(f"== 战报坐标作为战场：{coord}")

        result: dict = {"ok": False, "battlefield": coord,
                        "mails": len(mails), "group": len(group),
                        "troops": [int(t) for t in troops]}

        fac = AllianceFacilities(self.adb, log=self.log)
        camp = self.locate_camp(fac, coord)
        tm.mark("定位最近可调动据点", "分析", stage=STAGE_MOBILIZE)
        if not camp:
            result["reason"] = "no-mobilize-target"
            result["timing"] = tm.as_list()
            result["timing_by_kind"] = tm.totals_by_kind()
            result["timing_by_stage"] = tm.totals_by_stage()
            result["elapsed"] = tm.elapsed
            if home and not dry_run:
                result["home_replenish"] = HomeReplenish(
                    self.adb, log=self.log, timer=tm).run()
            return result
        result["camp"] = camp.describe()
        result["camp_coord"] = list(camp.coord)
        result["camp_kind"] = camp.kind or camp.kind_text
        shots = [self.nav.shoot(f"n1_据点_{time.strftime('%H%M%S')}")]

        if dry_run:
            result["ok"] = True
            result["reason"] = "dry-run"
            result["shots"] = shots
            result["timing"] = tm.as_list()
            result["timing_by_kind"] = tm.totals_by_kind()
            result["timing_by_stage"] = tm.totals_by_stage()
            result["elapsed"] = tm.elapsed
            return result

        # 城池/港口出征成功后「调动」面板会自动收起，给它一个「重新选中据点」的回调
        mob = Mobilizer(self.adb, log=self.log,
                        reselect=lambda: fac.reselect(camp), timer=tm)
        # send_stationed=True：上一轮打完停在战场格子的部队（状态「停留」）
        # 也算可派，避免整轮因为 4 队都在「停留/调动」而 no-troop-dispatched
        mr = mob.mobilize(indexes=troops, min_strength=min_strength,
                          replenish=replenish, send_stationed=True)
        tm.mark("调动部队", "执行", stage=STAGE_MOBILIZE)
        mr.camp = camp.describe()
        shots.append(self.nav.shoot(f"n2_调动_{time.strftime('%H%M%S')}"))
        mob.close()
        shots.append(self.nav.shoot(f"n3_完成_{time.strftime('%H%M%S')}"))
        tm.mark("关闭面板/截图", "执行", stage=STAGE_MOBILIZE)

        result.pop("troops", None)      # 先丢掉请求参数里的序号列表，避免覆盖明细
        result.update(mr.as_dict())
        result["mode"] = "mobilize"
        result["shots"] = shots
        result["ok"] = bool(mr.last_arrival) or any(
            t.dispatched for t in mr.troops)
        result["failed_troops"] = [
            t.index for t in mr.troops if not t.dispatched]

        # ---- 节点三：寻找敌方进攻（邮件驱动 / 红色敌对兜底）----
        if battle and result["ok"]:
            self.log("== 开始寻找敌方进攻（邮件驱动，失败则红色敌对兜底）")
            bt = BattleFlow(self.adb, log=self.log, timer=tm).run(
                camp=camp, mail=head, fac=fac, indexes=troops,
                min_strength=min_strength, replenish=replenish,
                wait_return=wait_return, timeout=battle_timeout,
                enemy_fallback=enemy_fallback)
            shots.extend(bt.get("shots", []))
            result["battle"] = {k: v for k, v in bt.items() if k != "shots"}
            result["mode"] = "mobilize+battle"
            result["ok"] = bool(result["ok"] and bt.get("ok"))
            if not bt.get("ok"):
                result["reason"] = bt.get("reason", "battle-failed")
        elif battle:
            self.log("× 调动阶段没有部队可用，跳过敌方进攻")
            result["battle"] = {"ok": False, "reason": "no-troop-dispatched"}
            result["ok"] = False

        # ---- 节点四：回家补兵 ----
        if home:
            self.log("== 回家补兵（回城 → 亲卫营 → 检查/补兵 → 出城）")
            try:
                hr = HomeReplenish(self.adb, log=self.log, timer=tm).run()
            except Exception as e:
                hr = {"ok": False, "reason": f"exception: {e}"}
                self.log(f"  × 回家补兵异常：{e!r}")
            result["home_replenish"] = hr
            result["home_ok"] = bool(hr.get("ok"))
            if not hr.get("ok"):
                result["reason"] = (result.get("reason") or "") + "+home-failed"

        result["timing"] = tm.as_list()
        result["timing_by_kind"] = tm.totals_by_kind()
        result["timing_by_stage"] = tm.totals_by_stage()
        result["elapsed"] = tm.elapsed
        path = vision.shot_path(f"auto_line_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        tm.mark("写记录", "收尾")
        result["record"] = path
        self.log(f"== 记录已写入 {path}")
        return result


def main(argv: list[str] | None = None) -> int:
    cli.setup_console()
    ap = argparse.ArgumentParser(
        description="自动拉线（一轮）：邮件分析战场 → 调动据点 → 寻找敌方进攻 → 回家补兵")
    ap.add_argument("--limit", type=int, default=10, help="读取同盟邮件条数")
    ap.add_argument("--troops", default="1,2,3,4",
                    help="要调动的部队序号（1-4），逗号分隔")
    ap.add_argument("--dry-run", action="store_true",
                    help="只到“点选最近可调动据点”，不实际出征")
    ap.add_argument("--no-battle", action="store_true",
                    help="只调动，不执行敌方进攻")
    ap.add_argument("--no-wait", action="store_true",
                    help="出征后不等待战斗结束（直接尝试回城）")
    ap.add_argument("--battle-timeout", type=float, default=900.0,
                    help="等待战斗结束/返回的秒数（默认 900）")
    ap.add_argument("--min-strength", type=int, default=STRENGTH_FULL,
                    help=f"补兵后仍低于该兵力就跳过该队（默认 {STRENGTH_FULL}）")
    ap.add_argument("--no-replenish", action="store_true",
                    help="兵力不满也不点补兵（直接跳过不足的队伍）")
    ap.add_argument("--no-enemy-fallback", action="store_true",
                    help="邮件执行不了时不去找红色敌对目标")
    ap.add_argument("--no-home", action="store_true",
                    help="结束后不回家补兵（直接在战场收尾）")
    args = ap.parse_args(argv)

    troops = tuple(int(t) for t in args.troops.split(",") if t.strip())
    flow = MobilizeFlow()
    res = flow.run(limit=args.limit, troops=troops, dry_run=args.dry_run,
                   battle=not args.no_battle, wait_return=not args.no_wait,
                   battle_timeout=args.battle_timeout,
                   min_strength=args.min_strength,
                   replenish=not args.no_replenish,
                   enemy_fallback=not args.no_enemy_fallback,
                   home=not args.no_home)

    print("\n===== 结果 =====")
    if not res.get("ok"):
        print("未完成：", res.get("reason", "unknown"))
    print(f"战场 {res.get('battlefield')}  调动目标 {res.get('camp', '')}")
    for t in res.get("troops", []):
        if isinstance(t, dict):
            if t.get("arrived"):
                flag = "已到达"
            elif t.get("dispatched"):
                flag = "在途/已派出"
            else:
                flag = t.get("status", "")
            extra = ""
            if t.get("strength") is not None:
                extra += f" 兵力 {t['strength']}"
                if t.get("replenished"):
                    extra += "(已补兵)"
            print(f"  部队{t['index']}: 耗时 {t.get('cost') or '-'} "
                  f"到达 {t.get('arrive_at') or '-'} [{flag}]{extra}")
    if res.get("last_arrival"):
        print(f"最后到达：部队{res['last_troop']} {res['last_arrival']}")
    bt = res.get("battle")
    if isinstance(bt, dict):
        print(f"战斗：{'完成' if bt.get('ok') else '未完成'} "
              f"动作 {bt.get('action') or '-'} "
              f"回城 {'是' if bt.get('home') else '否'} "
              f"敌对标 {bt.get('enemy_target') or '-'}")
        if bt.get("reason"):
            print("  战斗原因：", bt["reason"])
    hr = res.get("home_replenish")
    if isinstance(hr, dict):
        print(f"回家补兵：{'完成' if hr.get('ok') else '未完成'} "
              f"{hr.get('note') or hr.get('reason') or ''}")
    st = res.get("timing_by_stage")
    if st:
        print("阶段耗时：" + " | ".join(f"{k} {v:.1f}s" for k, v in st.items()))
    for s in res.get("shots", []):
        print("  shot", s)
    if res.get("record"):
        print("  record", res["record"])
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
