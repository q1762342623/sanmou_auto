"""战报自动收录 · 一条命令跑完一轮：采集 → 提取 → 上传 → 等可见 → 汇总。

一轮 = 打开「战报 → 同盟 → 交战」→ 逐卡展开 → 一直滑到列表真正到底
     （连续多屏画面不再变化）→ 停 tcpdump → pcap 提 BLOB → 增量上传 → 等 /api/live 可见。
     没滑到底的采集不算完成：production 会保留 capture 但拒绝上传。

用法：
    python scripts/round.py                       # 生产抓取（翻到列表底）+ 上传 + 等可见
    python scripts/round.py --kind diagnostic     # 只抓本地、不上传（同样翻到底）
    python scripts/round.py --max-seconds 1800
    python scripts/round.py --skip-upload
    # --max-pages 仅冒烟/校准；>0 会让本轮不完整而无法上传
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import common


def run_round(config: dict, *, kind: str, max_pages: int | None, max_seconds: int,
              expand: bool, skip_upload: bool, wait_visible: bool) -> dict:
    import capture
    import extract
    import upload

    started = time.time()
    summary: dict = {"ok": False, "kind": kind, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    try:
        captured = capture.run_capture(
            config, kind=kind, max_pages=max_pages, max_seconds=max_seconds, expand=expand,
        )
    except Exception as error:  # noqa: BLE001 - 未翻到底/设备异常都算本步失败
        summary.update({"failedStep": "capture", "reason": type(error).__name__,
                        "error": str(error)})
        return _finish(summary, started)
    summary["capture"] = captured
    summary["captureId"] = captured["captureId"]
    summary["captureComplete"] = bool(captured.get("complete"))
    if (kind == "production" and not summary["captureComplete"]
            and bool(config["capture"].get("requireListEnd", True))):
        summary.update({"failedStep": "capture", "reason": "incomplete-list",
                        "error": "列表未翻到底，本轮不算完成"})
        return _finish(summary, started)

    extracted = extract.extract_capture(config, captured["captureDir"])
    summary["extract"] = extracted
    if not extracted.get("ok"):
        summary["ok"] = False
        summary["failedStep"] = "extract"
        return _finish(summary, started)

    if skip_upload or kind != "production":
        summary["ok"] = True
        summary["skipped"] = "upload" if skip_upload else "diagnostic-kind"
        return _finish(summary, started)

    uploaded = upload.upload_captures(config, [captured["captureId"]])
    summary["upload"] = uploaded
    if not uploaded.get("ok"):
        summary["ok"] = False
        summary["failedStep"] = "upload"
        return _finish(summary, started)
    if uploaded.get("nothingNew"):
        # 列表翻到底了但服务器已有全部 BLOB：本轮成功，无需等可见。
        summary["ok"] = True
        summary["skipped"] = "no-new-blobs"
        return _finish(summary, started)

    if wait_visible:
        visibility = upload.wait_visible(config, uploaded)
        summary["visibility"] = visibility
        if not visibility.get("ok"):
            summary["ok"] = False
            summary["failedStep"] = "visibility"
            return _finish(summary, started)

    summary["ok"] = True
    return _finish(summary, started)


def _finish(summary: dict, started: float) -> dict:
    import extract  # noqa: F401  保证同目录导入一致

    summary["seconds"] = round(time.time() - started, 1)
    summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    rounds = common.ensure_dir(common.WORK_DIR / "rounds")
    path = rounds / f"round_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary["summaryPath"] = str(path)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="")
    parser.add_argument("--kind", choices=("production", "diagnostic"), default="production")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="仅调试用；>0 会提前停止，使本轮不完整而无法上传")
    parser.add_argument("--max-seconds", type=int, default=3600)
    parser.add_argument("--no-expand", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()
    config = common.load_config(args.config or None)
    try:
        result = run_round(
            config, kind=args.kind,
            max_pages=args.max_pages or None,
            max_seconds=args.max_seconds,
            expand=not args.no_expand,
            skip_upload=args.skip_upload,
            wait_visible=not args.no_wait,
        )
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "reason": type(error).__name__, "error": str(error)}
    common.json_print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
