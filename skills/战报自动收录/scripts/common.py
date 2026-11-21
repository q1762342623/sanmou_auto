"""战报自动收录 · 公共层：配置、adb 封装、截图、UI 目标换算、引擎模块加载。

坐标系约定：
- 配置里的 `targets` 都是**屏幕比例**（0~1），实际像素 = round(比例 × 当前截图宽/高)。
- 本项目固定 1600x900 横屏；比例默认值来自 1600x900 实测，portrait 设备可按
  reference/ui-coordinates.md 的音量表替换。

本文件不依赖 sanmou_auto 包，可被 skill 内任意脚本单独 import。
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _configure_stdout() -> None:
    """Windows 控制台默认 GBK，中文 JSON 会乱码；统一按 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


_configure_stdout()

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parents[1]      # 三谋自动拉线综合功能体
SCRIPTS_DIR = SKILL_ROOT / "scripts"
ENGINE_DIR = SCRIPTS_DIR / "engine"
TEMPLATES_DIR = SKILL_ROOT / "reference" / "ui_templates"
WORK_DIR = SKILL_ROOT / "work"
CAPTURE_ROOT = SKILL_ROOT / "captures"
CONTROL_DIR = SKILL_ROOT / "control"

# 战报页页签（x 比例区间，y 由 targets.tabBand 决定）；顺序与游戏内一致
TAB_WINDOWS = (
    ("全部", 0.020, 0.190),
    ("开拓", 0.200, 0.310),
    ("交战", 0.320, 0.430),
    ("收藏", 0.440, 0.580),
    ("个人", 0.620, 0.740),
    ("同盟", 0.740, 0.860),
)
TAB_SELECTED_GOLD = 0.05   # 选中页签金色边框比例下限（实测 0.15~0.18）

