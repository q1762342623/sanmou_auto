"""功能「自动拉线」：一轮 = 邮件分析战场 → 调动据点 → 寻找敌方进攻 → 回家补兵。

四个节点（计时器 `timing.StepTimer` 的 stage 字段）：
1. **邮件分析战场**：读「同盟」页签最近 N 封邮件，解析关键字（集结/保护/蹲坑/占领/
   攻占/梭哈/拉线…，黑名单「虫子」跳过），取最新一条可执行指令的坐标作为战场
2. **调动据点**：找离战场最近、且能「调动」的盟归属据点（同盟建筑 + 城池列表），
   逐支把 部队1-4 派过去（出征前读「兵力 N」，不满先点白色补兵，补完仍 <门槛就跳过）
2.5 **低兵力歇兵**：补不满的部队在其他队伍调动成功后，送到附近城池自动征兵；
   每轮发起调动前先把已征满的部队召回主城（`sanmou_auto/city_rest.py`）
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
    python -m sanmou_auto.flow_mobilize --no-city-rest    # 关闭进城歇兵/召回
"""
from __future__ import annotations

import argparse
import json
import math
import time

from . import camp_cache, cli, config, intent, vision
from .adb import Adb
from .alliance import NO_MOBILIZE_HINTS, AllianceFacilities, Camp
from .city_rest import CityRest
from .battle import BattleCommander, BattleFlow
from .home import HomeReplenish
from .mail import MailBody, MailReader
from .mapnav import MapNav
from .mobilize import STRENGTH_FULL, Mobilizer
from .sandbox import SandboxJump
from .timing import STAGE_MAIL, STAGE_MOBILIZE, StepTimer

CLUSTER_RADIUS = 6      # 判定“同一个战场”的坐标距离（格）
CAMP_CACHE_TRY = 6      # 每轮最多用缓存校验几个候选据点（按距离由近到远）


