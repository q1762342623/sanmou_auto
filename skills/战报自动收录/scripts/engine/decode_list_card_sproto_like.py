#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Decode battle-report list-card summary blobs as sproto-like objects.

This is an offline, read-only reverse-engineering helper for captures produced
by capture_list_card_rpc_timeline.py. The list-card payloads observed on
2026-06-27 are not BattleLog blobs; they are unpacked sproto-style objects:

  uint16 field_count
  uint16 field_headers[field_count]
  length-prefixed data fields

Non-zero even headers encode small integers as header / 2 - 1. Zero headers
consume the next length-prefixed data segment. Odd headers skip tag numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CN_TZ = timezone(timedelta(hours=8))
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
NOISE_RE = re.compile(r"[\ufffd]")


FORMATION_BY_TAG7: dict[int, str] = {
    # Validated against list-card/detail screenshot pairs.
    101: "钩行阵",
    201: "箕形阵",
    301: "雁形阵",
    401: "鱼鳞阵",
    501: "锥形阵",
    601: "方圆阵",
    701: "钩行阵",
    801: "偃月阵",
}

UNIT_TYPE_BY_TAG14: dict[int, str] = {
    # Validated across existing captures: tag14 equals tag32 for every observed
    # hero and remains fixed per hero ID. Known lineups map to these four arms.
    1: "盾",
    2: "弓",
    3: "枪",
    4: "骑",
}


KNOWN_LABELS: dict[tuple[str, str], dict[str, Any]] = {
    ("9fec0299f002", "2026/06/27 17:29:40"): {
        "group": "chain2",
        "round": 2,
        "direction": "attack",
        "result": "loss",
        "own_troops": "0/20212",
        "opponent_troops": "21856/33000",
        "merit": 5486,
    },
    ("a147ae50cb7a", "2026/06/27 17:28:01"): {
        "group": "chain2",
        "round": 1,
        "direction": "attack",
        "result": "win",
        "own_troops": "4697/33000",
        "opponent_troops": "0/27955",
        "merit": 5486,
    },
    ("e9d1f3846741", "2026/06/27 17:14:17"): {
        "group": "chain4",
        "round": 3,
        "direction": "defense",
        "result": "win",
        "own_troops": "8494/9372",
        "opponent_troops": "0/13803",
        "merit": 8603,
    },
    ("e9d1f3846741", "2026/06/27 17:14:04"): {
        "group": "chain4",
        "round": 2,
        "direction": "defense",
        "result": "draw",
        "own_troops": "9372/30857",
        "opponent_troops": "13803/23725",
        "merit": 8603,
    },
    ("e9d1f3846741", "2026/06/27 17:13:46"): {
        "group": "chain4",
        "round": 1,
        "direction": "defense",
        "result": "win",
        "own_troops": "29252/33000",
        "opponent_troops": "0/32700",
        "merit": 8603,
    },
}

DEFAULT_HERO_CATALOG = Path(__file__).resolve().parents[1] / "reference" / "hero_catalog.json"
HERO_CATALOG_SCHEMA_VERSION = 1


@dataclass
class Field:
    tag: int
    kind: str
    value: int | None = None
    start: int | None = None
    size: int | None = None


class ParseError(ValueError):
    pass


def configure_output_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_hero_catalog(path: Path) -> tuple[dict[int, str], dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Hero catalog not found: {path}. Run scripts\\sync_hero_catalog.ps1."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Hero catalog is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Hero catalog root must be an object: {path}")
    schema_version = int(payload.get("schemaVersion") or 0)
    if schema_version != HERO_CATALOG_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported hero catalog schema {schema_version}; "
            f"expected {HERO_CATALOG_SCHEMA_VERSION}"
        )
    raw_heroes = payload.get("heroes")
    if not isinstance(raw_heroes, dict):
        raise ValueError(f"Hero catalog heroes must be an object: {path}")
    try:
        names = {int(hero_id): str(name).strip() for hero_id, name in raw_heroes.items()}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Hero catalog contains a non-integer hero ID: {path}") from exc
    if len(names) != len(raw_heroes):
        raise ValueError(f"Hero catalog contains duplicate numeric hero IDs: {path}")
    if not names or any(hero_id <= 0 or not name.strip() for hero_id, name in names.items()):
        raise ValueError(f"Hero catalog contains invalid rows: {path}")
    return names, {
        "schemaVersion": schema_version,
        "catalogSha256": file_sha256(path),
        "generatedAt": str(payload.get("generatedAt") or ""),
        "heroCount": len(names),
        "format": "json",
    }


