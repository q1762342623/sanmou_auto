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


def expand_visible_cards(adb: common.Adb, config: dict, shot: Path,
                         page: int, targets: dict) -> dict:
    """自下而上逐个点开折叠卡；每点一次都重新检测，确认变成「收起」才算成功。

    误点进「战果」详情页时立刻返回，并结束本页展开（继续点只会更乱）。
    """
    expand = config["expand"]
    settle = int(expand["settleMs"]) / 1000.0
    max_taps = int(expand["maxPerPage"]) * 2
    opened = 0
    failed = 0
    skipped = 0
    taps = 0
    recovered = False
    result: dict = {"targets": []}
    while taps < max_taps:
        result = detect_arrows(shot, config)
        downs = result.get("targets", [])
        if not downs:
            break
        target = max(downs, key=lambda item: item["clickY"])
        adb.tap(int(target["clickX"]), int(target["clickY"]), wait_ms=int(settle * 1000))
        taps += 1
        adb.screenshot(shot)
        state_after = _state(shot, config)
        if not state_after.get("reportPage"):
            # 点到了卡片本体，进了战果页：返回列表收工
            adb.tap(*targets["pageBack"], wait_ms=900)
            adb.screenshot(shot)
            recovered = True
            failed += 1
            break
        still_down = [item for item in detect_arrows(shot, config).get("targets", [])
                      if abs(item["clickY"] - target["clickY"]) <= 60]
        if still_down:
            failed += 1
            continue
        opened += 1
    shot.with_name(f"screen_page_{page:02d}_expanded.png").write_bytes(shot.read_bytes())
    return {"opened": opened, "failed": failed, "skipped": skipped, "taps": taps,
            "recovered": recovered, "stripCount": len(result.get("strips", []))}


def swipe_page(adb: common.Adb, config: dict, targets: dict,
               before: Path, after: Path, page: int) -> dict:
    x1, y1, x2, y2, duration = targets["swipe"]
    adb.swipe(x1, y1, x2, y2, duration)
    time.sleep(int(config["capture"]["swipeDelayMs"]) / 1000.0)
    adb.screenshot(after)
    diff = common.visual_difference(before, after)
    capture = config["capture"]
    moved = (diff["mean"] >= float(capture["visualDiffMean"])
             and diff["changedRatio"] >= float(capture["changedRatio"]))
    state = _state(after, config)
    return {"moved": bool(moved), "difference": diff,
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
    shot = capture_dir / "screen_main.png"
    started = time.time()

    width, height = adb.screenshot(shot)
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
    opened = 0
    failed = 0
    recoveries = 0
    stop_reason = "not_started"
    try:
        width, height = adb.screenshot(shot)
        capture_meta["screen"] = {"width": width, "height": height}
        targets = common.targets_for(config, width, height)
        nav = open_combat_list(adb, config, targets, shot)
        list_shot = capture_dir / "screen_list.png"
        list_shot.write_bytes(shot.read_bytes())
        limit = int(max_pages or config["capture"]["maxPages"])
        stalled = 0
        for page in range(1, limit + 1):
            if time.time() - started >= max_seconds:
                stop_reason = "max_runtime"
                break
            if expand:
                result = expand_visible_cards(adb, config, shot, page, targets)
                opened += result["opened"]
                failed += result["failed"]
                if result.get("recovered"):
                    recoveries += 1
            after = capture_dir / f"screen_page_{page:02d}_after_swipe.png"
            move = swipe_page(adb, config, targets, shot, after, page)
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
                if stalled >= int(config["capture"]["stalledPagesToStop"]):
                    stop_reason = "list_end"
                    break
            else:
                stalled = 0
                pages += 1
            shot = after
        if stop_reason == "not_started":
            stop_reason = "page_limit"
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
        "expandedCards": opened,
        "expandFailures": failed,
        "navigationsRecovered": recoveries,
        "stopReason": stop_reason,
        "seconds": elapsed,
        "navigation": nav,
    }
    (capture_dir / "capture_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result["pcapBytes"] <= 24:
        raise RuntimeError(f"pcap 为空或无效：{pcap}")
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
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--max-seconds", type=int, default=1800)
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