DEFAULT_CONFIG: dict = {
    "adbPath": r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
    "serial": "127.0.0.1:16416",
    "package": "com.bilibili.nslg",
    "python": "",
    "screen": {"width": 1600, "height": 900},
    # 1600x900 横屏实测比例（来自上游 MuMu 采集工程 landscape profile）
    "targets": {
        "reportEntry": [0.215, 0.814],      # 主界面/城内左下「战报」入口
        "allianceTab": [0.769, 0.118],      # 战报页「同盟」页签
        "combatTab": [0.3375, 0.118],       # 战报页「交战」页签（实测中心 540,106）
        "pageBack": [0.056, 0.028],         # 全屏页左上返回箭头（90,25）
        "tabBand": [0.0, 0.075, 1.0, 0.165],  # 页签视觉带（x1,y1,x2,y2 比例）
        "swipe": {"x": 0.36, "y1": 0.72, "y2": 0.39, "durationMs": 700},
    },
    "expand": {
        "roiX1": 0.900,
        "roiX2": 1.000,          # 金色竖条在 0.9625~0.9906，ROI 必须到屏幕右边缘
        "roiY1": 0.080,
        "roiY2": 0.960,
        "templateScale": 0.9,    # 1600x900 实测（0.8/1.0 都检不到）
        "downThreshold": 0.85,   # 实测 down≈0.965 / up≈0.54；半截卡（0.46）必须排除
        "minMargin": 0.10,
        "minWidth": 14,
        "maxWidth": 90,
        "minHeight": 150,        # 完整金色竖条约 216px，底部半截卡约 121px
        "maxPerPage": 6,
        "settleMs": 420,
    },
    "ports": {"fallback": [20020, 20050, 20060, 20090], "min": 10000, "max": 65535},
    "capture": {
        "pageDelayMs": 900,
        "swipeDelayMs": 700,
        "maxPages": 60,
        "stalledPagesToStop": 2,
        "visualDiffMean": 3.0,
        "changedRatio": 0.03,
        "navigateRetries": 3,
        "tabsTimeoutMs": 4000,
        # 已经在战报页时先返回再重新进入：否则列表不重新请求，pcap 里没有战报帧
        "forceReload": True,
    },
    "report": {"reportEra": "auto", "label": "sanmou-auto-war-report"},
    "server": {
        "configPath": "control/server_upload.json",
        "baseUrl": "",
        "waitVisibleSeconds": 180,
        "pollSeconds": 5,
    },
}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, extra: dict) -> dict:
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | Path | None = None) -> dict:
    """config.json > config.example.json > 内置默认值；支持环境变量覆盖 adb/serial。"""
    import copy

    config = copy.deepcopy(DEFAULT_CONFIG)
    explicit = Path(path) if path else None
    candidates = [explicit] if explicit else [
        SKILL_ROOT / "config.json",
        SKILL_ROOT / "config.example.json",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            loaded = json.loads(Path(candidate).read_text(encoding="utf-8-sig"))
            _deep_merge(config, loaded)
            break
    if config.get("python") == "":
        config["python"] = sys.executable
    return config


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (SKILL_ROOT / path)


# --------------------------------------------------------------------------- #
# 引擎模块（解码 / 提取 / 上传 / 视觉）原样内置于 scripts/engine
# --------------------------------------------------------------------------- #
def load_engine(name: str):
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


def json_print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


# --------------------------------------------------------------------------- #
# adb
# --------------------------------------------------------------------------- #
class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, adb_path: str, serial: str, timeout: int = 60):
        self.adb_path = adb_path
        self.serial = serial
        self.timeout = timeout

    def run(self, args: list[str], *, binary: bool = False,
            allow_failure: bool = False, timeout: int | None = None):
        command = [self.adb_path, "-s", self.serial, *args]
        try:
            proc = subprocess.run(
                command, capture_output=True,
                timeout=timeout or self.timeout, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as error:
            raise AdbError(f"找不到 adb：{self.adb_path}") from error
        except subprocess.TimeoutExpired as error:
            raise AdbError(f"adb 超时：{' '.join(args)}") from error
        if proc.returncode != 0 and not allow_failure:
            message = proc.stderr.decode("utf-8", "ignore").strip()
            raise AdbError(message or f"adb 失败（{proc.returncode}）：{' '.join(args)}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "ignore")

    def shell(self, *args: str, **kwargs) -> str:
        return self.run(["shell", *args], **kwargs)

    def devices(self) -> str:
        return subprocess.run(
            [self.adb_path, "devices"], capture_output=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("utf-8", "ignore")

    def ensure_online(self) -> None:
        if self.serial not in self.devices():
            raise AdbError(f"设备 {self.serial} 不在线：请先启动 MuMu 模拟器")

    def is_root(self) -> bool:
        return "uid=0(root)" in self.shell("id", allow_failure=True)

    def ensure_root(self) -> bool:
        """tcpdump 需要 root adbd；MuMu 支持 `adb root` 且不需要 Windows 提权。"""
        if self.is_root():
            return True
        self.run(["root"], allow_failure=True)
        self.run(["wait-for-device"], allow_failure=True)
        return self.is_root()

    def tap(self, x: int, y: int, wait_ms: int = 0) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))
        if wait_ms:
            time.sleep(wait_ms / 1000.0)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 600) -> None:
        self.shell("input", "swipe", str(int(x1)), str(int(y1)),
                   str(int(x2)), str(int(y2)), str(int(ms)))

    def key(self, code: str) -> None:
        self.shell("input", "keyevent", code)

    def screenshot(self, path: str | Path) -> tuple[int, int]:
        data = self.run(["exec-out", "screencap", "-p"], binary=True, timeout=90)
        if not data:
            raise AdbError("screencap 返回空数据")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        from PIL import Image  # 延迟导入，doctor 时不强制

        with Image.open(path) as image:
            return image.size

    def push(self, local: str | Path, remote: str) -> None:
        self.run(["push", str(local), remote])

    def pull(self, remote: str, local: str | Path) -> None:
        local_path = Path(local)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        # 该版 adb 在「含中文的目录下新建文件」时会报 cannot create file/directory，
        # 预先 touch 出空文件后 pull 就能正常覆盖。
        if not local_path.exists():
            local_path.touch()
        self.run(["pull", remote, str(local_path)], timeout=300)

    def rm(self, *remote: str) -> None:
        self.run(["shell", "rm", "-f", *remote], allow_failure=True)


def make_adb(config: dict) -> Adb:
    adb_path = config.get("adbPath") or "adb"
    return Adb(str(adb_path), str(config.get("serial") or ""))


# --------------------------------------------------------------------------- #
# 视觉 / 目标换算
# --------------------------------------------------------------------------- #
def load_color(path: str | Path):
    cv2 = load_engine("cv2")
    import numpy as np

    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"截图读取失败：{path}")
    return image


def inspect_screenshot(path: str | Path, compare: str | Path | None = None) -> dict:
    ui = load_engine("inspect_mumu_ui")
    template = TEMPLATES_DIR / "portrait_report_entry.png"
    return ui.inspect(
        load_color(path),
        load_color(compare) if compare else None,
        ui.load_gray(template) if template.is_file() else None,
    )


def _ocr_engine():
    """加载项目自带 RapidOCR（技能既可能在仓库外，也可能被拷进 sanmou_auto 仓库内）。"""
    from PIL import Image  # noqa: F401 - 依赖自检

    for root in (PROJECT_ROOT, PROJECT_ROOT.parent):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    try:
        from sanmou_auto import ocrengine
    except ModuleNotFoundError:  # 技能位于 sanmou_auto/ 内部：直接按同包模块导入
        import ocrengine  # type: ignore[no-redef]
    return ocrengine


def _ocr_text(image_bgr) -> str:
    """只对页签带这种小区域调用，耗时可接受。"""
    from PIL import Image

    return _ocr_engine().ocr_text(Image.fromarray(image_bgr[:, :, ::-1]), scale=2.0)


