"""战报自动收录 · 采集一轮：tcpdump + ADB 导航 + 逐卡展开 + 滑动扫描。

一轮的定义（与上游工程一致）：
1. 停在主界面 → 发现游戏 TCP 端口 → 模拟器内启动 tcpdump（root）；
2. 进「战报」→「同盟」页签 →「交战」页签，并做视觉校验；
3. 逐页展开当前屏所有折叠卡（右侧金色展开条），展开后滑动列表；
4. 连续两页画面无变化 = 到底；或达到 --max-pages / --max-seconds；
5. 停止 tcpdump、拉回 pcap，写 capture_metadata.json 与 capture_result.json。

用法：
    python scripts/capture.py --kind production
    python scripts/capture.py --kind diagnostic --max-pages 3 --report-era auto
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import common


# --------------------------------------------------------------------------- #
# 端口 / tcpdump
# --------------------------------------------------------------------------- #
def discover_ports(adb: common.Adb, config: dict) -> list[int]:
    """只取 nslg 连接的**对端端口**（ss/netstat 的第 5 列）。

    本机临时端口在同一条记录里，误当业务端口会把无关流量也抓进来。
    """
    rules = config["ports"]
    low, high = int(rules["min"]), int(rules["max"])
    ports: list[int] = []
    for command in (("ss", "-tnp"), ("netstat", "-tnp")):
        output = adb.shell(*command, allow_failure=True) or ""
        for line in output.splitlines():
            if "nslg" not in line:
                continue
            addresses = [token for token in line.split() if ":" in token]
            if len(addresses) < 2:
                continue
            peer = addresses[1].replace("[", "").replace("]", "")
            _, _, port = peer.rpartition(":")
            if not port.isdigit():
                continue
            value = int(port)
            if low <= value <= high and value not in (5555, 16384):
                ports.append(value)
        if ports:
            break
    ports = sorted(set(ports))
    if not ports:
        ports = [int(value) for value in rules["fallback"]]
    return ports


def start_tcpdump(adb: common.Adb, capture_dir: Path, ports: list[int], token: str) -> dict:
    remote_pcap = f"/data/local/tmp/{token}.pcap"
    remote_log = f"/data/local/tmp/{token}.log"
    tcpdump = common.find_tcpdump(adb) or "tcpdump"
    filter_expr = " or ".join(f"tcp port {port}" for port in ports)
    command = (
        f"{tcpdump} -i any -s 0 -U -w {remote_pcap} '{filter_expr}' "
        f"> {remote_log} 2>&1 & echo $!"
    )
    pid = (adb.shell(command, allow_failure=False) or "").strip().splitlines()[-1:]
    pid = pid[0].strip() if pid else ""
    if not pid.isdigit():
        raise RuntimeError(f"tcpdump 启动失败，PID 异常：{pid!r}（端口 {ports}）")
    return {"pid": pid, "remotePcap": remote_pcap, "remoteLog": remote_log,
            "filter": filter_expr, "binary": tcpdump}


def stop_tcpdump(adb: common.Adb, handle: dict) -> None:
    """只负责停进程并等它刷盘；远端文件由调用方 pull 之后再删。"""
    pid = handle.get("pid", "")
    if pid.isdigit():
        adb.shell("kill", "-2", pid, allow_failure=True)
        time.sleep(1.2)
        alive = adb.shell("kill", "-0", pid, allow_failure=True)
        if not (alive or "").strip():
            adb.shell("kill", "-15", pid, allow_failure=True)
            time.sleep(0.5)


# --------------------------------------------------------------------------- #
# 导航
# --------------------------------------------------------------------------- #
def _state(path: Path, config: dict | None = None) -> dict:
    if config is None:
        return common.inspect_screenshot(path)
    return common.page_state(path, config)


def open_combat_list(adb: common.Adb, config: dict, targets: dict, shot: Path) -> dict:
    """从主界面/城内（或战报页）进入「交战」列表；返回最后一次截图与状态。"""
    capture = config["capture"]
    retries = int(capture["navigateRetries"])
    tabs_timeout = int(capture["tabsTimeoutMs"]) / 1000.0
    page_delay = int(capture["pageDelayMs"]) / 1000.0
    width, height = adb.screenshot(shot)
    last_error = ""
    for attempt in range(1, retries + 1):
        state = _state(shot, config)
        if state.get("blockingOverlay"):
            shot.with_name(f"screen_blocked_{attempt}.png").write_bytes(shot.read_bytes())
            last_error = "blocking-overlay"
            adb.tap(*targets["pageBack"], wait_ms=800)
            adb.screenshot(shot)
            continue
        if not state.get("reportPage"):
            adb.tap(*targets["reportEntry"], wait_ms=400)
            ready = common.wait_until(
                lambda: _state(shot, config).get("reportPage"),
                tabs_timeout, 0.5,
            )
            if not ready:
                adb.screenshot(shot)
                last_error = "report-page-not-visible"
                continue
        if state.get("combatTabSelected"):
            wait_still(adb, config, shot)
            state = _state(shot, config)
            return {"ok": True, "attempt": attempt, "path": str(shot),
                    "width": width, "height": height, "state": state}
        # 上游习惯先点「同盟」再点「交战」；两页签互斥，最终状态以「交战」为准
        if target_is_alliance(config):
            adb.tap(*targets["allianceTab"], wait_ms=page_delay * 1000)
        for tab_try in range(1, 3):
            adb.tap(*targets["combatTab"], wait_ms=page_delay * 1000)
            adb.screenshot(shot)
            state = _state(shot, config)
            if state.get("combatTabSelected"):
                # 切页签后列表还在做进场动画，等静止再交给展开逻辑
                wait_still(adb, config, shot)
                state = _state(shot, config)
                return {"ok": True, "attempt": attempt, "path": str(shot),
                        "width": width, "height": height, "state": state,
                        "combatTabTries": tab_try}
        last_error = (f"combat-tab-not-selected(selected={state.get('selectedTab')!r}, "
                      f"gold={state.get('tabGold')}, ocr={state.get('tabOcrText')!r})")
    raise RuntimeError(f"进入交战列表失败：{last_error}")


def target_is_alliance(config: dict) -> bool:
    return bool(config.get("targets", {}).get("tapAllianceFirst", True))


# --------------------------------------------------------------------------- #
# 展开 / 滑动
# --------------------------------------------------------------------------- #
def detect_arrows(path: Path, config: dict) -> dict:
    import arrow_detect

    return arrow_detect.detect_strips(common.load_color(path), config)


def _same_strip(a: dict, b: dict) -> bool:
    """是否为同一条展开条（列表静止后按矩形比对，容差很小）。"""
    return (abs(int(a["x"]) - int(b["x"])) <= 4
            and abs(int(a["w"]) - int(b["w"])) <= 6
            and abs(int(a["y"]) - int(b["y"])) <= 8)


def _find_same(strips: list[dict], target: dict) -> dict | None:
    """找同一条竖条：先精确匹配；展开动画会把列表整体顶上去（实测约 34px），
    所以允许纵向平移 48px 再匹配一次。"""
    for item in strips:
        if _same_strip(item, target):
            return item
    for item in strips:
        if (abs(int(item["x"]) - int(target["x"])) <= 4
                and abs(int(item["w"]) - int(target["w"])) <= 6
                and abs(int(item["y"]) - int(target["y"])) <= 48):
            return item
    return None


def _is_still(diff: dict, capture: dict) -> bool:
    """两帧是否几乎一致（列表已经停下）。实测静止时 mean≈0.05、changedRatio≈0.000。"""
    return bool(diff["mean"] <= float(capture.get("stableDiffMean", 0.9))
                and diff["changedRatio"] <= float(capture.get("stableChangedRatio", 0.002)))


def wait_still(adb: common.Adb, config: dict, shot: Path,
               timeout_ms: int | None = None,
               interval_ms: int | None = None) -> bool:
    """等到「连续两帧几乎一样」为止，并把这张稳定帧写回 shot。

    为什么必须等：`input swipe` 结束后列表还会因为图片懒加载/惯性继续上顶
    1.3~1.6s（实测竖条 y 从 363 一路走到 284）。若拿没停稳的截图算点击坐标，
    落点会整体偏下 40~90px，正好压到卡片本体 —— 也就是「误进战报详情页」。
    """
    capture = config["capture"]
    timeout = max(0.3, float(timeout_ms if timeout_ms is not None
                             else capture.get("stableTimeoutMs", 2600)) / 1000.0)
    interval = max(0.05, float(interval_ms if interval_ms is not None
                               else capture.get("stableIntervalMs", 180)) / 1000.0)
    first = shot.with_name(shot.stem + "_still_a.png")
    second = shot.with_name(shot.stem + "_still_b.png")
    adb.screenshot(first)
    started = time.time()
    still = False
    while True:
        time.sleep(interval)
        adb.screenshot(second)
        if _is_still(common.visual_difference(first, second), capture):
            still = True
            break
        if time.time() - started >= timeout:
            break
        first.write_bytes(second.read_bytes())
    shot.write_bytes(second.read_bytes())
    for temp in (first, second):
        try:
            temp.unlink()
        except OSError:
            pass
    return still


def _recover_to_list(adb: common.Adb, config: dict, shot: Path,
                     targets: dict, tries: int = 3) -> bool:
    """从战果详情页回到交战列表：先点左上返回，无效再用系统 BACK 兜底。

    注意 1：详情页打开动画未结束时点击会被吞掉，所以左上返回要重试；
    注意 2：BACK 在战报页会弹出系统「确认退出游戏」（ExitActivity），
            点错就整局退出。所以 BACK 之后先把这个弹窗取消掉。
    """
    for _ in range(max(1, tries)):
        if adb.exit_dialog_visible():
            adb.cancel_exit_dialog()
        adb.tap(*targets["pageBack"], wait_ms=1300)
        adb.screenshot(shot)
        if _state(shot, config).get("reportPage"):
            return True
        time.sleep(0.4)
    adb.shell("input", "keyevent", "4")
    time.sleep(1.2)
    if adb.exit_dialog_visible():
        adb.cancel_exit_dialog()
    adb.screenshot(shot)
    return bool(_state(shot, config).get("reportPage"))


def _judge_tap(adb: common.Adb, config: dict, shot: Path,
               target: dict, before: Path) -> str:
    """点完之后「等列表停下来」再判断：ok / detail / down。

    - 先等静止：展开动画 + 列表回弹结束后坐标才可信；
    - 箭头变成 up → 展开成功；
    - 页面不再是「交战列表」→ 误入战报详情页；
    - 竖条消失但画面有明显变化 → 也视为展开成功；
    - 画面完全没变且竖条仍是 down → 点击没生效（才允许补点一次）。
    """
    capture = config["capture"]
    wait_still(adb, config, shot)
    found = _find_same(detect_arrows(shot, config).get("strips", []), target)
    if found is not None and found.get("label") == "up":
        return "ok"
    state = _state(shot, config)
    if not state.get("reportPage") or not state.get("combatTabSelected"):
        return "detail"
    diff = common.visual_difference(before, shot)
    changed = bool(diff["mean"] >= float(capture["visualDiffMean"])
                   or diff["changedRatio"] >= float(capture["changedRatio"]))
    # 竖条还在（可能还没翻面或已经滚出视野）：只要画面有明显变化就算点击生效，
    # 否则就是点击被吞了 —— 只有这种情况才允许补点一次。
    return "ok" if changed else "down"


def _collapse_strip(adb: common.Adb, config: dict, shot: Path,
                    target: dict, settle: float) -> int:
    """把刚展开的卡再点一次收起来（展开内容已进 tcpdump，不必保持展开）。

    返回 1 表示收起成功。
    """
    if not wait_still(adb, config, shot, timeout_ms=1200):
        return 0
    fresh = _find_same(detect_arrows(shot, config).get("strips", []), target)
    if fresh is None or fresh.get("label") != "up":
        return 0
    adb.tap(int(fresh["clickX"]), int(fresh["clickY"]),
            wait_ms=int(max(0.3, settle * 0.8) * 1000))
    return 1


def expand_visible_cards(adb: common.Adb, config: dict, shot: Path,
                         page: int, targets: dict) -> dict:
    """自下而上逐个点开折叠卡，点开一张、确认一张。

    关键约束（都是实测出来的坑）：
    1. **先把列表等静止，再取坐标**：滑动/展开后列表会自己往上顶 1.3~1.6s，
       用没停稳的截图算出来的点会偏下 40~90px，正好落在卡片本体 → 进战报详情页；
    2. 点击点固定取竖条水平中心 + 距底边 `clickBottomInset`（双 V 箭头处），
       不点卡片、不点场次数字；
    3. 竖条底边低于 `bottomSafeRatio` 或被屏幕截断（`cutRatio`）时先上滑，绝不硬点；
    4. 只有「画面完全没变」才允许用静止后的新坐标补点一次，避免「展开→收起」抖动。
    """
    expand = config["expand"]
    settle = int(expand.get("settleMs", 700)) / 1000.0
    collapse_after_open = bool(expand.get("collapseAfterOpen", True))
    retry_tries = max(0, int(expand.get("retryTries", 1)))
    max_recoveries = max(1, int(expand.get("maxRecoveriesPerPage", 2)))
    final_settle = max(0, int(expand.get("finalSettleMs", 350))) / 1000.0
    max_taps = max(4, int(expand["maxPerPage"]) * 4)
    screen_height = common.load_color(shot).shape[0]
    bottom_safe = int(screen_height * float(expand.get(
        "bottomSafeRatio", expand.get("safeBottomRatio", 0.90))))
    cut_limit = int(screen_height * float(expand.get("cutRatio", 0.94)))
    swipe_x = int(targets["swipe"][0])
    opened = 0
    collapsed = 0
    failed = 0
    skipped = 0
    taps = 0
    recoveries = 0
    lost = False
    skip_targets: list[dict] = []
    result: dict = {"targets": []}
    before = shot.with_name(shot.stem + "_before_tap.png")
    while taps < max_taps:
        # 1) 先等静止，再检测：坐标必须来自已经停稳的那一帧
        if not wait_still(adb, config, shot):
            adb.swipe(swipe_x, int(screen_height * 0.72),
                      swipe_x, int(screen_height * 0.58), 400)
            taps += 1
            continue
        result = detect_arrows(shot, config)
        downs = [item for item in result.get("targets", [])
                 if not any(_same_strip(item, skip) for skip in skip_targets)]
        if not downs:
            break
        target = max(downs, key=lambda item: item["clickY"])
        if (int(target["clickY"]) > bottom_safe
                or int(target["y"]) + int(target["h"]) > cut_limit):
            # 竖条贴到屏幕底边/被截断：先上滑让它完整露出来，再重新检测。
            # 滑完它要还是原地不动（已在列表末尾），就跳过这条，别原地打转。
            mark = (int(target["x"]), int(target["y"]), int(target["h"]))
            adb.swipe(swipe_x, int(screen_height * 0.80),
                      swipe_x, int(screen_height * 0.54), 450)
            taps += 1
            wait_still(adb, config, shot, timeout_ms=1800)
            again = _find_same(detect_arrows(shot, config).get("strips", []), target)
            if again is not None and (int(again["x"]), int(again["y"]),
                                      int(again["h"])) == mark:
                skip_targets.append(target)
            continue
        before.write_bytes(shot.read_bytes())
        adb.tap(int(target["clickX"]), int(target["clickY"]),
                wait_ms=int(settle * 1000))
        taps += 1
        verdict = _judge_tap(adb, config, shot, target, before)
        attempt = 0
        while verdict == "down" and attempt < retry_tries:
            # 只有「画面一点没变」才会走到这里；补点也必须用静止后的新坐标
            if not wait_still(adb, config, shot, timeout_ms=1800):
                break
            fresh = _find_same(detect_arrows(shot, config).get("strips", []), target)
            if fresh is None or fresh.get("label") == "up":
                verdict = "ok"
                break
            if (int(fresh["clickY"]) > bottom_safe
                    or int(fresh["y"]) + int(fresh["h"]) > cut_limit):
                break
            before.write_bytes(shot.read_bytes())
            adb.tap(int(fresh["clickX"]), int(fresh["clickY"]),
                    wait_ms=int(settle * 1000))
            taps += 1
            attempt += 1
            verdict = _judge_tap(adb, config, shot, fresh, before)
        if verdict == "ok":
            opened += 1
            # 展开一次就已经把该场战报数据下发给客户端（tcpdump 里已有帧），
            # 所以立刻回点同一条竖条把它收起来：列表恢复紧凑，
            # 同样的滑动距离能看到更多卡 —— 滚动更快、也更少漏展开。
            if collapse_after_open:
                collapsed += _collapse_strip(adb, config, shot, target, settle)
            # 收起后这条竖条会重新变成「展开箭头」；必须标记为已处理，
            # 否则下一轮又会挑中它，反复展开→收起直到打满 max_taps
            # （实测每页 expanded=collapsed=128，整页点击预算全耗在同一批卡上）。
            skip_targets.append(target)
            continue
        if verdict == "detail":
            failed += 1
            if _recover_to_list(adb, config, shot, targets):
                recoveries += 1
                wait_still(adb, config, shot, timeout_ms=1800)
                skip_targets.append(target)
                skipped += 1
                if recoveries >= max_recoveries:
                    break
                continue
            lost = True
            break
        failed += 1
        skip_targets.append(target)
        skipped += 1
    if final_settle > 0:
        time.sleep(final_settle)
    # 收尾再检测一次：还剩几条 down 就是「本屏没来得及展开」的，
    # 正常情况下应为 0（滑动步长小于一张卡，没展开的下次会完整露出来）
    final = detect_arrows(shot, config)
    shot.with_name(f"screen_page_{page:02d}_expanded.png").write_bytes(shot.read_bytes())
    return {"opened": opened, "collapsed": collapsed,
            "failed": failed, "skipped": skipped, "taps": taps,
            "recovered": recoveries > 0, "recoveries": recoveries, "lost": lost,
            "stripCount": len(result.get("strips", [])),
            "downsSeen": len(result.get("targets", [])),
            "leftoverDowns": len(final.get("targets", []))}


def list_moved(diff: dict, capture: dict) -> bool:
    """列表确实往下滚了（而不是页面上无关区域在动）。"""
    return bool(diff["mean"] >= float(capture["visualDiffMean"])
                and diff["changedRatio"] >= float(capture["changedRatio"]))


def estimate_shift(before: Path, after: Path) -> tuple[float | None, float]:
    """量一次滑动实际滚了多少像素（负值 = 内容上移）。

    用模板匹配而不是相位相关：列表里全是“长得差不多”的卡片，相位相关会给
    出 ±150 这种自相矛盾的结果。这里取三条横向切片分别匹配，取分数最高的那条。
    """
    cv2 = common.load_engine("cv2")
    gray_a = cv2.cvtColor(common.load_color(before), cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(common.load_color(after), cv2.COLOR_BGR2GRAY)
    if gray_a.shape != gray_b.shape:
        return None, 0.0
    height, width = gray_a.shape[:2]
    left, right = int(width * 0.13), int(width * 0.75)
    best_score, best_dy = 0.0, None
    for top in (int(height * 0.33), int(height * 0.47), int(height * 0.60)):
        patch = gray_a[top:top + int(height * 0.10), left:right]
        if patch.size == 0 or patch.shape[0] >= gray_b.shape[0]:
            continue
        result = cv2.matchTemplate(gray_b[:, left:right], patch, cv2.TM_CCOEFF_NORMED)
        if result.size == 0:
            continue
        _, score, _, location = cv2.minMaxLoc(result)
        if float(score) > best_score:
            best_score = float(score)
            best_dy = float(location[1] - top)
    if best_dy is None or best_score < 0.5:
        return None, best_score
    return best_dy, best_score


def swipe_page(adb: common.Adb, config: dict, targets: dict,
               before: Path, after: Path, page: int) -> dict:
    """向上滑一屏并判断列表是否真的移动。

    没有移动时先等一小段再复检一次：列表可能正在向服务器请求下一批战报，
    立刻判“到底”会把只加载了一半的列表当成结束。
    """
    capture = config["capture"]
    x1, y1, x2, y2, duration = targets["swipe"]
    height = int(common.load_color(before).shape[0])
    retry_y1 = int(height * float(capture.get("swipeRetryY1", 0.82)))
    retry_y2 = int(height * float(capture.get("swipeRetryY2", 0.20)))
    retry_ms = int(capture.get("swipeRetryDurationMs", 900))
    # 新帧先写临时文件，最后再拷成 screen_page_XX_after_swipe.png（干净可回溯）
    work = after.with_name(after.stem + "_work.png")
    adb.swipe(x1, y1, x2, y2, duration)
    time.sleep(int(capture["swipeDelayMs"]) / 1000.0)
    # 滑完先等列表真正静止（惯性 + 图片懒加载约 1.3~1.6s），再做“有没有移动”的判断，
    # 否则会拿半途的帧去算差异，既可能误判到底，也会让下一步点击坐标失效
    wait_still(adb, config, work)
    diff = common.visual_difference(before, work)
    moved = list_moved(diff, capture)
    attempts = 1
    if not moved:
        # 手势偶尔会被游戏吞掉（实测约 10%：同屏连滑 40 次里出现过 4 次“假到底”）。
        # 换一个更低、更慢的第二次滑动，绝不能一次没动就判列表到底。
        adb.swipe(x1, retry_y1, x2, retry_y2, retry_ms)
        time.sleep(int(capture["swipeDelayMs"]) / 1000.0)
        wait_still(adb, config, work)
        diff = common.visual_difference(before, work)
        moved = list_moved(diff, capture)
        attempts += 1
    if not moved:
        # 还没动：可能是列表正在向服务器请求下一批战报，等一会儿再复检
        time.sleep(int(capture.get("stalledProbeDelayMs") or 1500) / 1000.0)
        wait_still(adb, config, work)
        diff = common.visual_difference(before, work)
        moved = list_moved(diff, capture)
        attempts += 1
    after.write_bytes(work.read_bytes())
    try:
        work.unlink()
    except OSError:
        pass
    shift, shift_score = estimate_shift(before, after)
    state = _state(after, config)
    return {"moved": bool(moved), "difference": diff, "attempts": attempts,
            "shiftPx": round(shift, 1) if shift is not None else None,
            "shiftScore": round(shift_score, 3),
            "combatTabSelected": bool(state.get("combatTabSelected")),
            "blockingOverlay": bool(state.get("blockingOverlay")),
            "page": page}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_capture(config: dict, *, kind: str = "production", label: str = "",
                report_era: str = "", max_pages: int | None = None,
                max_seconds: int = 1800, expand: bool = True,
                output_root: str | Path | None = None) -> dict:
    adb = common.make_adb(config)
    adb.ensure_online()
    if not adb.ensure_root():
        raise RuntimeError("adbd 不是 root：无法抓全量游戏流量（先执行 adb root）")

    report = config["report"]
    label = label or str(report["label"])
    report_era = report_era or str(report["reportEra"])
    if report_era in ("", "auto"):
        try:  # 采集前对齐服务器时代表，避免上传时 era 不匹配
            import upload as _upload

            _upload.sync_era(config)
        except Exception:  # noqa: BLE001 - 服务器不可达时退回本地表
            pass
        report_era = load_active_era()

    root = common.ensure_dir(common.resolve_path(output_root) if output_root else common.CAPTURE_ROOT)
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    capture_id = f"mumu_adb_{kind}_{stamp}"
    capture_dir = common.ensure_dir(root / capture_id)
    # 工作截图单独用一个文件，screen_main/screen_page_* 只存「干净的那一帧」，
    # 这样事后翻截图能直接看出每一屏到底展开了什么，不会被后续覆盖污染
    shot = capture_dir / "work_current.png"
    main_shot = capture_dir / "screen_main.png"
    started = time.time()

    width, height = adb.screenshot(shot)
    main_shot.write_bytes(shot.read_bytes())
    state = _state(shot, config)
    if state.get("blockingOverlay"):
        raise RuntimeError("主界面检测到遮挡/弹窗：见 capture_dir/screen_main.png，先人工清理")
    targets = common.targets_for(config, width, height)
    # 已经停在战报页时先返回再重进：否则列表不会重新请求，pcap 里没有战报帧
    if state.get("reportPage") and config["capture"].get("forceReload", True):
        adb.tap(*targets["pageBack"], wait_ms=900)
        adb.screenshot(shot)
        state = _state(shot, config)
        if state.get("reportPage"):
            raise RuntimeError("无法返回上一页（仍在战报页）：见 capture_dir/screen_main.png")

    ports = discover_ports(adb, config)
    handle = start_tcpdump(adb, capture_dir, ports, f"sanmou_report_{stamp}")
    capture_meta = {
        "schemaVersion": capture_metadata_schema_version(),
        "captureKind": kind,
        "reportEra": report_era,
        "label": label,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "mumu-tcpdump",
        "pcap": str(capture_dir / "game_traffic.pcap"),
        "adbSerial": adb.serial,
        "package": config["package"],
        "gamePorts": ports,
        "orientation": state.get("orientation"),
        "screen": {},
    }
    (capture_dir / "capture_metadata.json").write_text(
        json.dumps(capture_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pages = 0
    screens = 0
    opened = 0
    collapsed = 0
    failed = 0
    recoveries = 0
    stop_reason = "not_started"
    page_log: list[dict] = []
    try:
        width, height = adb.screenshot(shot)
        capture_meta["screen"] = {"width": width, "height": height}
        targets = common.targets_for(config, width, height)
        nav = open_combat_list(adb, config, targets, shot)
        list_shot = capture_dir / "screen_list.png"
        list_shot.write_bytes(shot.read_bytes())
        nav["path"] = str(list_shot)
        nav["state"] = _state(list_shot, config)
        # maxPages=0（默认）表示不设页数上限，必须一直滑到列表真正到底；
        # hardMaxPages 只是防止界面异常导致死循环的安全上限。
        limit = int(max_pages or config["capture"].get("maxPages") or 0)
        hard_cap = max(1, int(config["capture"].get("hardMaxPages") or 500))
        if limit <= 0 or limit > hard_cap:
            limit = hard_cap
        stalled = 0
        stalled_limit = max(1, int(config["capture"]["stalledPagesToStop"]))
        for page in range(1, limit + 1):
            if time.time() - started >= max_seconds:
                stop_reason = "max_runtime"
                break
            screens = page
            entry: dict = {"page": page}
            if expand:
                result = expand_visible_cards(adb, config, shot, page, targets)
                opened += result["opened"]
                collapsed += result.get("collapsed", 0)
                failed += result["failed"]
                entry.update({"opened": result["opened"],
                              "collapsed": result.get("collapsed", 0),
                              "failed": result["failed"],
                              "skipped": result["skipped"],
                              "taps": result["taps"],
                              "strips": result.get("stripCount"),
                              "downsSeen": result.get("downsSeen"),
                              "leftoverDowns": result.get("leftoverDowns")})
                if result.get("recovered"):
                    recoveries += 1
                if result.get("lost"):
                    stop_reason = "detail_lost"
                    page_log.append(entry)
                    break
            after = capture_dir / f"screen_page_{page:02d}_after_swipe.png"
            move = swipe_page(adb, config, targets, shot, after, page)
            entry.update({"moved": move["moved"],
                          "swipeAttempts": move.get("attempts", 1),
                          "shiftPx": move.get("shiftPx")})
            page_log.append(entry)
            if move["blockingOverlay"]:
                stop_reason = "blocking_overlay"
                break
            if not move["combatTabSelected"]:
                # 多半是误入了战果/详情页：返回列表后再继续
                adb.tap(*targets["pageBack"], wait_ms=900)
                adb.screenshot(shot)
                if _state(shot, config).get("combatTabSelected"):
                    recoveries += 1
                    stalled += 1
                    continue
                stop_reason = "combat_tab_lost"
                break
            if not move["moved"]:
                stalled += 1
                if stalled >= stalled_limit:
                    stop_reason = "list_end"
                    break
            else:
                stalled = 0
                pages += 1
            # 下一屏继续用工作截图文件，页面快照保持干净、不被覆盖
            shot.write_bytes(after.read_bytes())
        if stop_reason == "not_started":
            stop_reason = "page_limit" if max_pages else "hard_page_cap"
    finally:
        stop_tcpdump(adb, handle)

    pcap = capture_dir / "game_traffic.pcap"
    try:
        adb.pull(handle["remotePcap"], pcap)
    except Exception as error:  # noqa: BLE001 - 拉取失败也要落诊断信息
        (capture_dir / "pull_error.txt").write_text(str(error), encoding="utf-8")
    try:
        adb.pull(handle["remoteLog"], capture_dir / "tcpdump.log")
    except Exception:  # noqa: BLE001
        pass
    adb.rm(handle["remotePcap"], handle["remoteLog"])

    complete = stop_reason == "list_end"
    elapsed = round(time.time() - started, 1)
    result = {
        "ok": True,
        "captureId": capture_id,
        "captureDir": str(capture_dir),
        "pcap": str(pcap),
        "pcapBytes": pcap.stat().st_size if pcap.is_file() else 0,
        "captureKind": kind,
        "reportEra": report_era,
        "gamePorts": ports,
        "screen": capture_meta.get("screen"),
        "pages": pages,
        "screens": screens,
        "complete": complete,
        "expandedCards": opened,
        "collapsedCards": collapsed,
        "expandFailures": failed,
        "navigationsRecovered": recoveries,
        "stopReason": stop_reason,
        "seconds": elapsed,
        "navigation": nav,
        # 逐屏明细：leftoverDowns>0 说明这一屏还有折叠卡没展开（正常应为 0）
        "pageLog": page_log,
    }
    # pageLog 只留在本地 capture_result.json：capture_metadata.json 有 schemaVersion 契约，
    # 不往里塞额外字段，避免上传端校验变严时被拒
    capture_meta.update({"complete": complete, "stopReason": stop_reason, "pages": pages,
                         "screens": screens})
    (capture_dir / "capture_metadata.json").write_text(
        json.dumps(capture_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (capture_dir / "capture_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result["pcapBytes"] <= 24:
        raise RuntimeError(f"pcap 为空或无效：{pcap}")
    if (not complete and kind == "production"
            and bool(config["capture"].get("requireListEnd", True))):
        (capture_dir / "incomplete.json").write_text(
            json.dumps({"stopReason": stop_reason, "pages": pages, "screens": screens,
                        "complete": False}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        raise RuntimeError(
            f"列表未翻到底（stopReason={stop_reason}, 已滑 {pages} 屏/共 {screens} 屏）："
            "本轮不算完成，已保留 capture 但不会上传；可增大 --max-seconds 或检查滑动")
    return result


def capture_metadata_schema_version() -> int:
    contract = common.load_engine("tool_contract")
    return int(getattr(contract, "CAPTURE_METADATA_SCHEMA_VERSION", 2))


def load_active_era() -> str:
    path = common.SKILL_ROOT / "reference" / "current_report_era.json"
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    eras = payload.get("eras") or []
    if not eras:
        raise RuntimeError(f"统计时代为空：{path}")
    return max(eras, key=lambda item: item["startedAt"])["eraId"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="")
    parser.add_argument("--kind", choices=("production", "diagnostic"), default="production")
    parser.add_argument("--label", default="")
    parser.add_argument("--report-era", default="")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="仅调试用；>0 会提前停止，使本轮不完整")
    parser.add_argument("--max-seconds", type=int, default=3600)
    parser.add_argument("--no-expand", action="store_true")
    parser.add_argument("--output-root", default="")
    args = parser.parse_args()
    config = common.load_config(args.config or None)
    try:
        result = run_capture(
            config, kind=args.kind, label=args.label,
            report_era=args.report_era,
            max_pages=args.max_pages or None,
            max_seconds=args.max_seconds,
            expand=not args.no_expand,
            output_root=args.output_root or None,
        )
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "reason": type(error).__name__, "error": str(error)}
    common.json_print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
