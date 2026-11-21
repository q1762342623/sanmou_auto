"""战报自动收录 · pcap → 原始 BLOB（解码去重）+ 质量门槛。

调用内置引擎 scripts/engine/extract_mumu_pcap_blobs.py：
  - 合并 TCP 流、按两字节大端长度前缀切分传输帧、自动发现 XOR 密钥；
  - 解出 sproto-like 战报 blob，按内容 SHA-256 去重后落 captures/<captureId>/blobs/；
  - 输出 mumu_packet_extract.json（uniqueBlobs / rawRecords / completeRecords）。

用法：
    python scripts/extract.py --capture-dir captures/mumu_adb_production_20261121_193000_000
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import common


def extract_capture(config: dict, capture_dir: str | Path, *,
                    kind: str | None = None, report_era: str | None = None,
                    label: str | None = None) -> dict:
    capture_dir = Path(capture_dir)
    meta_path = capture_dir / "capture_metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    kind = kind or str(metadata.get("captureKind") or "diagnostic")
    report_era = report_era or str(metadata.get("reportEra") or "")
    label = label or str(metadata.get("label") or "sanmou-auto-war-report")
    ports = metadata.get("gamePorts") or config["ports"]["fallback"]

    engine = common.load_engine("extract_mumu_pcap_blobs")
    summary = engine.extract_pcap(
        capture_dir / "game_traffic.pcap",
        capture_dir,
        game_ports={int(port) for port in ports},
        xor_key=None,
        capture_kind=kind,
        report_era=report_era,
        label=label,
    )
    result = {
        "ok": True,
        "captureId": capture_dir.name,
        "captureDir": str(capture_dir),
        "captureKind": kind,
        "reportEra": report_era,
        "uniqueBlobs": int(summary["uniqueBlobs"]),
        "rawRecords": int(summary["rawRecords"]),
        "completeRecords": int(summary["completeRecords"]),
        "transportFrames": int(summary["transportFrames"]),
        "xorKeySource": summary.get("xorKeySource"),
        "gamePorts": summary.get("gamePorts"),
    }
    # 质量门槛：没有任何可解码战报说明这轮没抓到东西（端口错/没进列表/被弹窗挡住）
    if result["uniqueBlobs"] <= 0:
        result["ok"] = False
        result["reason"] = "no-blobs"
        result["hint"] = "检查 gamePorts、是否真的停在「交战」页、capture_dir 截图与 tcpdump.log"
    elif result["completeRecords"] <= 0:
        result["ok"] = False
        result["reason"] = "no-complete-records"
        result["hint"] = "抓到 BLOB 但没有完整战报：多为列表没加载完或抓包时间过短，延长 max-seconds 重试"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="")
    parser.add_argument("--capture-dir", required=True)
    parser.add_argument("--kind", choices=("production", "diagnostic"), default=None)
    parser.add_argument("--report-era", default=None)
    parser.add_argument("--label", default=None)
    args = parser.parse_args()
    config = common.load_config(args.config or None)
    try:
        result = extract_capture(config, args.capture_dir, kind=args.kind,
                                 report_era=args.report_era, label=args.label)
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "reason": type(error).__name__, "error": str(error)}
    common.json_print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
