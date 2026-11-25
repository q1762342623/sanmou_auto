"""指令 7 · 战报收集：采集 → 提取 → **异步上传** → 取最高频次交战坐标。

输入：无（内部用 `ReportCollector`：`截图 + tcpdump + 提取 + 上传`）
输出：
- `hotspot`：最高频次交战坐标 `[x, y]`（就是本轮战场）
- `records` / `blobs` / `pages` / `uploadOk`：有效战报数量与上传结果
- `pendingUploads`：上传失败的待补交批次（异步补交，不丢战报）

这条指令同时兼任「等待填充」：调度器在任意两条非收集指令之间都会先跑它，
所以它必须自己保证「一次调用 = 一次完整收集」。
"""
from __future__ import annotations

from ..hotspot import HotspotTracker
from .base import CommandSpec, Timer

SPEC = CommandSpec(
    key="reports", title="战报收集", module="sanmou_auto.commands.collect_reports",
    inputs=(), outputs=("hotspot", "records", "blobs", "pages", "uploadOk",
                        "pendingUploads", "collectSeconds"),
    notes="取缔邮件分析：战场只来自本轮收集的 hottest 坐标",
)


def run(collector, *, tracker: HotspotTracker | None = None,
        trigger: str = "指令", log=print) -> dict:
    """跑一次完整战报收集并更新热点。"""
    timer = Timer()
    enabled = bool(getattr(collector, "enabled", True)) if collector is not None else False
    if collector is None or not enabled:
        hotspot = tracker.coord() if tracker is not None else None
        log("== [战报收集] 收集器未启用（--no-report），沿用历史热点"
            + (f" {list(hotspot)}" if hotspot else "（暂无）"))
        return {"ok": hotspot is not None, "hotspot": list(hotspot) if hotspot else None,
                "enabled": False, "seconds": timer.seconds}

    result = collector.collect(trigger=f"指令:{trigger}") or {}
    if tracker is not None:
        tracker.update(result, trigger=f"指令:{trigger}")
    hotspot = tracker.coord() if tracker is not None else None
    status = {}
    try:
        status = collector.status() or {}
    except Exception:              # noqa: BLE001 - 状态只是附加信息
        status = {}

    ok = bool(result.get("ok")) if result else False
    log("== [战报收集] "
        + (f"ok 记录{result.get('completeRecords')}条/"
           f"BLOB{result.get('uniqueBlobs')}/{result.get('pages')}页"
           f"上传={'ok' if result.get('uploadOk') else '待补交'}"
           if ok else f"失败：{result.get('error') or result.get('reason') or '未知'}"))
    if hotspot:
        log(f"   当前热点 {hotspot}")

    return {
        "ok": ok,
        "hotspot": list(hotspot) if hotspot else None,
        "records": int(result.get("completeRecords") or 0),
        "blobs": int(result.get("uniqueBlobs") or 0),
        "pages": int(result.get("pages") or 0),
        "uploadOk": bool(result.get("uploadOk")),
        "captureId": result.get("captureId"),
        "collectSeconds": result.get("roundSeconds"),
        "pendingUploads": status.get("pendingUploads") or [],
        "collections": status.get("count"),
        "failures": status.get("failures"),
        "error": result.get("error") or result.get("reason"),
        "enabled": True,
        "seconds": timer.seconds,
    }
