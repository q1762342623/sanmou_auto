"""战报自动收录 · 本轮交战坐标汇总：只解码「本次 capture」，输出最高交战频率点。

用法：
    python scripts/coordinate_stats.py --capture-dir captures/<captureId>
    python scripts/coordinate_stats.py                 # 默认取最新的 production capture

数据来源：captures/<id>/blobs/*.bin 解码出的战报记录字段 `coord.x / coord.y`。
「本轮」= 本次从进入战报到拉到底所抓到的全部战报（capture 内全部 BLOB），
**与战报之前是否已提交过无关**（不看上传状态、不查历史 capture）。
去重只在本轮内做：同一场战报在本轮多个分页响应里重复出现时只算一次。
每轮/每次运行只输出本轮自己的热点，不做任何历史累计。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import common

LATEST_PATH = common.WORK_DIR / "latest_hotspot.json"
LATEST_TEXT_PATH = common.WORK_DIR / "latest_hotspot.txt"


def _capture_metadata(capture_dir: Path) -> dict:
    try:
        return json.loads((capture_dir / "capture_metadata.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _valid_coord(x, y) -> bool:
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return False
    return 0 < x < 2000 and 0 < y < 2000


def _battle_key(record: dict) -> tuple[str, list[int]] | None:
    """返回 (去重键, [x,y])；缺时间/坐标/双方玩家时返回 None。"""
    coord = record.get("coord") or {}
    x, y = coord.get("x"), coord.get("y")
    if not record.get("payload_time") or not _valid_coord(x, y):
        return None
    sides = record.get("sides") or []
    if len(sides) != 2:
        return None
    identities = []
    for side in sides:
        identity = side.get("identity") or {}
        player = str(identity.get("player") or "")
        if not player:
            return None
        identities.append(f"{player}|{identity.get('alliance') or ''}")
    # 同一场战报在别的响应里可能左右互换：排序后再做键
    left, right = sorted(identities)
    key = "|".join([str(record.get("payload_time")), str(int(x)), str(int(y)),
                    left, right])
    return key, [int(x), int(y)]


def capture_battles(capture_dir: str | Path, min_blob_size: int = 1000) -> dict[str, list[int]]:
    """解码一个 capture，返回 {去重键: [x,y]}。单个 BLOB 解析失败不影响其它。"""
    capture_dir = Path(capture_dir)
    engine = common.load_engine("decode_list_card_sproto_like")
    battles: dict[str, list[int]] = {}
    try:
        blob_entries = engine.iter_unique_blobs(capture_dir, int(min_blob_size))
    except (OSError, ValueError):
        return battles
    for sha12, path in blob_entries:
        try:
            decoded = engine.decode_blob(path, sha12, "", {})
        except Exception:  # noqa: BLE001
            continue
        for record in decoded.get("records") or []:
            item = _battle_key(record)
            if item is not None:
                battles.setdefault(item[0], item[1])
    return battles


def _hotspot(battles: dict[str, list[int]], top_n: int) -> tuple[dict | None, list[dict], int]:
    counts = Counter((int(x), int(y)) for x, y in battles.values())
    total = sum(counts.values())
    top = [
        {"x": x, "y": y, "count": count,
         "share": round(count / total, 4) if total else 0.0}
        for (x, y), count in counts.most_common(max(1, int(top_n)))
    ]
    return (dict(top[0]) if top else None), top, total


def _extract_counts(capture_dir: Path) -> dict:
    try:
        payload = json.loads((capture_dir / "mumu_packet_extract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "blobs": int(payload.get("uniqueBlobs") or 0),
        "records": int(payload.get("completeRecords") or 0),
    }


def summarize_capture(config: dict, capture_dir: str | Path,
                      top_n: int = 10) -> dict:
    """只汇总指定 capture（本轮），输出最高交战频率点。

    `records` = 本轮 capture 解出的完整战报记录数（含同一场战报被分页重复带回的次数）；
    `battles` = 本轮内去重后的战报场次数；`duplicates` = 两者之差。
    """
    coordinates_config = (config.get("coordinates") or {})
    min_blob_size = int(coordinates_config.get("minBlobSize", 1000))
    capture_dir = Path(capture_dir)
    metadata = _capture_metadata(capture_dir)
    extract_counts = _extract_counts(capture_dir)
    battles = capture_battles(capture_dir, min_blob_size)
    hottest, top, total = _hotspot(battles, top_n)
    records = int(extract_counts.get("records") or 0)
    result = {
        "ok": True,
        "scope": "round",
        "captureId": capture_dir.name,
        "captureKind": metadata.get("captureKind"),
        "reportEra": metadata.get("reportEra"),
        "source": "capture-blobs",
        "records": records,
        "battles": total,
        "duplicates": max(0, records - total),
        "hottest": hottest,
        "top": top,
        "generatedAt": common.timestamp(),
    }
    LATEST_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    LATEST_TEXT_PATH.write_text(
        (f"{hottest['x']},{hottest['y']},{hottest['count']},{hottest['share']}\n"
         if hottest else "none\n"),
        encoding="utf-8")
    return result


def newest_production_capture() -> Path | None:
    """CLI 未指定 capture 时，取最新一次 production capture。"""
    candidates: list[Path] = []
    if common.CAPTURE_ROOT.is_dir():
        for capture_dir in common.CAPTURE_ROOT.iterdir():
            if not capture_dir.is_dir():
                continue
            if not (capture_dir / "binary_string_blobs.jsonl").is_file():
                continue
            if str(_capture_metadata(capture_dir).get("captureKind") or "") != "production":
                continue
            candidates.append(capture_dir)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def human_line(result: dict) -> str:
    hottest = result.get("hottest") or {}
    if not hottest:
        return f"[交战热点] 本轮({result.get('captureId')}) 无有效坐标"
    return (f"[交战热点] 本轮({result.get('captureId')}) "
            f"记录={result.get('records')} 去重={result.get('battles')}场 "
            f"最高交战点=({hottest['x']},{hottest['y']}) "
            f"{hottest['count']}场 {hottest['share'] * 100:.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="")
    parser.add_argument("--capture-dir", default="")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    config = common.load_config(args.config or None)
    try:
        capture_dir = Path(args.capture_dir) if args.capture_dir else newest_production_capture()
        if capture_dir is None:
            raise RuntimeError("没有找到 production capture，请用 --capture-dir 指定")
        result = summarize_capture(config, capture_dir, top_n=args.top)
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "reason": type(error).__name__, "error": str(error)}
    print(human_line(result))
    common.json_print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