class MobilizeFlow:
    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.reader = MailReader(self.adb, log=log)
        self.nav = MapNav(self.adb, log=log)
        self.log = log
        # 据点全量快照：启动时从 screenshots/flow/camp_cache.json 载入，
        # 之后每轮只做「选点 + 有没有调动按钮」的校验，不再重复扫描。
        self._camps: list[Camp] = []
        self._camps_loaded = False

    # ------------------------------------------------------------------
    def read_mails(self, limit: int = 10, tabs: tuple[str, ...] = ("同盟",)):
        self.log(f"== 读取邮件（{list(tabs)}，上限 {limit} 封）")
        mails = self.reader.read_latest(limit=limit, tabs=list(tabs))
        self.reader.back()
        return mails

    def battlefield(self, mails: list[MailBody]):
        """（见下方 docstring）"""
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
    def camps(self) -> list[Camp]:
        """当前据点全量快照（供歇兵选城等复用，避免各自再扫一次）。"""
        return self._load_camps()

    def _load_camps(self) -> list[Camp]:
        """读据点缓存（只读磁盘一次）；没有缓存时返回空列表。"""
        if self._camps_loaded:
            return self._camps
        self._camps_loaded = True
        camps, meta = camp_cache.load_camps()
        if camps:
            self._camps = camps
            self.log(f"== 载入据点缓存 {len(camps)} 个"
                     f"（{meta.get('scannedAtIso') or '-'}"
                     f"{'，已超 6h 建议重扫' if meta.get('stale') else ''}）")
        return self._camps

    def _sandbox_locate(self, fac: AllianceFacilities,
                        coord: tuple[int, int]) -> Camp | None:
        """沙盘快跳：按距离取缓存候选据点，逐个「缩放→坐标→前往→点中心」。

        注意不能直接跳战场格：战场格多半是敌方/中立地块（如 663,754 的建业台），
        右侧只有「攻城」没有「调动」。要跳的是**己方可调动据点的坐标**。
        实测单次 ~15~20s（含 2~3 次捏合）；失败候选立即从缓存剔除并试下一个，
        最多试 2 个，之后交给原有缓存/列表链路兜底。
        """
        camps = self._load_camps()
        if not camps:
            return None
        ordered = camp_cache.sort_by_distance(camps, coord)
        # 上一轮成功过的据点排最前：战场通常就在附近，一次命中概率很高。
        last_ok = camp_cache.get_last_ok()
        if last_ok:
            ordered.sort(key=lambda c: 0 if tuple(c.coord) == tuple(last_ok) else 1)
        # 候选最多试 3 个；每次沙盘跳内部还有 4 次坐标校验重试。
        for c in ordered[:3]:
            try:
                sb = SandboxJump(self.adb, log=self.log)
                if not sb.goto((int(c.coord[0]), int(c.coord[1]))):
                    self.log("  ! 没能进入沙盘页，回退缓存/列表定位")
                    return None
                if fac.has_mobilize_button():
                    self.log(f"== 沙盘快跳命中：{c.describe()}"
                             f"（距目标 {c.dist(coord):.1f}）")
                    camp_cache.set_last_ok(c.coord)
                    return c
                self.log(f"  ! ({c.coord[0]},{c.coord[1]}) 跳转后没有「调动」→ 剔除缓存")
                camp_cache.invalidate(c.coord)
                self._camps = [x for x in self._camps
                               if tuple(x.coord) != tuple(c.coord)]
            except Exception as e:  # noqa: BLE001 - 快路径失败不阻断主链路
                self.log(f"  ! 沙盘快跳异常：{e!r}")
                return None
        return None

    def _reselect_camp(self, fac: AllianceFacilities, camp: Camp) -> bool:
        """出征后「调动」面板自动收起时重开：优先沙盘重跳，失败再走列表重选。"""
        try:
            sb = SandboxJump(self.adb, log=self.log)
            sb.goto((int(camp.coord[0]), int(camp.coord[1])), tries=4)
            if fac.has_mobilize_button():
                self.log(f"  ✓ 沙盘重跳回 {camp.describe()}")
                camp_cache.set_last_ok(camp.coord)
                return True
        except Exception as e:  # noqa: BLE001
            self.log(f"  ! 沙盘重跳异常：{e!r}")
        return fac.reselect(camp)

    def locate_camp(self, fac: AllianceFacilities,
                    coord: tuple[int, int]) -> Camp | None:
        """找最近可调动据点：**先查缓存，全部失效才重扫**。

        旧实现只在「战场移动 ≤60 格」时复用内存缓存、且只试最近 2 个候选，
        一旦候选被占 / 没有「调动」按钮就退回全量扫描（单轮 2~5 分钟）。
        现在改为「一次全量查询 + 持久化缓存」：
        1. 启动后第一次调动前扫一次全量据点，写入 camp_cache.json；
        2. 之后每轮按距离由近到远校验候选（选点 + 是否有「调动」），命中即用；
        3. 校验失败的据点立即从缓存剔除，只有全部候选都不可用才重扫并覆盖缓存。
        """
        # 快路径：沙盘坐标直跳（~10~15s），命中就不用再进同盟列表逐屏 OCR。
        camp = self._sandbox_locate(fac, coord)
        if camp is not None:
            return camp

        camps = self._load_camps()
        if camps:
            cands = camp_cache.sort_by_distance(camps, coord)[:CAMP_CACHE_TRY]
            for i, c in enumerate(cands):
                self.log(f"  · 缓存候选 {c.describe()} 距目标 {c.dist(coord):.1f}")
                # select() 返回 True 就代表跳转后已校验到「调动」按钮，
                # 不再重复调 has_mobilize_button()（整屏按钮 OCR，一次 5~10s）。
                # 只有最后一个候选才允许「回顶全表重扫」，避免单个失效据点
                # 拖掉 2~5 分钟（实测 378s 就是这么来的）。
                if fac.select(c, full_scan=(i == len(cands) - 1)):
                    self.log(f"== 最近可调动据点（缓存命中）：{c.describe()}"
                             f"（距目标 {c.dist(coord):.1f}）")
                    return c
                self.log(f"  ! 据点 {c.coord} 选不中或没有「调动」→ 从缓存剔除")
                camp_cache.invalidate(c.coord)
                self._camps = [x for x in self._camps
                               if tuple(x.coord) != tuple(c.coord)]
            self.log("  · 缓存候选全部不可用，重新扫描同盟据点")
        else:
            self.log("  · 没有据点缓存，先全量扫描一次（后续复用）")

        camps = fac.scan_all()
        camps = [c for c in camps
                 if not any(h in c.note for h in NO_MOBILIZE_HINTS)]
        self._camps = camps
        self._camps_loaded = True
        if camps:
            path = camp_cache.save_camps(camps, battlefield=coord)
            self.log(f"  · 据点缓存已更新：{len(camps)} 个 → {path}")
        if not camps:
            return None
        return fac.goto_nearest(coord, camps=camps)

    # ------------------------------------------------------------------
    def run(self, limit: int = 10, troops: tuple[int, ...] = (1, 2, 3, 4),
            dry_run: bool = False, battle: bool = True,
            wait_return: bool = True, battle_timeout: float = 900.0,
            min_strength: int = STRENGTH_FULL, replenish: bool = True,
            enemy_fallback: bool = True, timer: StepTimer | None = None,
            home: bool = True, city_rest: bool = True,
            city_rest_state: str | None = None,
            wait_hook=None, safe_point=None) -> dict:
        """`wait_hook(seconds)`：等待阶段分片回调（自动拉线据此插入战报收集）；
        `safe_point(reason)`：世界地图上的节点边界回调（超时未收集时补一次）。
        """
        # 调动范围固定为 1-4 队
        troops = tuple(int(t) for t in troops if 1 <= int(t) <= 4) or (1, 2, 3, 4)
        tm = timer or StepTimer()
        self.adb.ensure_online()
        tm.mark("连接设备", "准备", stage=STAGE_MAIL)
        self.nav.ensure_map()
        tm.mark("回到地图", "执行", stage=STAGE_MAIL)
        if safe_point is not None:
            safe_point("本轮开始")

        fac = AllianceFacilities(self.adb, log=self.log)
        rest = CityRest(self.adb, fac=fac, log=self.log, timer=tm,
                        state_path=city_rest_state)
        rest_recall: dict | None = None
        if city_rest:
            self.log("== 检查歇兵城池：把已征满的部队召回主城")
            try:
                rest_recall = rest.recall_ready()
                self.log(f"  检查 {rest_recall.get('checked', 0)} 支，"
                         f"召回 {len(rest_recall.get('recalled') or [])} 支，"
                         f"仍在歇兵 {len(rest_recall.get('remaining') or [])} 支")
            except Exception as e:  # 歇兵是增益功能，异常不阻断主线
                rest_recall = {"ok": False, "reason": f"exception: {e}"}
                self.log(f"  ! 歇兵召回异常：{e!r}")
            tm.mark("召回歇兵部队", "执行", stage=STAGE_MOBILIZE)

        mails = self.read_mails(limit=limit)
        tm.mark("读取/解析邮件", "分析", stage=STAGE_MAIL)
        if safe_point is not None:
            safe_point("邮件读完")
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
        if city_rest and rest_recall is not None:
            result["city_rest_recall"] = _rest_summary(rest_recall)

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

        # 歇兵登记只代表「这队之前兵力不足被送进城」，不能整轮踢出参战池：
        # 召回经常失败（竖屏找不到撤回按钮），而其中可能已有征满的部队。
        # 按要求「1-4 谁有兵谁出战」——照常把全部部队交给 mobilize，
        # 由 ensure_strength 单队判定：达标/可补满就出征，否则只跳过这一队。
        active_troops = troops
        if city_rest:
            held = {e.troop for e in rest.entries()}
            resting = sorted(t for t in held if t in troops)
            if resting:
                result["city_rest_held"] = resting
                self.log(f"  · 部队{resting} 仍在城池歇兵：本轮照常尝试，"
                         "兵力达标且可派就出征，否则只跳过该队")
        if not active_troops:
            self.log("× 没有要调动的部队，本轮只做收尾")
            result["ok"] = False
            result["reason"] = "no-troops-requested"
            result["timing"] = tm.as_list()
            result["timing_by_kind"] = tm.totals_by_kind()
            result["timing_by_stage"] = tm.totals_by_stage()
            result["elapsed"] = tm.elapsed
            if home:
                result["home_replenish"] = HomeReplenish(
                    self.adb, log=self.log, timer=tm).run()
                result["home_ok"] = bool(result["home_replenish"].get("ok"))
            return result

        # 城池/港口出征成功后「调动」面板会自动收起，给它一个「重新选中据点」的回调
        mob = Mobilizer(self.adb, log=self.log,
                        reselect=lambda: self._reselect_camp(fac, camp), timer=tm)
        # send_stationed=True：上一轮打完停在战场格子的部队（状态「停留」）
        # 也算可派，避免整轮因为 4 队都在「停留/调动」而 no-troop-dispatched
        mr = mob.mobilize(indexes=active_troops, min_strength=min_strength,
                          replenish=replenish, send_stationed=True)
        tm.mark("调动部队", "执行", stage=STAGE_MOBILIZE)
        mr.camp = camp.describe()
        shots.append(self.nav.shoot(f"n2_调动_{time.strftime('%H%M%S')}"))
        mob.close()
        shots.append(self.nav.shoot(f"n3_完成_{time.strftime('%H%M%S')}"))
        tm.mark("关闭面板/截图", "执行", stage=STAGE_MOBILIZE)
        if safe_point is not None:
            safe_point("调动结束")

        result.pop("troops", None)      # 先丢掉请求参数里的序号列表，避免覆盖明细
        result.update(mr.as_dict())
        result["mode"] = "mobilize"
        result["shots"] = shots
        result["ok"] = bool(mr.last_arrival) or any(
            t.dispatched for t in mr.troops)
        result["failed_troops"] = [
            t.index for t in mr.troops if not t.dispatched]

        # ---- 节点二点五：低兵力部队进城歇兵（城池自动征兵）----
        if city_rest:
            low = tuple(t.index for t in mr.troops
                        if t.skipped_reason == "low-strength")
            others = any(t.dispatched for t in mr.troops
                         if t.index not in low and t.dispatched)
            try:
                send = rest.send_low_strength(low, coord, camp=camp,
                                              allow_send=others, camps=self.camps())
            except Exception as e:  # 可选增益，异常不阻断进攻
                send = {"ok": False, "reason": f"exception: {e}"}
                self.log(f"  ! 低兵力进城异常：{e!r}")
            result["city_rest_send"] = _rest_summary(send)
            if low:
                result["low_strength_troops"] = list(low)
            tm.mark("低兵部队进城歇兵", "执行", stage=STAGE_MOBILIZE)

        # ---- 节点三：寻找敌方进攻（邮件驱动 / 红色敌对兜底）----
        if battle and result["ok"]:
            self.log("== 开始寻找敌方进攻（邮件驱动，失败则红色敌对兜底）")
            bt = BattleFlow(self.adb, log=self.log, timer=tm).run(
                camp=camp, mail=head, fac=fac, indexes=troops,
                min_strength=min_strength, replenish=replenish,
                wait_return=wait_return, timeout=battle_timeout,
                enemy_fallback=enemy_fallback, wait_hook=wait_hook)
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
        if safe_point is not None:
            safe_point("战斗结束")

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
            if safe_point is not None:
                safe_point("回家补兵结束")

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