def tab_status(path: str | Path, config: dict) -> dict:
    """战报页判定：页签行 OCR 文字 + 每个页签的金色边框比例。

    上游 inspect_mumu_ui 的横屏页签阈值在城内视角会误报（建筑/顶栏也是金色），
    这里加一道「OCR 必须出现 交战/收藏/开拓」的硬门槛。
    """
    image = load_color(path)
    height, width = image.shape[:2]
    x1, y1, x2, y2 = config["targets"]["tabBand"]
    left, right = max(0, int(x1 * width)), min(width, int(x2 * width))
    top, bottom = max(0, int(y1 * height)), min(height, int(y2 * height))
    band = image[top:bottom, left:right]
    try:
        text = _ocr_text(band)
    except Exception:  # noqa: BLE001 - 没装 OCR 时退化为纯视觉
        text = ""
    gold = load_engine("inspect_mumu_ui").gold_ratio
    ratios: dict[str, float] = {}
    for name, rx1, rx2 in TAB_WINDOWS:
        roi = image[top:bottom, max(0, int(rx1 * width)):min(width, int(rx2 * width))]
        ratios[name] = round(float(gold(roi)), 4) if roi.size else 0.0
    best = max(ratios, key=ratios.get) if ratios else ""
    selected = best if ratios.get(best, 0.0) >= TAB_SELECTED_GOLD else ""
    report_page = bool(
        "战报" in text
        or ("交战" in text and "收藏" in text)
        or ("交战" in text and "开拓" in text)
    )
    return {
        "reportPage": report_page,
        "selectedTab": selected,
        "combatTabSelected": selected == "交战",
        "tabGold": ratios,
        "tabOcrText": text,
    }


def page_state(path: str | Path, config: dict) -> dict:
    """页签状态 + 遮挡检测，采集流程与 CLI 共用。"""
    state = tab_status(path, config)
    engine = inspect_screenshot(path)
    state["blockingOverlay"] = bool(engine.get("blockingOverlay"))
    state["dialogOverlay"] = bool(engine.get("dialogOverlay"))
    state["orientation"] = engine.get("orientation")
    return state


def pixels(target: list[float], width: int, height: int) -> tuple[int, int]:
    return int(round(target[0] * width)), int(round(target[1] * height))


def targets_for(config: dict, width: int, height: int) -> dict:
    targets = config["targets"]
    swipe = targets["swipe"]
    x = int(round(swipe["x"] * width))
    return {
        "reportEntry": pixels(targets["reportEntry"], width, height),
        "allianceTab": pixels(targets["allianceTab"], width, height),
        "combatTab": pixels(targets["combatTab"], width, height),
        "pageBack": pixels(targets["pageBack"], width, height),
        "tabBand": tuple(targets["tabBand"]),
        "swipe": (
            x, int(round(swipe["y1"] * height)),
            x, int(round(swipe["y2"] * height)),
            int(swipe["durationMs"]),
        ),
    }


def visual_difference(before: str | Path, after: str | Path) -> dict:
    ui = load_engine("inspect_mumu_ui")
    return ui.visual_difference(load_color(before), load_color(after))


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #
def wait_until(predicate, timeout_seconds: float, interval_seconds: float = 0.3):
    deadline = time.time() + max(0.1, timeout_seconds)
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_seconds)
    return None


def timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def which_python(config: dict) -> str:
    python = str(config.get("python") or "").strip()
    return python or sys.executable


def has_module(python: str, name: str) -> bool:
    code = f"import importlib.util as u,sys;sys.exit(0 if u.find_spec({name!r}) else 1)"
    result = subprocess.run(
        [python, "-c", code], capture_output=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0


def find_tcpdump(adb: Adb) -> str:
    for candidate in ("/system/xbin/tcpdump", "/system/bin/tcpdump", "/data/local/tmp/tcpdump"):
        output = adb.shell("test", "-x", candidate, "&&", "echo", candidate, allow_failure=True)
        if candidate in (output or ""):
            return candidate
    output = adb.shell("which", "tcpdump", allow_failure=True).strip()
    return output.splitlines()[0] if output else ""


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def copy_tree_hint() -> str:
    return f"引擎文件缺失：请确认 {ENGINE_DIR} 下存在解码/提取/上传脚本（见 reference/upstream.md）"


__all__ = [
    "SKILL_ROOT", "PROJECT_ROOT", "SCRIPTS_DIR", "ENGINE_DIR", "TEMPLATES_DIR",
    "WORK_DIR", "CAPTURE_ROOT", "CONTROL_DIR", "DEFAULT_CONFIG", "Adb", "AdbError",
    "load_config", "resolve_path", "load_engine", "json_print", "make_adb",
    "load_color", "inspect_screenshot", "targets_for", "visual_difference",
    "tab_status", "page_state", "TAB_WINDOWS", "TAB_SELECTED_GOLD",
    "pixels", "wait_until", "timestamp", "which_python", "has_module",
    "find_tcpdump", "ensure_dir", "copy_tree_hint", "shutil",
]
