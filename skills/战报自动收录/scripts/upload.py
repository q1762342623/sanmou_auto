"""战报自动收录 · 增量上传到报表服务器 + 等可见。

上传契约（内置引擎 upload_capture_batches.py）：
  PUT {baseUrl}/api/v1/batches/<captureId>/<batchId>
  Header: X-Upload-Timestamp / X-Upload-Content-SHA256 / X-Upload-Signature
  签名: HMAC-SHA256(secret, "PUT\\n<path>\\n<timestamp>\\n<contentSha256>")
  ZIP 内只含 batch_manifest.json + capture_metadata.json + blobs/，pcap 与截图不上传。
同一批次重复提交幂等；服务器逐文件校验 size + sha256，拒绝 diagnostic / 跨 era。

可见性：上传成功只代表服务器已入库排队；要等 /api/live 的 visibleIngestVersion
覆盖回执里的 ingestVersion，才算出现在网页报表里。

用法：
    python scripts/upload.py --capture-id mumu_adb_production_20261121_193000_000
    python scripts/upload.py --capture-id <id> --no-wait
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

import common


def upload_captures(config: dict, capture_ids: list[str],
                    config_path: str | Path | None = None) -> dict:
    uploader = common.load_engine("upload_capture_batches")
    path = common.resolve_path(config_path or config["server"]["configPath"])
    state_path = common.WORK_DIR / "server_upload_state.json"
    common.ensure_dir(common.WORK_DIR)
    result = uploader.upload_pending_captures(
        common.SKILL_ROOT,
        path,
        state_path=state_path,
        capture_ids=capture_ids or None,
    )
    result["ok"] = bool(result.get("captures")) or not capture_ids
    return result


def sync_era(config: dict, path: str | Path | None = None) -> dict:
    """从服务器拉 /api/report-era-schedule，原子写回本地时代表。

    本地表过旧会让 capture 的 reportEra 与服务器不一致，上传被拒（skippedCaptures）。
    服务器接口是公开只读的，采集前同步一次即可。
    """
    target = common.resolve_path(path) if path else (
        common.SKILL_ROOT / "reference" / "current_report_era.json")
    url = base_url(config).rstrip("/") + "/api/report-era-schedule"
    import os

    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    schedule = payload.get("schedule") or {}
    eras = schedule.get("eras") or []
    if not eras:
        raise RuntimeError(f"服务器时代表为空：{url}")
    text = json.dumps(schedule, ensure_ascii=False, indent=2) + "\n"
    current = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
    changed = current.strip() != text.strip()
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    active = max(eras, key=lambda item: item["startedAt"])["eraId"]
    return {"ok": True, "reportEra": active, "eras": len(eras),
            "scheduleSha256": payload.get("scheduleSha256", ""),
            "updated": changed, "path": str(target)}


def base_url(config: dict) -> str:
    """优先取 config.server.baseUrl，否则读上传配置里的 baseUrl。"""
    explicit = str(config["server"].get("baseUrl") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    path = common.resolve_path(config["server"]["configPath"])
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    value = str(payload.get("baseUrl") or "").strip()
    if not value:
        raise RuntimeError(f"上传配置缺少 baseUrl：{path}")
    return value.rstrip("/")


def live_status(base_url: str) -> dict:
    url = base_url.rstrip("/") + "/api/live"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_visible(config: dict, upload_result: dict) -> dict:
    """轮询 /api/live，直到 visibleIngestVersion 覆盖本次上传的最大 ingestVersion。"""
    server = config["server"]
    timeout = float(server["waitVisibleSeconds"])
    interval = float(server["pollSeconds"])
    targets = [item for item in upload_result.get("captures", [])
               if int(item.get("ingestVersion") or 0) > 0]
    if not targets:
        return {"ok": False, "reason": "nothing-uploaded", "live": _safe_live(config)}
    required = max(int(item["ingestVersion"]) for item in targets)
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = _safe_live(config)
        if int(last.get("visibleIngestVersion") or 0) >= required:
            return {"ok": True, "requiredIngestVersion": required,
                    "visibleIngestVersion": last.get("visibleIngestVersion"),
                    "buildId": last.get("buildId"), "live": last}
        time.sleep(interval)
    return {"ok": False, "reason": "visible-wait-timeout",
            "requiredIngestVersion": required,
            "visibleIngestVersion": last.get("visibleIngestVersion"),
            "lastError": last.get("lastError"), "live": last}


def _safe_live(config: dict) -> dict:
    try:
        return live_status(base_url(config))
    except Exception as error:  # noqa: BLE001
        return {"error": str(error)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="")
    parser.add_argument("--upload-config", default="")
    parser.add_argument("--capture-id", action="append", default=[])
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--sync-era", action="store_true", help="只同步服务器时代表")
    args = parser.parse_args()
    config = common.load_config(args.config or None)
    if args.wait_seconds:
        config["server"]["waitVisibleSeconds"] = args.wait_seconds
    try:
        if args.sync_era:
            common.json_print(sync_era(config))
            return 0
        result = upload_captures(config, args.capture_id, args.upload_config or None)
        if result.get("ok") and args.capture_id and not args.no_wait:
            result["visibility"] = wait_visible(config, result)
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "reason": type(error).__name__, "error": str(error)}
    common.json_print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
