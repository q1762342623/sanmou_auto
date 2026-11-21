#!/usr/bin/env python3
"""Extract decoded battle-report list-card blobs from a MuMu tcpdump pcap."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import socket
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import dpkt

import decode_list_card_sproto_like as cards
from tool_contract import CAPTURE_METADATA_SCHEMA_VERSION


DEFAULT_XOR_KEY = bytes.fromhex("3330d32fba040800")
DEFAULT_GAME_PORTS = (20020, 20050, 20060, 20090)
TRANSPORT_HEADER_SIZE = 9
RPC_PREFIX_SIZE = 2
MAX_FRAME_SIZE = 0xFFFF


@dataclass(frozen=True)
class TcpPayload:
    timestamp: float
    sequence: int
    data: bytes


@dataclass(frozen=True)
class TransportFrame:
    timestamp: float
    source: tuple[str, int]
    target: tuple[str, int]
    index: int
    data: bytes


class PcapExtractionError(ValueError):
    pass


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for value in data:
        counts[value] += 1
    total = len(data)
    return -sum(
        (count / total) * math.log2(count / total)
        for count in counts
        if count
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def xor_bytes(data: bytes, key: bytes = DEFAULT_XOR_KEY) -> bytes:
    if not key:
        raise ValueError("XOR key cannot be empty")
    return bytes(value ^ key[index % len(key)] for index, value in enumerate(data))


def merge_tcp_payloads(payloads: Iterable[TcpPayload]) -> list[tuple[float, int, bytes]]:
    """Merge ordered or retransmitted TCP payloads into contiguous byte chunks."""
    chunks: list[tuple[float, int, bytearray]] = []
    for payload in sorted(payloads, key=lambda item: (item.sequence, item.timestamp)):
        if not payload.data:
            continue
        if not chunks:
            chunks.append((payload.timestamp, payload.sequence, bytearray(payload.data)))
            continue
        timestamp, start, current = chunks[-1]
        end = start + len(current)
        if payload.sequence > end:
            chunks.append((payload.timestamp, payload.sequence, bytearray(payload.data)))
            continue
        overlap = end - payload.sequence
        if overlap >= len(payload.data):
            continue
        current.extend(payload.data[max(0, overlap) :])
        chunks[-1] = (timestamp, start, current)
    return [(timestamp, start, bytes(data)) for timestamp, start, data in chunks]


def parse_length_prefixed_frames(
    data: bytes, *, allow_trailing_partial: bool = False
) -> list[bytes]:
    """Parse two-byte big-endian frames where the length excludes its prefix."""
    frames: list[bytes] = []
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            if allow_trailing_partial and frames:
                break
            raise PcapExtractionError("truncated transport length prefix")
        size = int.from_bytes(data[offset : offset + 2], "big")
        if size <= 0 or size > MAX_FRAME_SIZE:
            raise PcapExtractionError(f"invalid transport frame size: {size}")
        start = offset + 2
        end = start + size
        if end > len(data):
            if allow_trailing_partial and frames:
                break
            raise PcapExtractionError(
                f"truncated transport frame: need {size} bytes, have {len(data) - start}"
            )
        frames.append(data[start:end])
        offset = end
    return frames


def _xor_key_candidates_for_frame(frame: bytes) -> tuple[bytes, ...]:
    encoded = frame[TRANSPORT_HEADER_SIZE:]
    if len(encoded) < 10:
        return ()
    payload_size = len(encoded) - 10
    encoded_size = payload_size.to_bytes(4, "little")
    return tuple(
        bytes(
            (
                encoded[8] ^ encoded_size[2],
                encoded[9] ^ encoded_size[3],
                encoded[2] ^ first_tag,
                encoded[3],
                encoded[4],
                encoded[5],
                encoded[6] ^ encoded_size[0],
                encoded[7] ^ encoded_size[1],
            )
        )
        for first_tag in range(1, 9)
    )


def resynchronize_length_prefixed_frames(data: bytes) -> tuple[list[bytes], int]:
    """Find a transport boundary when tcpdump began midway through a frame."""
    scan_limit = min(max(0, len(data) - 2), MAX_FRAME_SIZE + 2)
    best_score = (0, 0, 0, 0)
    best_start: int | None = None
    next_offsets: dict[int, int] = {}
    for start in range(scan_limit):
        size = int.from_bytes(data[start : start + 2], "big")
        end = start + 2 + size
        if size >= TRANSPORT_HEADER_SIZE + RPC_PREFIX_SIZE and end <= len(data):
            next_offsets[start] = end

    for start in range(scan_limit):
        position = start
        candidates: Counter[bytes] = Counter()
        frame_count = 0
        while frame_count < 32 and position in next_offsets:
            end = next_offsets[position]
            candidates.update(
                _xor_key_candidates_for_frame(data[position + 2 : end])
            )
            frame_count += 1
            position = end
        support = candidates.most_common(1)[0][1] if candidates else 0
        score = (support, frame_count, position - start, -start)
        if score > best_score:
            best_score = score
            best_start = start

    if best_start is None or best_score[0] < 2:
        raise PcapExtractionError("could not resynchronize transport frames")

    # Walk predecessor boundaries back to the earliest frame that joins the
    # high-confidence chain. This retains reports before a repetitive run.
    reachable = {best_start}
    for start in range(best_start - 1, -1, -1):
        if next_offsets.get(start) in reachable:
            reachable.add(start)
    alignment = min(reachable)
    return (
        parse_length_prefixed_frames(
            data[alignment:], allow_trailing_partial=True
        ),
        alignment,
    )


def endpoint(raw_ip: bytes, port: int) -> tuple[str, int]:
    family = socket.AF_INET6 if len(raw_ip) == 16 else socket.AF_INET
    return socket.inet_ntop(family, raw_ip), port


def read_tcp_flows(pcap_path: Path) -> dict[
    tuple[tuple[str, int], tuple[str, int]], list[TcpPayload]
]:
    flows: dict[
        tuple[tuple[str, int], tuple[str, int]], list[TcpPayload]
    ] = {}
    with pcap_path.open("rb") as stream:
        try:
            reader = dpkt.pcap.Reader(stream)
            datalink = reader.datalink()
            link_parsers = {
                dpkt.pcap.DLT_EN10MB: dpkt.ethernet.Ethernet,
                dpkt.pcap.DLT_LINUX_SLL: dpkt.sll.SLL,
                dpkt.pcap.DLT_LINUX_SLL2: dpkt.sll2.SLL2,
            }
            link_parser = link_parsers.get(datalink)
            if link_parser is None:
                raise PcapExtractionError(f"unsupported pcap datalink type: {datalink}")
            for timestamp, raw in reader:
                try:
                    link = link_parser(raw)
                    ip = link.data
                    tcp = ip.data
                    if not isinstance(tcp, dpkt.tcp.TCP) or not tcp.data:
                        continue
                    source = endpoint(ip.src, tcp.sport)
                    target = endpoint(ip.dst, tcp.dport)
                except (AttributeError, ValueError, dpkt.UnpackError):
                    continue
                key = (source, target)
                flows.setdefault(key, []).append(
                    TcpPayload(float(timestamp), int(tcp.seq), bytes(tcp.data))
                )
        except (ValueError, dpkt.NeedData) as exc:
            raise PcapExtractionError(f"invalid pcap: {pcap_path}") from exc
    return flows


def iter_incoming_frames(
    pcap_path: Path,
    game_ports: set[int],
) -> tuple[list[TransportFrame], list[dict[str, Any]]]:
    frames: list[TransportFrame] = []
    skipped_chunks: list[dict[str, Any]] = []
    for (source, target), payloads in read_tcp_flows(pcap_path).items():
        if source[1] not in game_ports:
            continue
        frame_index = 0
        for timestamp, sequence, chunk in merge_tcp_payloads(payloads):
            try:
                parsed = parse_length_prefixed_frames(chunk)
            except PcapExtractionError as exc:
                try:
                    parsed, alignment = resynchronize_length_prefixed_frames(chunk)
                except PcapExtractionError:
                    alignment = 0
                    try:
                        parsed = parse_length_prefixed_frames(
                            chunk, allow_trailing_partial=True
                        )
                    except PcapExtractionError:
                        parsed = []
                skipped_chunks.append(
                    {
                        "source": f"{source[0]}:{source[1]}",
                        "target": f"{target[0]}:{target[1]}",
                        "sequence": sequence,
                        "size": len(chunk),
                        "error": str(exc),
                        "completeFramesRetained": len(parsed),
                        "resynchronizedOffset": alignment,
                    }
                )
                if not parsed:
                    continue
            for data in parsed:
                frames.append(
                    TransportFrame(timestamp, source, target, frame_index, data)
                )
                frame_index += 1
    return frames, skipped_chunks


def inspect_list_card_blob(data: bytes) -> tuple[int, int]:
    """Return (raw records, complete report cards) for a decoded candidate."""
    try:
        top_fields = cards.parse_object(data)
        if not top_fields:
            return 0, 0
        first = top_fields[0]
        if first.kind != "data" or first.start is None or first.size is None:
            return 0, 0
        items = cards.parse_array(data, first.start, first.size)
    except (cards.ParseError, IndexError, ValueError):
        return 0, 0

    complete = 0
    digest = hashlib.sha256(data).hexdigest()[:12]
    for index, (base, size, fields) in enumerate(items):
        try:
            record = cards.decode_record(
                data,
                digest,
                index,
                base,
                size,
                fields,
                {},
            )
        except (cards.ParseError, IndexError, UnicodeError, ValueError):
            continue
        sides = record.get("sides") or []
        if not record.get("payload_time") or not record.get("coord") or len(sides) != 2:
            continue
        if all((side.get("identity") or {}).get("player") for side in sides) and all(
            side.get("heroes") for side in sides
        ):
            complete += 1
    return len(items), complete


def decode_transport_frame(
    frame: bytes,
    *,
    xor_key: bytes = DEFAULT_XOR_KEY,
    validator: Callable[[bytes], tuple[int, int]] = inspect_list_card_blob,
) -> tuple[bytes, int, int, bytes] | None:
    if len(frame) <= TRANSPORT_HEADER_SIZE + RPC_PREFIX_SIZE:
        return None
    header = frame[:TRANSPORT_HEADER_SIZE]
    decoded = xor_bytes(frame[TRANSPORT_HEADER_SIZE:], xor_key)
    rpc_prefix = decoded[:RPC_PREFIX_SIZE]
    candidate = decoded[RPC_PREFIX_SIZE:]
    raw_records, complete_records = validator(candidate)
    if raw_records <= 0 or complete_records <= 0:
        return None
    return candidate, raw_records, complete_records, header + rpc_prefix


def derive_xor_key_candidates(frames: Iterable[TransportFrame]) -> list[tuple[bytes, int]]:
    """Infer repeating XOR keys from common response-envelope length fields."""
    support: dict[bytes, int] = {}
    for frame in frames:
        # Ordinary RPC responses begin with a small sproto field tag followed
        # by three zero bytes and a four-byte envelope payload length.
        for key in _xor_key_candidates_for_frame(frame.data):
            support[key] = support.get(key, 0) + 1
    return sorted(support.items(), key=lambda item: item[1], reverse=True)


def discover_xor_key(
    frames: list[TransportFrame],
    *,
    validator: Callable[[bytes], tuple[int, int]] = inspect_list_card_blob,
) -> bytes:
    best_key: bytes | None = None
    best_score = (0, 0, 0)
    candidates = derive_xor_key_candidates(frames)
    if not candidates:
        raise PcapExtractionError("could not derive a MuMu XOR key candidate")
    maximum_support = candidates[0][1]
    minimum_support = max(1, math.ceil(maximum_support * 0.5))
    for key, key_support in candidates:
        if key_support < minimum_support:
            break
        raw_records = 0
        complete_records = 0
        for frame in frames:
            decoded = decode_transport_frame(
                frame.data,
                xor_key=key,
                validator=validator,
            )
            if decoded is not None:
                raw_records += decoded[1]
                complete_records += decoded[2]
        score = (complete_records, raw_records, key_support)
        if score > best_score:
            best_key = key
            best_score = score
    if best_key is None or best_score[0] <= 0:
        raise PcapExtractionError("could not discover a validated MuMu XOR key")
    return best_key


def ensure_capture_metadata(
    capture_dir: Path,
    *,
    capture_kind: str,
    report_era: str,
    label: str,
    pcap_path: Path,
) -> None:
    metadata_path = capture_dir / "capture_metadata.json"
    if metadata_path.exists():
        payload = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        if payload.get("captureKind") != capture_kind:
            raise PcapExtractionError("existing capture metadata kind does not match")
        if payload.get("reportEra") != report_era:
            raise PcapExtractionError("existing capture metadata era does not match")
        return
    metadata_path.write_text(
        json.dumps(
            {
                "schemaVersion": CAPTURE_METADATA_SCHEMA_VERSION,
                "captureKind": capture_kind,
                "reportEra": report_era,
                "label": label,
                "createdAt": datetime.now().isoformat(timespec="milliseconds"),
                "source": "mumu-tcpdump",
                "pcap": str(pcap_path),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def extract_pcap(
    pcap_path: Path,
    capture_dir: Path,
    *,
    game_ports: set[int],
    xor_key: bytes | None,
    capture_kind: str,
    report_era: str,
    label: str,
) -> dict[str, Any]:
    if not pcap_path.is_file():
        raise FileNotFoundError(f"pcap not found: {pcap_path}")
    capture_dir.mkdir(parents=True, exist_ok=True)
    blob_dir = capture_dir / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    ensure_capture_metadata(
        capture_dir,
        capture_kind=capture_kind,
        report_era=report_era,
        label=label,
        pcap_path=pcap_path,
    )

    frames, skipped_chunks = iter_incoming_frames(pcap_path, game_ports)
    xor_key_source = "explicit"
    if xor_key is None:
        xor_key = discover_xor_key(frames)
        xor_key_source = "auto"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_records = 0
    complete_records = 0
    for frame in frames:
        decoded = decode_transport_frame(frame.data, xor_key=xor_key)
        if decoded is None:
            continue
        blob, frame_raw_records, frame_complete_records, transport_header = decoded
        sha = hashlib.sha256(blob).hexdigest()
        if sha in seen:
            continue
        seen.add(sha)
        sequence = len(rows) + 1
        path = blob_dir / f"{sequence:05d}_mumu_tcp_{len(blob)}_{sha[:12]}.bin"
        path.write_bytes(blob)
        captured_at = datetime.fromtimestamp(frame.timestamp).isoformat(timespec="milliseconds")
        row = {
            "seq": sequence,
            "time": captured_at,
            "type": "blob",
            "func": "mumu_tcp_xor",
            "size": len(blob),
            "returnAddress": "",
            "returnOffset": "",
            "text": "",
            "blobSeq": sequence,
            "sha256": sha,
            "entropy": round(entropy(blob), 4),
            "path": str(path),
            "first16Hex": blob[:16].hex(),
            "sourceEndpoint": f"{frame.source[0]}:{frame.source[1]}",
            "targetEndpoint": f"{frame.target[0]}:{frame.target[1]}",
            "pcapFrameIndex": frame.index,
            "transportHeaderHex": transport_header.hex(),
            "rawRecordCount": frame_raw_records,
            "completeRecordCount": frame_complete_records,
        }
        rows.append(row)
        raw_records += frame_raw_records
        complete_records += frame_complete_records

    index_path = capture_dir / "binary_string_blobs.jsonl"
    timeline_path = capture_dir / "timeline.jsonl"
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    index_path.write_text(content, encoding="utf-8")
    timeline_path.write_text(content, encoding="utf-8")
    summary = {
        "pcap": str(pcap_path),
        "pcapSha256": file_sha256(pcap_path),
        "captureDir": str(capture_dir),
        "gamePorts": sorted(game_ports),
        "xorKeyHex": xor_key.hex(),
        "xorKeySource": xor_key_source,
        "transportFrames": len(frames),
        "skippedTcpChunks": skipped_chunks,
        "uniqueBlobs": len(rows),
        "rawRecords": raw_records,
        "completeRecords": complete_records,
    }
    (capture_dir / "mumu_packet_extract.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_xor_key(value: str) -> bytes:
    try:
        key = bytes.fromhex(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("XOR key must be hexadecimal") from exc
    if not key:
        raise argparse.ArgumentTypeError("XOR key cannot be empty")
    return key


def configure_output_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> int:
    configure_output_encoding()
    parser = argparse.ArgumentParser()
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--game-port", type=int, action="append", default=[])
    parser.add_argument("--xor-key", type=parse_xor_key, default=None)
    parser.add_argument(
        "--capture-kind",
        choices=("production", "diagnostic"),
        default="diagnostic",
    )
    parser.add_argument("--report-era", required=True)
    parser.add_argument("--capture-label", default="mumu-tcpdump")
    args = parser.parse_args()
    game_ports = set(args.game_port or DEFAULT_GAME_PORTS)
    summary = extract_pcap(
        args.pcap,
        args.capture_dir,
        game_ports=game_ports,
        xor_key=args.xor_key,
        capture_kind=args.capture_kind,
        report_era=args.report_era,
        label=args.capture_label,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