def _rest_summary(payload: dict | None) -> dict:
    """把城市歇兵/召回结果裁成落盘用的精简结构。"""
    if not isinstance(payload, dict):
        return {}
    keys = ("ok", "reason", "checked", "recalled", "pending", "dropped",
            "remaining", "sent", "city", "statuses")
    return {key: payload[key] for key in keys if key in payload}


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
    ap.add_argument("--no-city-rest", action="store_true",
                    help="关闭「低兵力进城歇兵 / 征满召回」")
    ap.add_argument("--city-rest-state", default="",
                    help="歇兵状态文件（默认 screenshots/flow/city_rest.json）")
    args = ap.parse_args(argv)

    troops = tuple(int(t) for t in args.troops.split(",") if t.strip())
    flow = MobilizeFlow()
    res = flow.run(limit=args.limit, troops=troops, dry_run=args.dry_run,
                   battle=not args.no_battle, wait_return=not args.no_wait,
                   battle_timeout=args.battle_timeout,
                   min_strength=args.min_strength,
                   replenish=not args.no_replenish,
                   enemy_fallback=not args.no_enemy_fallback,
                   home=not args.no_home,
                   city_rest=not args.no_city_rest,
                   city_rest_state=args.city_rest_state or None)

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
    cr = res.get("city_rest_recall")
    if isinstance(cr, dict):
        print(f"歇兵召回：检查 {cr.get('checked', 0)} 支，"
              f"召回 {len(cr.get('recalled') or [])} 支，"
              f"剩余 {len(cr.get('remaining') or [])} 支")
    cs = res.get("city_rest_send")
    if isinstance(cs, dict):
        sent = [e.get("troop") for e in (cs.get("sent") or []) if isinstance(e, dict)]
        print(f"歇兵进城：{'完成' if cs.get('ok') else '未执行'} "
              f"{cs.get('city') or cs.get('reason') or ''} 部队 {sent}")
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