def load_hero_names(path: Path) -> dict[int, str]:
    return load_hero_catalog(path)[0]


def resolve_blob_path(row: dict[str, Any], capture_dir: Path) -> Path | None:
    raw = str(row.get("path") or "")
    if not raw:
        return None
    path = Path(raw)
    if path.exists():
        return path
    if not path.is_absolute() and (capture_dir / path).exists():
        return capture_dir / path
    candidate = capture_dir / "blobs" / path.name
    return candidate if candidate.exists() else None


def read_u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise ParseError(f"u16 out of range: {offset}")
    return struct.unpack_from("<H", data, offset)[0]


def read_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ParseError(f"u32 out of range: {offset}")
    return struct.unpack_from("<I", data, offset)[0]


def read_u64(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        raise ParseError(f"u64 out of range: {offset}")
    return struct.unpack_from("<Q", data, offset)[0]


def parse_object(data: bytes, base: int = 0, size: int | None = None, *, max_fields: int = 160) -> list[Field]:
    if size is None:
        size = len(data) - base
    end = base + size
    if base < 0 or end > len(data) or base + 2 > end:
        raise ParseError("object bounds out of range")

    field_count = read_u16(data, base)
    if field_count > max_fields:
        raise ParseError(f"too many fields: {field_count}")

    header_start = base + 2
    data_pos = header_start + 2 * field_count
    if data_pos > end:
        raise ParseError("object header exceeds bounds")

    headers = [read_u16(data, header_start + 2 * index) for index in range(field_count)]
    tag = -1
    fields: list[Field] = []
    for header in headers:
        if header & 1:
            tag += (header + 1) // 2
            continue
        tag += 1
        if header == 0:
            if data_pos + 4 > end:
                raise ParseError("data length exceeds object bounds")
            field_size = read_u32(data, data_pos)
            start = data_pos + 4
            stop = start + field_size
            if stop > end:
                raise ParseError("data field exceeds object bounds")
            fields.append(Field(tag=tag, kind="data", start=start, size=field_size))
            data_pos = stop
        else:
            fields.append(Field(tag=tag, kind="small", value=header // 2 - 1))

    if data_pos != end:
        raise ParseError(f"object has trailing bytes: {end - data_pos}")
    return fields


def try_parse_object(data: bytes, base: int, size: int) -> list[Field] | None:
    try:
        return parse_object(data, base, size)
    except ParseError:
        return None


def parse_array(data: bytes, start: int, size: int) -> list[tuple[int, int, list[Field]]]:
    pos = start
    end = start + size
    items: list[tuple[int, int, list[Field]]] = []
    while pos + 4 <= end:
        item_size = read_u32(data, pos)
        item_start = pos + 4
        item_end = item_start + item_size
        if item_size <= 0 or item_end > end:
            raise ParseError("bad array item bounds")
        items.append((item_start, item_size, parse_object(data, item_start, item_size)))
        pos = item_end
    if pos != end:
        raise ParseError("array has trailing bytes")
    return items


def try_parse_array(data: bytes, start: int, size: int) -> list[tuple[int, int, list[Field]]] | None:
    try:
        return parse_array(data, start, size)
    except ParseError:
        return None


def payload_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(CN_TZ).strftime("%Y/%m/%d %H:%M:%S")


def clean_string(text: str) -> str:
    return "".join(ch for ch in text if ord(ch) >= 0x20).strip()


def decode_utf8(data: bytes, start: int, size: int) -> str | None:
    if not (0 <= start <= len(data) and 0 <= size and start + size <= len(data)):
        return None
    try:
        text = clean_string(data[start : start + size].decode("utf-8"))
    except UnicodeDecodeError:
        return None
    if not text or NOISE_RE.search(text):
        return None
    return text


def value_summary(data: bytes, field: Field) -> dict[str, Any]:
    if field.kind == "small":
        return {"kind": "small", "value": field.value}
    assert field.start is not None and field.size is not None
    start = field.start
    size = field.size
    out: dict[str, Any] = {"kind": "data", "start": start, "size": size}
    if size == 0:
        out["empty"] = True
    if size == 4:
        out["u32"] = read_u32(data, start)
    if size == 8:
        raw = read_u64(data, start)
        out["u64"] = raw
        dbl = struct.unpack_from("<d", data, start)[0]
        if math.isfinite(dbl):
            out["double"] = dbl
    text = decode_utf8(data, start, size)
    if text is not None and CHINESE_RE.search(text):
        out["utf8"] = text
    sub = try_parse_object(data, start, size)
    if sub is not None:
        out["object_tags"] = [f.tag for f in sub]
    arr = try_parse_array(data, start, size)
    if arr is not None:
        out["array_count"] = len(arr)
        out["array_item_sizes"] = [item_size for _, item_size, _ in arr[:20]]
    return out


def field_int_value(data: bytes, field: Field) -> int | None:
    if field.kind == "small":
        return field.value
    if field.kind == "data" and field.start is not None and field.size == 4:
        return read_u32(data, field.start)
    return None


def fields_to_compact(data: bytes, fields: list[Field]) -> list[dict[str, Any]]:
    return [{"tag": field.tag, **value_summary(data, field)} for field in fields]


def field_by_tag(fields: list[Field], tag: int) -> Field | None:
    for field in fields:
        if field.tag == tag:
            return field
    return None


def read_data_u32(data: bytes, fields: list[Field], tag: int) -> int | None:
    field = field_by_tag(fields, tag)
    if field is None or field.kind != "data" or field.start is None or field.size != 4:
        return None
    return read_u32(data, field.start)


def read_data_double(data: bytes, fields: list[Field], tag: int) -> float | None:
    field = field_by_tag(fields, tag)
    if field is None or field.kind != "data" or field.start is None or field.size != 8:
        return None
    return struct.unpack_from("<d", data, field.start)[0]


def decode_coord(data: bytes, fields: list[Field]) -> dict[str, Any] | None:
    field = field_by_tag(fields, 4)
    if field is None or field.kind != "data" or field.start is None or field.size != 9:
        return None
    start = field.start
    # The first byte belongs to the sproto field payload; the visible x/y pair
    # starts one byte later in all verified captures.
    if start + 9 > len(data):
        return None
    x = read_u32(data, start + 1)
    y = read_u32(data, start + 5)
    return {"x": x, "y": y, "payload_start": start, "coord_offset": start + 1}


def decode_name_object(data: bytes, fields: list[Field]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for field in fields:
        if field.kind != "data" or field.start is None or field.size is None:
            continue
        text = decode_utf8(data, field.start, field.size)
        if text is not None and CHINESE_RE.search(text):
            decoded[f"tag{field.tag}"] = text
    if "tag1" in decoded:
        decoded["player"] = decoded["tag1"]
    if "tag3" in decoded:
        decoded["alliance"] = decoded["tag3"]
    return decoded


def decode_team(data: bytes, field: Field, hero_names: dict[int, str]) -> dict[str, Any]:
    if field.kind != "data" or field.start is None or field.size is None:
        return {}
    team_fields = parse_object(data, field.start, field.size)
    out: dict[str, Any] = {
        "start": field.start,
        "size": field.size,
        "fields": fields_to_compact(data, team_fields),
    }

    identity_field = field_by_tag(team_fields, 2)
    if identity_field is not None and identity_field.kind == "data" and identity_field.start is not None and identity_field.size is not None:
        identity_fields = try_parse_object(data, identity_field.start, identity_field.size)
        if identity_fields is not None:
            out["identity"] = decode_name_object(data, identity_fields)

    out["selected"] = {}
    for tag in (1, 4, 7, 9, 10, 11, 12, 16, 19, 23, 27, 28, 29):
        current = field_by_tag(team_fields, tag)
        if current is not None:
            out["selected"][str(tag)] = value_summary(data, current)
    formation_field = field_by_tag(team_fields, 7)
    if formation_field is not None:
        formation_tag7 = field_int_value(data, formation_field)
        if formation_tag7 is not None:
            out["formation_tag7"] = formation_tag7
            formation_name = FORMATION_BY_TAG7.get(formation_tag7)
            if formation_name is not None:
                out["formation_name"] = formation_name

    heroes_field = field_by_tag(team_fields, 3)
    if heroes_field is not None and heroes_field.kind == "data" and heroes_field.start is not None and heroes_field.size is not None:
        heroes_array = try_parse_array(data, heroes_field.start, heroes_field.size)
        if heroes_array is not None:
            heroes = []
            troop_current = 0
            troop_max = 0
            for _, _, hero_fields in heroes_array:
                hero = {}
                for tag in (0, 1, 2, 3, 4, 5, 9, 10, 14, 16, 17, 20, 24, 26, 32):
                    hero_field = field_by_tag(hero_fields, tag)
                    if hero_field is not None:
                        value = field_int_value(data, hero_field)
                        if value is not None:
                            hero[str(tag)] = value
                troop_current += int(hero.get("1") or 0)
                troop_max += int(hero.get("3") or 0)
                hero_id = int(hero.get("0") or 0)
                if hero_id:
                    name = hero_names.get(hero_id)
                    hero["name"] = name or str(hero_id)
                    if name is None:
                        hero["unknown_hero_id"] = hero_id
                if "10" in hero:
                    # Validated against list-card screenshots: per-hero tag10 is the displayed red/star level.
                    hero["red_level"] = int(hero["10"])
                if "20" in hero:
                    # Validated against user-provided known grades for Cao Cao/Dian Wei/Bian Furen.
                    hero["grade_level"] = int(hero["20"])
                if "5" in hero:
                    # List-card hero tag5 is the displayed hero level.
                    hero["level"] = int(hero["5"])
                if "14" in hero:
                    unit_type_tag14 = int(hero["14"])
                    hero["unit_type_tag14"] = unit_type_tag14
                    unit_type_name = UNIT_TYPE_BY_TAG14.get(unit_type_tag14)
                    if unit_type_name is not None:
                        hero["unit_type_name"] = unit_type_name
                heroes.append(hero)
            out["heroes"] = heroes
            out["troops"] = {"current": troop_current, "max": troop_max, "fraction": f"{troop_current}/{troop_max}"}
    return out


def decode_wrapper_summary(data: bytes, wrapper_fields: list[Field]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    w1 = field_by_tag(wrapper_fields, 1)
    if w1 is not None:
        out["folded_extra_count"] = field_int_value(data, w1)
        if isinstance(out["folded_extra_count"], int):
            out["chain_count_guess"] = int(out["folded_extra_count"]) + 1 if int(out["folded_extra_count"]) > 0 else 1
    w2 = field_by_tag(wrapper_fields, 2)
    if w2 is not None:
        out["tag2"] = field_int_value(data, w2)
    combat_field = field_by_tag(wrapper_fields, 3)
    if combat_field is not None and combat_field.kind == "data" and combat_field.start is not None and combat_field.size is not None:
        text = decode_utf8(data, combat_field.start, combat_field.size)
        if text:
            out["combat_id"] = text

    summary_field = field_by_tag(wrapper_fields, 4)
    if summary_field is None or summary_field.kind != "data" or summary_field.start is None or summary_field.size is None:
        return out
    outer = try_parse_object(data, summary_field.start, summary_field.size)
    if outer is None:
        return out
    inner_field = field_by_tag(outer, 1)
    if inner_field is None or inner_field.kind != "data" or inner_field.start is None or inner_field.size is None:
        return out
    inner = try_parse_object(data, inner_field.start, inner_field.size)
    if inner is None:
        return out
    values: dict[str, Any] = {}
    for field in inner:
        value = field_int_value(data, field)
        if value is not None:
            values[str(field.tag)] = value
    out["summary_fields"] = values
    if "0" in values:
        out["merit"] = values["0"]
    return out


def unwrap_record(data: bytes, fields: list[Field]) -> tuple[list[Field], dict[str, Any]]:
    if field_by_tag(fields, 6) is not None and field_by_tag(fields, 2) is not None:
        return fields, {}
    inner = field_by_tag(fields, 0)
    if inner is None or inner.kind != "data" or inner.start is None or inner.size is None:
        return fields, {}
    inner_fields = try_parse_object(data, inner.start, inner.size)
    if inner_fields is None or field_by_tag(inner_fields, 6) is None:
        return fields, {}
    wrapper = {"wrapper_fields": fields_to_compact(data, fields)}
    return inner_fields, wrapper


def decode_record(
    data: bytes,
    sha12: str,
    index: int,
    base: int,
    size: int,
    fields: list[Field],
    hero_names: dict[int, str],
) -> dict[str, Any]:
    original_fields = fields
    fields, wrapper = unwrap_record(data, fields)
    ts = read_data_u32(data, fields, 6)
    time_text = payload_time(ts) if ts is not None else ""
    coord = decode_coord(data, fields)
    rec: dict[str, Any] = {
        "index": index,
        "start": base,
        "size": size,
        "payload_time": time_text,
        "timestamp": ts,
        "coord": coord,
        "record_fields": fields_to_compact(data, fields),
        "known_label": KNOWN_LABELS.get((sha12, time_text), {}),
    }
    rec.update(wrapper)
    if wrapper:
        rec["wrapper_summary"] = decode_wrapper_summary(data, original_fields)

    combat = read_data_double(data, fields, 0)
    ui_time_double = read_data_double(data, fields, 17)
    if combat is not None:
        rec["tag0_double"] = combat
    if ui_time_double is not None:
        rec["tag17_double"] = ui_time_double

    teams_field = field_by_tag(fields, 2)
    if teams_field is not None and teams_field.kind == "data" and teams_field.start is not None and teams_field.size is not None:
        teams_obj = parse_object(data, teams_field.start, teams_field.size)
        rec["teams_object_fields"] = fields_to_compact(data, teams_obj)
        sides = []
        for side_tag in (1, 2):
            side_field = field_by_tag(teams_obj, side_tag)
            if side_field is not None:
                side = decode_team(data, side_field, hero_names)
                side["side_tag"] = side_tag
                sides.append(side)
        rec["sides"] = sides
    return rec


def enrich_record_result(record: dict[str, Any], own_alliance: str) -> None:
    sides = record.get("sides") or []
    if len(sides) < 2:
        return
    direction = direction_from_identity(sides, own_alliance)
    record["direction"] = direction
    if direction == "attack":
        own = sides[0]
        opponent = sides[1]
    elif direction == "defense":
        own = sides[1]
        opponent = sides[0]
    else:
        return
    own_troops = own.get("troops") or {}
    opponent_troops = opponent.get("troops") or {}
    own_current = int(own_troops.get("current") or 0)
    opponent_current = int(opponent_troops.get("current") or 0)
    if own_current > 0 and opponent_current == 0:
        result = "win"
    elif own_current == 0 and opponent_current > 0:
        result = "loss"
    elif own_current > 0 and opponent_current > 0:
        result = "draw"
    else:
        result = "unknown"
    record["own_troops"] = own_troops
    record["opponent_troops"] = opponent_troops
    record["result_guess"] = result


def decode_blob(path: Path, sha12: str, own_alliance: str, hero_names: dict[int, str]) -> dict[str, Any]:
    data = path.read_bytes()
    top_fields = parse_object(data)
    top = fields_to_compact(data, top_fields)
    first = field_by_tag(top_fields, 0)
    records = []
    if first is not None and first.kind == "data" and first.start is not None and first.size is not None:
        items = parse_array(data, first.start, first.size)
        for index, (base, size, fields) in enumerate(items, 1):
            record = decode_record(data, sha12, index, base, size, fields, hero_names)
            enrich_record_result(record, own_alliance)
            records.append(record)
    return {"sha": sha12, "path": str(path), "size": len(data), "top_fields": top, "record_count": len(records), "records": records}


def iter_unique_blob_entries(capture_dir: Path, min_blob_size: int) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for row in read_jsonl(capture_dir / "binary_string_blobs.jsonl"):
        size = int(row.get("size") or 0)
        sha = str(row.get("sha256") or "")
        if size < min_blob_size or not sha or sha in seen:
            continue
        path = resolve_blob_path(row, capture_dir)
        if path is None:
            continue
        seen.add(sha)
        out.append((sha, path))
    return out


def iter_unique_blobs(capture_dir: Path, min_blob_size: int) -> list[tuple[str, Path]]:
    return [(sha[:12], path) for sha, path in iter_unique_blob_entries(capture_dir, min_blob_size)]


def direction_from_identity(sides: list[dict[str, Any]], own_alliance: str) -> str:
    if len(sides) < 2:
        return ""
    left_alliance = sides[0].get("identity", {}).get("alliance", "")
    right_alliance = sides[1].get("identity", {}).get("alliance", "")
    if left_alliance == own_alliance:
        return "attack"
    if right_alliance == own_alliance:
        return "defense"
    return ""


def lineup_text(side: dict[str, Any]) -> str:
    heroes = side.get("heroes") or []
    names = []
    for hero in heroes:
        name = str(hero.get("name") or hero.get("0") or "?")
        red = hero.get("red_level")
        red_text = f"{red}红" if red is not None else ""
        grade = hero.get("grade_level")
        grade_text = f"{grade}品" if grade is not None else ""
        current = hero.get("1")
        maximum = hero.get("3")
        if current is not None and maximum is not None:
            names.append(f"{name}{red_text}{grade_text}({current}/{maximum})")
        else:
            names.append(f"{name}{red_text}{grade_text}")
    return " / ".join(names)


def formation_text(side: dict[str, Any]) -> str:
    tag7 = side.get("formation_tag7")
    name = side.get("formation_name")
    if name is not None and tag7 is not None:
        return f"{name}(tag7={tag7})"
    if tag7 is not None:
        return f"未知阵型(tag7={tag7})"
    return "?"


RESULT_CN = {"win": "胜", "loss": "败", "draw": "平", "unknown": "未知"}
DIRECTION_CN = {"attack": "进攻", "defense": "防守", "": ""}


def print_table(blobs: list[dict[str, Any]], own_alliance: str, *, compact: bool = False) -> None:
    for blob in blobs:
        if not blob["records"]:
            continue
        print(f"\nblob {blob['sha']} size={blob['size']} records={blob['record_count']} path={Path(blob['path']).name}")
        if not compact:
            print(f"  top={json.dumps(blob['top_fields'], ensure_ascii=False)}")
        for rec in blob["records"]:
            sides = rec.get("sides", [])
            left = sides[0] if len(sides) >= 1 else {}
            right = sides[1] if len(sides) >= 2 else {}
            left_id = left.get("identity", {})
            right_id = right.get("identity", {})
            coord = rec.get("coord") or {}
            label = rec.get("known_label") or {}
            selected = {
                "record_tag11": next((f.get("value") for f in rec["record_fields"] if f["tag"] == 11), None),
                "record_tag19": next((f.get("value") for f in rec["record_fields"] if f["tag"] == 19), None),
                "left": left.get("selected", {}),
                "right": right.get("selected", {}),
            }
            own_troops = rec.get("own_troops", {}).get("fraction", "?")
            opponent_troops = rec.get("opponent_troops", {}).get("fraction", "?")
            merit = (rec.get("wrapper_summary") or {}).get("merit")
            result = rec.get("result_guess", "")
            print(
                f"  {rec['index']:02d}. {direction_from_identity(sides, own_alliance):7s} {result:7s} "
                f"{left_id.get('player', '?')}({left_id.get('alliance', '?')}) -> "
                f"{right_id.get('player', '?')}({right_id.get('alliance', '?')}) "
                f"coord=({coord.get('x')},{coord.get('y')}) time={rec.get('payload_time')} "
                f"own={own_troops} opp={opponent_troops} merit={merit if merit is not None else '?'}"
            )
            if compact:
                summary = rec.get("wrapper_summary") or {}
                chain = summary.get("chain_count_guess")
                chain_text = f" 连战{chain}" if chain and int(chain) > 1 else ""
                own_side = left if rec.get("direction") == "attack" else right if rec.get("direction") == "defense" else {}
                opponent_side = right if rec.get("direction") == "attack" else left if rec.get("direction") == "defense" else {}
                print(
                    f"      卡片: {DIRECTION_CN.get(rec.get('direction', ''), rec.get('direction', ''))}"
                    f"{RESULT_CN.get(result, result)}{chain_text} "
                    f"己方{own_troops} 对方{opponent_troops}"
                    f"{' 武勋' + str(merit) if merit is not None else ''}"
                )
                print(f"      阵型: 己方{formation_text(own_side)} 对方{formation_text(opponent_side)}")
                print(f"      己方阵容: {lineup_text(own_side)}")
                print(f"      对方阵容: {lineup_text(opponent_side)}")
                continue
            if rec.get("wrapper_fields"):
                print(f"      wrapper_summary={json.dumps(rec.get('wrapper_summary') or {}, ensure_ascii=False)}")
            if label:
                print(f"      label={json.dumps(label, ensure_ascii=False)}")
            print(f"      selected={json.dumps(selected, ensure_ascii=False)}")


def main() -> int:
    configure_output_encoding()
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("--sha", action="append", default=[], help="Only decode blobs with this sha prefix.")
    parser.add_argument("--min-blob-size", type=int, default=1000)
    parser.add_argument("--own-alliance", required=True)
    parser.add_argument("--hero-catalog", type=Path, default=DEFAULT_HERO_CATALOG)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--compact", action="store_true", help="Print only decoded card facts.")
    args = parser.parse_args()

    wanted = [item.lower() for item in args.sha]
    hero_names = load_hero_names(args.hero_catalog)
    blobs = []
    for sha12, path in iter_unique_blobs(args.capture_dir, args.min_blob_size):
        if wanted and not any(sha12.lower().startswith(prefix) for prefix in wanted):
            continue
        try:
            decoded = decode_blob(path, sha12, args.own_alliance, hero_names)
        except ParseError:
            continue
        if decoded["record_count"]:
            blobs.append(decoded)

    result = {"capture_dir": str(args.capture_dir), "blob_count": len(blobs), "blobs": blobs}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_table(blobs, args.own_alliance, compact=args.compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
