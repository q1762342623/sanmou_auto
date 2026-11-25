"""指令 6 · 部队撤回：把「参战被打回据点」或「已补满兵力」的部队撤回主城。

前置条件：本轮执行过一次有效行军，且行军到位已超过 5 分钟。
输入：
- `troops`：参与行军的部队编号
- `arriveAt`：行军到达时间（HH:MM:SS，用来判断「到位是否超过 5 分钟」）
- `replenish`：参与补兵的部队编号（这些部队补满后也要撤回）

输出：无（内部完成「右侧头像 → 回城 → 勾『今天登录不再提示』→ 确定」）

判定方案（用户口径）：
- **需要撤回**：部队至少进行过一次行军，且当前处于据点中（不在行军/出征/排队）；
- **是否补满**：连点两次右侧补兵部队头像 → 打开「部队详情」→ 汇总兵力 ≥ 3.3W；
  详情打不开时退化为「据点面板里读到的兵力」。
"""
from __future__ import annotations

import time

from .. import vision, ocrengine, config
from ..mapnav import MapNav
from ..mobilize import STRENGTH_FULL
from ..troop_return import TroopReturn
from .base import CommandSpec, Timer, seconds_until, now_hhmmss

SPEC = CommandSpec(
    key="return", title="部队撤回", module="sanmou_auto.commands.return_troops",
    inputs=("troops", "arriveAt", "replenish"), outputs=(),
    preconditions=("本轮已有效行军且到位超过 5 分钟",),
    notes="右侧头像→回城；「今日登录不再提示」勾选后确定",
)

# 「部队详情」弹窗的正文区域（竖屏实测：头像/兵力/兵力汇总都在中央大面板里）
DETAIL_BOX = config._p((380, 240, 1240, 720), (150, 420, 750, 1200))
DETAIL_CLOSE = config._p((1500, 95), (820, 160))


def _numbers_in(spans) -> list[int]:
    out: list[int] = []
    for span in spans:
        text = (span.text or "").replace(" ", "").replace(",", "")
        digits = "".join(ch if ch.isdigit() else " " for ch in text).split()
        for chunk in digits:
            if 1 <= len(chunk) <= 6:
                out.append(int(chunk))
    return out


def detail_strength(adb, idx: int, *, nav: MapNav | None = None, log=print) -> int | None:
    """连点两次头像打开「部队详情」，读汇总兵力；读不到返回 None。"""
    nav = nav or MapNav(adb, log=log)
    returner = TroopReturn(adb, log=log, nav=nav)
    try:
        nav.ensure_map()
        if not returner.tap_avatar(idx):
            log(f"  · 部队{idx} 点头像失败，读不到详情")
            return None
        time.sleep(0.35)
        returner.tap_avatar(idx)       # 连点两次 → 打开「部队详情」
        time.sleep(2.0)
        img = adb.screencap()
        spans = ocrengine.ocr(vision.crop(img, _scaled(img, DETAIL_BOX)), 2.5)
        texts = "".join(s.text for s in spans)
        if "兵力" not in texts and "部队" not in texts:
            log(f"  · 部队{idx} 没打开部队详情（屏幕上没有兵力字样）")
            return None
        values = [v for v in _numbers_in(spans) if 1000 <= v <= 99999]
        if not values:
            return None
        best = max(values)
        log(f"  · 部队{idx} 详情兵力读数 {sorted(values)[-3:]} → {best}")
        return best
    except Exception as error:      # noqa: BLE001
        log(f"  ! 部队{idx} 读部队详情异常：{error!r}")
        return None
    finally:
        try:
            adb.tap(*DETAIL_CLOSE)
            time.sleep(0.8)
        except Exception:           # noqa: BLE001
            pass


def _scaled(img, box):
    """把当前方向基准坐标换算到实际截图尺寸（截图被缩放时）。"""
    k = img.width / config.SCREEN[0]
    if abs(k - 1.0) < 1e-3:
        return box
    return (max(0, round(box[0] * k)), max(0, round(box[1] * k)),
            min(img.width, round(box[2] * k)), min(img.height, round(box[3] * k)))


def _at_camp(states: dict[int, dict], idx: int) -> bool:
    row = states.get(idx) or {}
    return not row.get("moving") and not row.get("queued")


def run(*, troops, arriveAt: str = "", replenish=(), adb=None, log=print,
        nav: MapNav | None = None, check_full: bool = True,
        full_threshold: int = STRENGTH_FULL) -> dict:
    timer = Timer()
    marched = [int(t) for t in (troops or []) if 1 <= int(t) <= 4]
    replenishing = [int(t) for t in (replenish or []) if 1 <= int(t) <= 4]
    out: dict = {"ok": False, "returned": [], "failed": [],
                 "full": [], "skipped": [], "waitSeconds": None}
    if not marched and not replenishing:
        log("  · [部队撤回] 没有需要撤回的部队")
        out["ok"] = True
        out["skipped"] = True
        return out

    nav = nav or MapNav(adb, log=log)
    nav.ensure_map()
    returner = TroopReturn(adb, log=log, nav=nav)
    try:
        states = {row["index"]: row for row in returner.read_avatars_clean()}
    except Exception as error:      # noqa: BLE001
        log(f"  ! [部队撤回] 读头像状态失败：{error!r}")
        states = {}

    # 行军到位是否已超过 5 分钟（未到就等，交给调度器继续收集战报）
    if arriveAt:
        left = seconds_until(arriveAt, now_hhmmss())
        if left is not None and left > -300:
            out["waitSeconds"] = round(-left, 1)
            log(f"  · [部队撤回] 行军到位未满 5 分钟（还剩 {-left:.0f}s 才到 5 分钟），先不撤")
            if not replenishing:
                return out

    candidates = [i for i in marched if _at_camp(states, i)]
    still_busy = [i for i in marched if i not in candidates]
    out["skipped"] = still_busy
    if still_busy:
        log(f"  · [部队撤回] 部队{still_busy} 仍在行军/排队，本次不撤")

    # 补兵部队：补满才撤
    full_now: list[int] = []
    for idx in replenishing:
        if check_full:
            value = detail_strength(adb, idx, nav=nav, log=log)
            if value is not None and value >= full_threshold:
                full_now.append(idx)
            elif value is not None:
                log(f"  · 部队{idx} 仍未补满（{value}/{full_threshold}），继续补兵")
        if idx in candidates and idx not in full_now:
            candidates = [c for c in candidates if c != idx]
    out["full"] = full_now
    to_return = sorted(set(candidates) | set(full_now))
    if not to_return:
        log("  · [部队撤回] 本轮没有满足条件的部队（未被打回 / 补兵未满）")
        out["ok"] = True
        out["seconds"] = timer.seconds
        return out

    log(f"== [部队撤回] 逐支回城：{to_return}")
    try:
        result = returner.return_all(tuple(to_return))
    except Exception as error:      # noqa: BLE001
        log(f"  ! [部队撤回] 异常：{error!r}")
        out["reason"] = repr(error)
        out["seconds"] = timer.seconds
        return out
    out["returned"] = result.get("returned") or []
    out["failed"] = result.get("failed") or []
    out["ok"] = bool(out["returned"])
    if not out["ok"]:
        out["reason"] = "return-failed"
    log(f"== [部队撤回] 已回城 {out['returned']}"
        + (f"，失败 {out['failed']}" if out["failed"] else ""))
    out["seconds"] = timer.seconds
    return out
