"""Upload completed capture BLOBs to the report server in resumable batches."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterable

from server_report_era import sync_server_report_era


CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
BLOB_NAME_RE = re.compile(
    r"^(?P<seq>\d{5,})_[A-Za-z0-9_]+_(?P<size>\d+)_(?P<sha>[0-9a-f]{12})\.bin$"
)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_upload_config(config_path: Path) -> dict[str, Any]:
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or int(payload.get("schemaVersion") or 0) != 1:
        raise ValueError("server upload config schemaVersion must be 1")
    base_url = str(payload.get("baseUrl") or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("server upload baseUrl must be an http or https URL")
    secret_value = str(payload.get("secret") or "").strip()
    secret_file = str(payload.get("secretFile") or "").strip()
    if secret_file:
        secret_path = Path(secret_file)
        if not secret_path.is_absolute():
            secret_path = config_path.parent / secret_path
        secret_value = secret_path.read_text(encoding="utf-8-sig").strip()
    if len(secret_value) < 32:
        raise ValueError("server upload secret must contain at least 32 characters")
    return {
        **payload,
        "baseUrl": base_url,
        "secretValue": secret_value,
        "timeoutSeconds": max(10, int(payload.get("timeoutSeconds") or 120)),
        "maxBatchBytes": max(1024 * 1024, int(payload.get("maxBatchBytes") or 32 * 1024 * 1024)),
        "maxBatchFiles": max(1, int(payload.get("maxBatchFiles") or 500)),
    }


def load_capture_metadata(capture_dir: Path, expected_era: str) -> dict[str, Any]:
    path = capture_dir / "capture_metadata.json"
    if not path.is_file():
        raise ValueError(f"capture metadata is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"capture metadata is not an object: {path}")
    if str(payload.get("captureKind") or "").strip().lower() != "production":
        raise ValueError(f"only production captures can be uploaded: {capture_dir.name}")
    era = str(payload.get("reportEra") or "").strip()
    if era != expected_era:
        raise ValueError(
            f"capture {capture_dir.name} belongs to report era {era!r}, expected {expected_era!r}"
        )
    return payload


def iter_blob_entries(capture_dir: Path, after_seq: int = 0) -> list[dict[str, Any]]:
    blob_dir = capture_dir / "blobs"
    if not blob_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in blob_dir.iterdir():
        if not path.is_file():
            continue
        match = BLOB_NAME_RE.fullmatch(path.name)
        if not match:
            continue
        seq = int(match.group("seq"))
        if seq <= after_seq:
            continue
        size = path.stat().st_size
        if size != int(match.group("size")):
            raise ValueError(f"BLOB size does not match its filename: {path}")
        sha256 = file_sha256(path)
        if not sha256.startswith(match.group("sha")):
            raise ValueError(f"BLOB hash does not match its filename: {path}")
        entries.append(
            {
                "seq": seq,
                "name": path.name,
                "size": size,
                "sha256": sha256,
                "path": path,
            }
        )
    return sorted(entries, key=lambda entry: (entry["seq"], entry["name"]))


def chunk_entries(
    entries: Iterable[dict[str, Any]], *, max_bytes: int, max_files: int
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for entry in entries:
        size = int(entry["size"])
        if current and (len(current) >= max_files or current_bytes + size > max_bytes):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(entry)
        current_bytes += size
    if current:
        chunks.append(current)
    return chunks


def create_batch_archive(
    capture_dir: Path,
    metadata: dict[str, Any],
    entries: list[dict[str, Any]],
    output_dir: Path,
) -> tuple[Path, str, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schemaVersion": 1,
        "captureId": capture_dir.name,
        "reportEra": str(metadata["reportEra"]),
        "files": [
            {
                "seq": int(entry["seq"]),
                "name": str(entry["name"]),
                "size": int(entry["size"]),
                "sha256": str(entry["sha256"]),
            }
            for entry in entries
        ],
    }
    handle, temporary_name = tempfile.mkstemp(prefix="capture_batch_", suffix=".zip", dir=output_dir)
    os.close(handle)
    archive_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3
        ) as archive:
            def zip_info(name: str) -> zipfile.ZipInfo:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                return info

            archive.writestr(
                zip_info("batch_manifest.json"),
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            )
            archive.writestr(
                zip_info("capture_metadata.json"),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            )
            for entry in entries:
                with entry["path"].open("rb") as source, archive.open(
                    zip_info(f"blobs/{entry['name']}"), "w"
                ) as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
        archive_sha = file_sha256(archive_path)
        return archive_path, archive_sha[:32], manifest
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _signature(secret: str, method: str, path: str, timestamp: str, content_sha: str) -> str:
    canonical = f"{method}\n{path}\n{timestamp}\n{content_sha}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def upload_archive(
    base_url: str,
    secret: str,
    capture_id: str,
    batch_id: str,
    archive_path: Path,
    *,
    timeout: int,
) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(base_url)
    path = f"/api/v1/batches/{capture_id}/{batch_id}"
    if parsed.path and parsed.path != "/":
        path = parsed.path.rstrip("/") + path
    content_sha = file_sha256(archive_path)
    timestamp = str(int(time.time()))
    signature = _signature(secret, "PUT", path, timestamp, content_sha)
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.putrequest("PUT", path)
        connection.putheader("Content-Type", "application/zip")
        connection.putheader("Content-Length", str(archive_path.stat().st_size))
        connection.putheader("X-Upload-Timestamp", timestamp)
        connection.putheader("X-Upload-Content-SHA256", content_sha)
        connection.putheader("X-Upload-Signature", signature)
        connection.endheaders()
        with archive_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                connection.send(chunk)
        response = connection.getresponse()
        raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"error": raw.decode("utf-8", errors="replace")}
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f"upload failed with HTTP {response.status}: {payload.get('error') or payload}"
            )
        return payload
    finally:
        connection.close()


def upload_pending_captures(
    tool_root: Path,
    config_path: Path,
    *,
    state_path: Path | None = None,
    capture_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    tool_root = tool_root.resolve()
    config_path = config_path.resolve()
    config = load_upload_config(config_path)
    server_context = sync_server_report_era(
        tool_root / "reference" / "current_report_era.json", config
    )
    server_instance_id = str(server_context["serverInstanceId"])
    state_path = state_path or tool_root / "work" / "server_upload_state.json"
    state = {"schemaVersion": 1, "captures": {}}
    if state_path.is_file():
        loaded = json.loads(state_path.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict) and int(loaded.get("schemaVersion") or 0) == 1:
            state = loaded
            state.setdefault("captures", {})
    if (state.get("serverInstanceId") != server_instance_id
            or state.get("server") != config["baseUrl"]):
        # Sequence and visibility versions have meaning only within one server corpus.
        state = {"schemaVersion": 1, "captures": {}}
    state.update({"serverInstanceId": server_instance_id, "server": config["baseUrl"]})
    era = str(server_context["reportEra"])
    capture_root = tool_root / "captures"
    batches = 0
    files = 0
    bytes_sent = 0
    last_response: dict[str, Any] = {}
    capture_results: list[dict[str, Any]] = []
    skipped_captures: list[dict[str, str]] = []
    requested_ids = list(dict.fromkeys(str(value) for value in capture_ids or []))
    if requested_ids:
        invalid = [value for value in requested_ids if not CAPTURE_ID_RE.fullmatch(value)]
        if invalid:
            raise ValueError(f"invalid capture id: {invalid[0]}")
        capture_dirs = [capture_root / value for value in requested_ids]
    else:
        capture_dirs = sorted(capture_root.iterdir() if capture_root.is_dir() else [])
    for capture_dir in capture_dirs:
        if not capture_dir.is_dir() or not CAPTURE_ID_RE.fullmatch(capture_dir.name):
            continue
        try:
            metadata = load_capture_metadata(capture_dir, era)
        except ValueError as error:
            skipped_captures.append({"captureId": capture_dir.name, "error": str(error)})
            continue
        capture_state = state["captures"].setdefault(capture_dir.name, {"maxBlobSeq": 0})
        entries = iter_blob_entries(capture_dir, int(capture_state.get("maxBlobSeq") or 0))
        capture_batches = 0
        capture_files = 0
        capture_bytes = 0
        capture_response: dict[str, Any] = {}
        for chunk in chunk_entries(
            entries,
            max_bytes=config["maxBatchBytes"],
            max_files=config["maxBatchFiles"],
        ):
            archive_path, batch_id, _manifest = create_batch_archive(
                capture_dir,
                metadata,
                chunk,
                tool_root / "work" / "upload_batches",
            )
            try:
                last_response = upload_archive(
                    config["baseUrl"],
                    config["secretValue"],
                    capture_dir.name,
                    batch_id,
                    archive_path,
                    timeout=config["timeoutSeconds"],
                )
                receipt = last_response.get("batch") or {}
                if (last_response.get("accepted") is not True
                        or receipt.get("serverInstanceId") != server_instance_id
                        or receipt.get("captureId") != capture_dir.name
                        or receipt.get("batchId") != batch_id
                        or int(receipt.get("maxBlobSeq") or 0) != max(int(e["seq"]) for e in chunk)
                        or int(receipt.get("ingestVersion") or 0) <= 0):
                    raise RuntimeError("server did not return a matching, versioned batch receipt")
                archive_bytes = archive_path.stat().st_size
                bytes_sent += archive_bytes
                capture_bytes += archive_bytes
                capture_response = last_response
            finally:
                archive_path.unlink(missing_ok=True)
            capture_state["maxBlobSeq"] = max(int(entry["seq"]) for entry in chunk)
            capture_state["serverInstanceId"] = server_instance_id
            capture_state["server"] = config["baseUrl"]
            capture_state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            batch_receipt = (
                last_response.get("batch")
                if isinstance(last_response.get("batch"), dict)
                else {}
            )
            capture_state["ingestVersion"] = max(
                int(capture_state.get("ingestVersion") or 0),
                int(batch_receipt.get("ingestVersion") or 0),
            )
            capture_state["acceptedAt"] = str(
                batch_receipt.get("acceptedAt")
                or capture_state.get("acceptedAt")
                or ""
            )
            atomic_write_json(state_path, state)
            batches += 1
            files += len(chunk)
            capture_batches += 1
            capture_files += len(chunk)
        if capture_batches:
            batch_receipt = (
                capture_response.get("batch")
                if isinstance(capture_response.get("batch"), dict)
                else {}
            )
            capture_results.append(
                {
                    "captureId": capture_dir.name,
                    "serverInstanceId": server_instance_id,
                    "server": config["baseUrl"],
                    "batches": capture_batches,
                    "files": capture_files,
                    "bytes": capture_bytes,
                    "maxBlobSeq": int(capture_state["maxBlobSeq"]),
                    "ingestVersion": int(batch_receipt.get("ingestVersion") or 0),
                    "acceptedAt": str(batch_receipt.get("acceptedAt") or ""),
                    "serverStatus": capture_response,
                }
            )
    return {
        "schemaVersion": 1,
        "server": config["baseUrl"],
        "serverInstanceId": server_instance_id,
        "uploadedBatches": batches,
        "uploadedFiles": files,
        "uploadedBytes": bytes_sent,
        "requestedCaptures": requested_ids,
        "captures": capture_results,
        "skippedCaptures": skipped_captures,
        "reportEra": era,
        "serverStatus": last_response,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--capture-id", action="append", default=[])
    parser.add_argument("--sync-era-only", action="store_true", help="Synchronize the server era schedule without uploading")
    args = parser.parse_args()
    tool_root = args.tool_root.resolve()
    config = args.config or tool_root / "control" / "server_upload.json"
    if args.sync_era_only:
        context = sync_server_report_era(
            tool_root / "reference" / "current_report_era.json", load_upload_config(config)
        )
        print(json.dumps({key: context[key] for key in (
            "reportEra", "serverInstanceId", "scheduleUpdated", "scheduleSha256"
        )}, ensure_ascii=False))
        return 0
    result = upload_pending_captures(
        tool_root,
        config,
        state_path=args.state,
        capture_ids=args.capture_id,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
