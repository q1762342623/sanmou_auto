"""Resolve the active report era from a time-based configuration."""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ERA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _parse_started_at(value: object, *, path: Path) -> datetime:
    started_at = str(value or "").strip()
    if not started_at:
        raise ValueError(f"Report era has no startedAt: {path}")
    try:
        parsed = datetime.fromisoformat(started_at)
    except ValueError as error:
        raise ValueError(f"Report-era startedAt is invalid: {path}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Report-era startedAt must include a UTC offset: {path}")
    return parsed


def normalize_report_era_config(
    payload: object, *, path: Path = Path("current_report_era.json")
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid report-era config: {path}")
    schema_version = int(payload.get("schemaVersion") or 0)
    if schema_version in {0, 1} and str(payload.get("eraId") or "").strip():
        era_id = str(payload["eraId"]).strip()
        started_at = str(payload.get("startedAt") or "").strip()
        if started_at:
            _parse_started_at(started_at, path=path)
        return {**payload, "eras": [{**payload, "eraId": era_id}]}
    if schema_version == 2:
        raw_eras = payload.get("eras")
    else:
        raise ValueError(f"Unsupported report-era config schemaVersion: {path}")
    if not isinstance(raw_eras, list) or not raw_eras:
        raise ValueError(f"Report-era config has no eras: {path}")

    eras: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_starts: set[float] = set()
    for raw in raw_eras:
        if not isinstance(raw, dict):
            raise ValueError(f"Report-era entry must be an object: {path}")
        era_id = str(raw.get("eraId") or "").strip()
        if not era_id:
            raise ValueError(f"Report-era entry has no eraId: {path}")
        started = _parse_started_at(raw.get("startedAt"), path=path)
        normalized_start = started.timestamp()
        if era_id in seen_ids:
            raise ValueError(f"Duplicate report-era ID {era_id!r}: {path}")
        if normalized_start in seen_starts:
            raise ValueError(f"Duplicate report-era startedAt {started.isoformat()!r}: {path}")
        seen_ids.add(era_id)
        seen_starts.add(normalized_start)
        eras.append({**raw, "eraId": era_id, "startedAt": str(raw["startedAt"]).strip()})
    eras.sort(key=lambda era: _parse_started_at(era["startedAt"], path=path).timestamp())
    return {**payload, "eras": eras}


def load_report_era_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Current report-era config not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return normalize_report_era_config(payload, path=path)


def report_era_schedule_sha256(payload: object) -> str:
    normalized = normalize_report_era_config(payload)
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def append_report_era(
    payload: object, era: object
) -> tuple[dict[str, Any], bool]:
    config = normalize_report_era_config(payload)
    if int(config.get("schemaVersion") or 0) != 2:
        raise ValueError("Scheduled report resets require report-era schemaVersion 2")
    if not isinstance(era, dict):
        raise ValueError("New report-era entry must be an object")
    era_id = str(era.get("eraId") or "").strip()
    if not ERA_ID_RE.fullmatch(era_id):
        raise ValueError("New report-era ID is invalid")
    started = _parse_started_at(era.get("startedAt"), path=Path("new report era"))
    started_at = started.isoformat(timespec="seconds")
    reason = str(era.get("reason") or "").strip()
    if len(reason) > 500:
        raise ValueError("New report-era reason is too long")

    for existing in config["eras"]:
        existing_started = _parse_started_at(
            existing.get("startedAt"), path=Path("current_report_era.json")
        )
        same_id = existing["eraId"] == era_id
        same_start = existing_started.timestamp() == started.timestamp()
        if same_id and same_start:
            return config, False
        if same_id:
            raise ValueError(f"Duplicate report-era ID {era_id!r}")
        if same_start:
            raise ValueError(f"Duplicate report-era startedAt {started_at!r}")

    new_era = {**era, "eraId": era_id, "startedAt": started_at}
    if reason:
        new_era["reason"] = reason
    else:
        new_era.pop("reason", None)
    updated = {**config, "eras": [*config["eras"], new_era]}
    return normalize_report_era_config(updated), True


def atomic_write_report_era_config(path: Path, payload: object) -> dict[str, Any]:
    normalized = normalize_report_era_config(payload, path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return normalized


def resolve_report_era(
    path: Path,
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    return resolve_report_era_config(load_report_era_config(path), at=at, path=path)


def resolve_report_era_config(
    payload: object, *, at: datetime | None = None,
    path: Path = Path("current_report_era.json"),
) -> dict[str, Any]:
    config = normalize_report_era_config(payload, path=path)
    if int(config.get("schemaVersion") or 0) != 2:
        return dict(config["eras"][0])
    current = at or datetime.now().astimezone()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Report-era resolution time must include a UTC offset")
    eligible = [
        era
        for era in config["eras"]
        if _parse_started_at(era["startedAt"], path=path) <= current
    ]
    if not eligible:
        raise ValueError(f"No report era has started yet: {path}")
    return dict(eligible[-1])


def report_era_by_id(path: Path, era_id: str) -> dict[str, Any] | None:
    expected = str(era_id or "").strip()
    for era in load_report_era_config(path)["eras"]:
        if era["eraId"] == expected:
            return dict(era)
    return None


def current_report_era_id(path: Path, *, at: datetime | None = None) -> str:
    return str(resolve_report_era(path, at=at)["eraId"])
