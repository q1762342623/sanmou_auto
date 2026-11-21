"""Synchronize client era schedules from the authoritative report server."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from report_era import (
    atomic_write_report_era_config,
    load_report_era_config,
    normalize_report_era_config,
    report_era_schedule_sha256,
    resolve_report_era_config,
)


def fetch_json(config: dict[str, Any], endpoint: str) -> dict[str, Any]:
    request = urllib.request.Request(
        config["baseUrl"].rstrip("/") + endpoint,
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(request, timeout=min(30, int(config["timeoutSeconds"]))) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError(f"server response is invalid: {endpoint}")
    return payload


def sync_server_report_era(era_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    # A reset can happen between the two GETs. Retry that race before changing disk.
    for _attempt in range(3):
        response = fetch_json(config, "/api/report-era-schedule")
        schedule = normalize_report_era_config(response.get("schedule"))
        digest = report_era_schedule_sha256(schedule)
        if response.get("scheduleSha256") != digest:
            raise RuntimeError("server report-era schedule hash is invalid; local schedule retained")
        live = fetch_json(config, "/api/live")
        if not live.get("ok") or not live.get("serverInstanceId"):
            raise RuntimeError("server identity unavailable; local schedule retained")
        active_era = str(resolve_report_era_config(schedule)["eraId"])
        if live.get("reportEra") != active_era:
            continue
        try:
            local_digest = report_era_schedule_sha256(load_report_era_config(era_path))
        except (OSError, ValueError):
            local_digest = ""
        changed = digest != local_digest
        if changed:
            # Existing capture metadata is intentionally never rewritten here.
            atomic_write_report_era_config(era_path, schedule)
        return {**live, "scheduleUpdated": changed, "scheduleSha256": digest}
    raise RuntimeError(
        "server active era does not match its schedule; check client clock and server configuration"
    )
