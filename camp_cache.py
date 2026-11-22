"""据点列表持久化缓存：城池 / 民港 / 军港 / 同盟建筑 只查一次，失效才重扫。

实测背景（2026-11-22 竖屏长跑）：
- 「定位最近可调动据点」每轮若全量重扫同盟建筑 + 14 屏城池列表，单轮要 2~5 分钟；
- 同一片战场上可调动的据点几乎不变，重复扫描纯属浪费，
  真正需要重扫的时机只有：缓存里的候选全都选不中 / 没有「调动」按钮。

使用方式：
    from .camp_cache import load_camps, save_camps, invalidate, clear

    camps, meta = load_camps()                 # 读缓存（可能为空）
    if not camps:
        camps = fac.scan_all()                 # 只在没有缓存时扫一次
        save_camps(camps, battlefield=coord)
    ...
    if not fac.select(camp):                   # 目标不可用 → 从缓存里剔除
        invalidate(camp.coord)

缓存文件：`screenshots/flow/camp_cache.json`（相对项目根目录）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .alliance import Camp

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "screenshots" / "flow" / "camp_cache.json"
SCHEMA_VERSION = 1
# 缓存超过该秒数只做「提示」，不自动丢弃：据点归属/类型很少变化，
# 真正的失效由 invalidate() 在「选不中 / 没有调动按钮」时触发。
STALE_HINT_SECONDS = 6 * 3600


def _camp_to_dict(camp: Camp) -> dict:
    return {
        "name": camp.name,
        "coord": [int(camp.coord[0]), int(camp.coord[1])],
        "source": camp.source,
        "kind": camp.kind,
        "note": camp.note,
        "status": camp.status,
        "page": int(camp.page or 0),
        "distText": camp.dist_text,
    }


def _dict_to_camp(raw: dict) -> Camp | None:
    coord = raw.get("coord") or []
    if len(coord) != 2:
        return None
    try:
        coord_t = (int(coord[0]), int(coord[1]))
    except (TypeError, ValueError):
        return None
    return Camp(
        name=str(raw.get("name") or ""),
        coord=coord_t,
        point=(0, 0),
        dist_text=str(raw.get("distText") or ""),
        status=str(raw.get("status") or ""),
        source=str(raw.get("source") or "city"),
        kind=str(raw.get("kind") or ""),
        note=str(raw.get("note") or ""),
        page=int(raw.get("page") or 0),
    )


def load_payload() -> dict:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_camps() -> tuple[list[Camp], dict]:
    """返回 (camps, meta)；meta 里含 scannedAt / battlefield / ageSeconds。"""
    payload = load_payload()
    camps: list[Camp] = []
    for raw in payload.get("camps") or []:
        camp = _dict_to_camp(raw) if isinstance(raw, dict) else None
        if camp is not None:
            camps.append(camp)
    scanned_at = float(payload.get("scannedAt") or 0.0)
    meta = {
        "scannedAt": scanned_at,
        "scannedAtIso": payload.get("scannedAtIso"),
        "battlefield": payload.get("battlefield"),
        "ageSeconds": round(time.time() - scanned_at, 1) if scanned_at else None,
        "stale": bool(scanned_at) and (time.time() - scanned_at) > STALE_HINT_SECONDS,
        "path": str(CACHE_PATH),
    }
    return camps, meta


def save_camps(camps: list[Camp], battlefield: tuple[int, int] | None = None) -> Path:
    """整体覆盖写入（据点列表是「一份完整快照」，不做增量合并）。"""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "scannedAt": now,
        "scannedAtIso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "battlefield": list(battlefield) if battlefield else None,
        "count": len(camps),
        "camps": [_camp_to_dict(c) for c in camps],
    }
    temporary = CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(CACHE_PATH)
    return CACHE_PATH


def invalidate(coord: tuple[int, int] | list[int]) -> bool:
    """把某个不可用的据点从缓存里剔除（选不中 / 没有调动按钮时调用）。"""
    payload = load_payload()
    rows = payload.get("camps") or []
    target = [int(coord[0]), int(coord[1])]
    kept = [r for r in rows
            if not (isinstance(r, dict)
                    and [int(v) for v in (r.get("coord") or [])] == target)]
    if len(kept) == len(rows):
        return False
    payload["camps"] = kept
    payload["count"] = len(kept)
    payload["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    except OSError:
        return False
    return True


def _write_payload(payload: dict) -> bool:
    payload["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(CACHE_PATH)
        return True
    except OSError:
        return False


def get_last_ok() -> tuple[int, int] | None:
    """上次沙盘直跳成功的据点坐标（下一轮优先验证它，通常一次命中）。"""
    payload = load_payload()
    coord = payload.get("lastOk") or []
    if len(coord) != 2:
        return None
    try:
        return int(coord[0]), int(coord[1])
    except (TypeError, ValueError):
        return None


def set_last_ok(coord: tuple[int, int] | list[int]) -> None:
    payload = load_payload()
    payload["lastOk"] = [int(coord[0]), int(coord[1])]
    payload["lastOkAt"] = time.time()
    payload["lastOkAtIso"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.localtime())
    _write_payload(payload)


def clear() -> bool:
    try:
        CACHE_PATH.unlink()
        return True
    except OSError:
        return False


def sort_by_distance(camps: list[Camp], target: tuple[int, int]) -> list[Camp]:
    return sorted(camps, key=lambda c: c.dist(target))


__all__ = ["CACHE_PATH", "SCHEMA_VERSION", "STALE_HINT_SECONDS",
           "load_payload", "load_camps", "save_camps", "invalidate", "clear",
           "sort_by_distance", "get_last_ok", "set_last_ok"]
