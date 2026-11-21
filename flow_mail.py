"""功能一：读邮件 -> 解析关键字（集结/保护/蹲坑/占领/攻占/梭哈）->
跳到坐标 -> 找最近可调动据点点击 -> 截图。

两种模式：
- camp（默认）：跳到坐标后，在 同盟建筑 + 同盟城池（城/水寨/军港/民港）里
  找最近且能调动的据点并点击，截图
- action：跳到坐标后，按关键字点 攻城/占领/行军/驻守 按钮，停在确认面板前截图

用法:
    python -m sanmou_auto.flow_mail                    # 读 10 封，执行第一条可执行指令
    python -m sanmou_auto.flow_mail --limit 5
    python -m sanmou_auto.flow_mail --tabs 同盟
    python -m sanmou_auto.flow_mail --mode action      # 真正点击出兵按钮（不点确认）
    python -m sanmou_auto.flow_mail --all              # 逐条执行所有可执行指令
    python -m sanmou_auto.flow_mail --dry-run          # 只读邮件并解析，不点地图
"""
from __future__ import annotations

import argparse
import time

from . import cli, intent
from .adb import Adb
from .alliance import AllianceFacilities
from .mapnav import MapNav
from .mail import MailBody, MailReader

# 动作 -> 地块面板上的按钮文案候选
ACTION_BUTTONS = {
    intent.OCCUPY: ("占领", "攻城", "攻占"),
    intent.MARCH: ("行军", "出征"),
    intent.GARRISON: ("驻守", "调动"),
}


