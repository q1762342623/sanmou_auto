"""战报自动收录 · 环境自检（不点屏幕、不抓包）。

检查项：内置引擎/模板、Python 依赖、adb/设备/root、模拟器内 tcpdump、
上传配置与密钥、统计时代文件、服务器 /api/live 可用性。

用法：
    python scripts/doctor.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import common

ENGINE_FILES = (
    "decode_list_card_sproto_like.py",
    "extract_mumu_pcap_blobs.py",
    "tool_contract.py",
    "upload_capture_batches.py",
    "server_report_era.py",
    "report_era.py",
    "report_policy.py",
    "detect_expand_arrow.py",
    "inspect_mumu_ui.py",
)


def add(checks: list, name: str, ok: bool, detail="") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def run_checks(config: dict) -> dict:
    checks: list[dict] = []

    missing = [name for name in ENGINE_FILES
               if not (common.ENGINE_DIR / name).is_file()]
    add(checks, "engine-modules", not missing,
        f"缺少 {missing}" if missing else str(common.ENGINE_DIR))

    # 仓库不保存图片：模板默认从 engine/arrow_templates.py 内嵌数据解码；
    # 本地若放了 reference/ui_templates/*.png 则优先用文件（现场替换模板）。
    embedded = True
    try:
        from engine import arrow_templates
        embedded = all(arrow_templates.load_gray(name).size > 0
                       for name in ("expand_down.png", "expand_up.png"))
    except Exception:  # noqa: BLE001
        embedded = False
    files = [name for name in ("expand_down.png", "expand_up.png")
             if (common.TEMPLATES_DIR / name).is_file()]
    add(checks, "ui-templates", embedded,
        ("内嵌模板可用" + (f"；本地覆盖 {files}" if files else ""))
        if embedded else "缺少内嵌模板 engine/arrow_templates.py")

    python = common.which_python(config)
    modules = {"cv2": False, "numpy": False, "PIL": False, "dpkt": False}
    for name in modules:
        modules[name] = common.has_module(python, name)
    add(checks, "python-deps", all(modules.values()),
        f"{python} -> {modules}；缺 dpkt 时先 {python} -m pip install dpkt")

    era_path = common.SKILL_ROOT / "reference" / "current_report_era.json"
    era_detail = ""
    era_ok = False
    try:
        payload = json.loads(era_path.read_text(encoding="utf-8-sig"))
        era_ok = bool(payload.get("eras"))
        era_detail = str(era_path)
    except Exception as error:  # noqa: BLE001
        era_detail = f"{era_path}: {error}"
    add(checks, "report-era", era_ok, era_detail)

    upload_cfg = common.resolve_path(config["server"]["configPath"])
    upload_ok = upload_cfg.is_file()
    detail = str(upload_cfg)
    if upload_ok:
        try:
            payload = json.loads(upload_cfg.read_text(encoding="utf-8-sig"))
            base_url = str(payload.get("baseUrl") or "")
            secret_file = str(payload.get("secretFile") or "control/server_upload_secret.txt")
            secret_path = Path(secret_file)
            if not secret_path.is_absolute():
                secret_path = upload_cfg.parent / secret_path.name
            secret_ok = secret_path.is_file() and len(
                secret_path.read_text(encoding="utf-8-sig").strip()) >= 32
            upload_ok = bool(base_url) and secret_ok
            detail = f"baseUrl={base_url or '<空>'} secret={'ok' if secret_ok else '缺失/过短'}"
        except Exception as error:  # noqa: BLE001
            upload_ok = False
            detail = f"{upload_cfg}: {error}"
    add(checks, "server-upload-config", upload_ok, detail)

    adb_ok = Path(str(config.get("adbPath"))).is_file()
    add(checks, "adb-path", adb_ok, str(config.get("adbPath")))

    if adb_ok:
        adb = common.make_adb(config)
        try:
            adb.ensure_online()
            add(checks, "device-online", True, adb.serial)
            root_ok = adb.is_root()
            add(checks, "adbd-root", root_ok, "adb root 后可开启 tcpdump" if not root_ok else "uid=0")
            tcpdump = common.find_tcpdump(adb)
            add(checks, "device-tcpdump", bool(tcpdump), tcpdump or "设备内未找到 tcpdump")
            ports = _ports(adb, config)
            add(checks, "game-ports", bool(ports), str(ports))
        except Exception as error:  # noqa: BLE001
            add(checks, "device-online", False, str(error))
            for name in ("adbd-root", "device-tcpdump", "game-ports"):
                add(checks, name, False, "设备不可用，跳过")

    add(checks, "server-live", *_live(config))

    ok = all(item["ok"] for item in checks)
    return {"ok": ok, "checks": checks,
            "failed": [item["name"] for item in checks if not item["ok"]]}


def _ports(adb, config) -> list[int]:
    import capture  # 复用同一套端口发现逻辑

    return capture.discover_ports(adb, config)


def _live(config: dict) -> tuple[bool, str]:
    import upload

    try:
        payload = upload.live_status(upload.base_url(config))
    except Exception as error:  # noqa: BLE001
        return False, f"不可达：{error}"
    return True, (
        f"ready={payload.get('ready')} publishing={payload.get('publishing')} "
        f"caughtUp={payload.get('caughtUp')} ingestVersion={payload.get('ingestVersion')} "
        f"visibleIngestVersion={payload.get('visibleIngestVersion')} "
        f"reportEra={payload.get('reportEra')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="")
    args = parser.parse_args()
    config = common.load_config(args.config or None)
    try:
        result = run_checks(config)
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "reason": type(error).__name__, "error": str(error)}
    common.json_print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