class MailFlow:
    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.reader = MailReader(self.adb, log=log)
        self.nav = MapNav(self.adb, log=log)
        self.log = log

    # ------------------------------------------------------------------
    def read(self, limit: int = 10, tabs: tuple[str, ...] = ("同盟", "法令")):
        self.log(f"== 读取邮件（上限 {limit} 封，页签 {list(tabs)}）")
        mails = self.reader.read_latest(limit=limit, tabs=list(tabs))
        self.reader.back()
        return mails

    # ------------------------------------------------------------------
    def _jump_to_mail_coord(self, mail: MailBody, tag: str,
                            result: dict) -> MailBody | None:
        """打开邮件 -> 点正文坐标 -> 确认跳转。返回正文，失败返回 None。"""
        body = self.reader.open_mail_at(mail)
        result["shots"].append(self.nav.shoot(f"m1_正文_{tag}"))
        if body.coord_point is None:
            self.log("  × 正文中没有可点击坐标，跳过")
            self.reader.back()
            return None

        self.log(f"== 点击坐标 {body.intent.coord} @{body.coord_point}")
        self.adb.tap(*body.coord_point)
        time.sleep(1.4)
        if self.nav.jump_dialog_visible():
            result["shots"].append(self.nav.shoot(f"m2_跳转确认_{tag}"))
            self.nav.confirm_jump(3.0)
        else:
            self.log("  ! 未检测到跳转弹窗，按已跳转处理")
        return body

    def _do_action(self, body: MailBody, tag: str, result: dict) -> bool:
        """按关键字点出兵按钮。成功返回 True。"""
        names = ACTION_BUTTONS.get(body.intent.action or "", ())
        if not names:
            return False
        buttons = self.nav.find_all_buttons()
        self.log("  地块按钮: " + (", ".join(t for t, _ in buttons) or "无"))
        hit = next(((t, p) for t, p in buttons if any(n in t for n in names)), None)
        if not hit:
            self.log(f"  ! 当前状态没有 {names} 按钮（无法攻占/不可用），"
                     f"改为寻路到最近可调动据点")
            return False
        self.log(f"== 点击操作按钮「{hit[0]}」@{hit[1]}")
        self.adb.tap(*hit[1])
        time.sleep(2.0)
        result["shots"].append(self.nav.shoot(f"m4_操作_{tag}"))
        result["action_button"] = hit[0]
        return True

    def _goto_nearest_camp(self, coord: tuple[int, int], tag: str,
                          result: dict) -> bool:
        """同盟建筑 + 同盟城池：点最近可调动据点的坐标（会跳转并选中）+ 截图。"""
        fac = AllianceFacilities(self.adb, log=self.log)
        camp = fac.goto_nearest(coord)
        if camp:
            result["camp"] = camp.describe()
            result["camp_coord"] = list(camp.coord)
            result["camp_kind"] = camp.kind or camp.kind_text
            result["camp_dist"] = round(camp.dist(coord), 1)
            result["shots"].append(self.nav.shoot(f"m5_据点_{tag}"))
            btns = self.nav.find_all_buttons()
            result["camp_buttons"] = [t for t, _ in btns]
            self.log("  据点按钮: " + (", ".join(result["camp_buttons"]) or "无"))
            result["shots"].append(self.nav.shoot(f"m6_最终_{tag}"))
            return True

        # 兜底：走「定位 → 同盟」列表（列表内容会变，仅作备用）
        self.log("  ! 设施列表取不到，改用定位面板")
        return self._goto_camp_via_locate(coord, tag, result)

    def _goto_camp_via_locate(self, coord: tuple[int, int], tag: str,
                              result: dict) -> bool:
        """备用路线：定位 → 同盟 → 最近同盟大营 点击 + 截图。"""
        self.nav.open_locate()
        self.nav.select_locate_tab("同盟")
        camps = self.nav.scan_camps()
        for c in sorted(camps, key=lambda c: c.dist(coord))[:6]:
            self.log(f"  同盟大营 {c.describe()} 距目标 {c.dist(coord):.0f}")
        near = self.nav.nearest_camp(camps, coord)
        if not near:
            self.log("  × 定位面板里没有找到「同盟大营」")
            self.nav.close_locate()
            return False

        self.log(f"== 点击最近同盟大营 {near.describe()}")
        if not self.nav.select_camp(near):
            self.log("  × 列表里没点到该同盟大营")
            self.nav.close_locate()
            return False

        result["camp"] = near.describe()
        result["camp_dist"] = round(near.dist(coord), 1)
        result["shots"].append(self.nav.shoot(f"m5_据点_{tag}"))
        self.nav.close_locate()        # 收起定位面板，露出大营详情
        result["shots"].append(self.nav.shoot(f"m6_最终_{tag}"))
        btns = self.nav.find_all_buttons()
        result["camp_buttons"] = [t for t, _ in btns]
        self.log("  大营按钮: " + (", ".join(result["camp_buttons"]) or "无"))
        return True

    # ------------------------------------------------------------------
    def execute(self, mail: MailBody, mode: str = "camp") -> dict:
        """对一封邮件执行：跳转坐标 -> (点按钮 | 找最近同盟大营点击) -> 截图。"""
        tag = f"{mail.tab}{mail.slot}_{time.strftime('%H%M%S')}"
        result: dict = {"title": mail.title, "shots": [], "ok": False,
                        "mode": mode}

        self.nav.close_locate()   # 保证进邮件页前地图上没有叠面板
        body = self._jump_to_mail_coord(mail, tag, result)
        if body is None:
            return result

        coord = body.intent.coord
        result["shots"].append(self.nav.shoot(f"m3_已跳转_{tag}"))

        if mode == "action" and self._do_action(body, tag, result):
            result["ok"] = True
            return result

        if coord:
            result["ok"] = self._goto_nearest_camp(coord, tag, result)
        return result

    # ------------------------------------------------------------------
    def run(self, limit: int = 10, tabs: tuple[str, ...] = ("同盟", "法令"),
            mode: str = "camp", run_all: bool = False,
            dry_run: bool = False) -> dict:
        self.adb.ensure_online()
        mails = self.read()

        actionable = [m for m in mails if m.intent and m.intent.actionable]
        self.log(f"== 共读 {len(mails)} 封，其中 {len(actionable)} 封含可执行指令")
        for m in mails:
            self.log("   - " + (m.intent.describe() if m.intent else m.title))

        if dry_run or not actionable:
            return {"mails": len(mails), "actionable": len(actionable),
                    "results": []}

        targets = actionable if run_all else actionable[:1]
        results = []
        for m in targets:
            self.log(f"== 执行：{m.intent.describe()}")
            results.append(self.execute(m, mode=mode))
        return {"mails": len(mails), "actionable": len(actionable),
                "results": results}


def main(argv: list[str] | None = None) -> int:
    cli.setup_console()
    ap = argparse.ArgumentParser(description="邮件关键字 -> 跳坐标 -> 最近可调动据点")
    ap.add_argument("--limit", type=int, default=10, help="读取邮件条数")
    ap.add_argument("--tabs", default="同盟,法令", help="页签，逗号分隔")
    ap.add_argument("--mode", choices=("camp", "action"), default="camp",
                    help="camp=点最近同盟大营（默认）；action=点攻城/行军/驻守")
    ap.add_argument("--action", action="store_true",
                    help="等价于 --mode action")
    ap.add_argument("--all", action="store_true", help="执行所有可执行指令")
    ap.add_argument("--dry-run", action="store_true", help="只读邮件不操作地图")
    args = ap.parse_args(argv)

    mode = "action" if args.action else args.mode
    tabs = tuple(t.strip() for t in args.tabs.split(",") if t.strip())
    flow = MailFlow()
    res = flow.run(limit=args.limit, tabs=tabs, mode=mode,
                   run_all=args.all, dry_run=args.dry_run)

    print("\n===== 结果 =====")
    print(f"读取 {res['mails']} 封，可执行 {res['actionable']} 条")
    for r in res["results"]:
        status = "成功" if r["ok"] else "未完成"
        extra = r.get("camp") or r.get("action_button") or ""
        print(f"- {r['title']} -> {status} {extra}")
        for s in r["shots"]:
            print(f"    {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
