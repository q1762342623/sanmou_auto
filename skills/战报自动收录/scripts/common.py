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
import re
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

def _is_portrait_env() -> bool:
    """读 SANMOU_ORIENT 或 screenshots/flow/_orientation.json 判断当前方向。"""
    import os
    forced = (os.environ.get("SANMOU_ORIENT") or "").strip().lower()
    if forced in ("portrait", "landscape"):
        return forced == "portrait"
    try:
        cache = (Path(__file__).resolve().parents[4]
                 / "screenshots" / "flow" / "_orientation.json")
        data = json.loads(cache.read_text(encoding="utf-8"))
        return str(data.get("orientation")).lower() == "portrait"
    except Exception:          # noqa: BLE001 - 无缓存时按横屏
        return False


PORTRAIT = _is_portrait_env()

# 战报页页签（x 比例区间，y 由 targets.tabBand 决定）；顺序与游戏内一致
TAB_WINDOWS = (
    ("全部", 0.020, 0.190),
    ("开拓", 0.200, 0.310),
    ("交战", 0.320, 0.430),
    ("收藏", 0.440, 0.580),
    ("个人", 0.620, 0.740),
    ("同盟", 0.740, 0.860),
)
if PORTRAIT:
    # 竖屏（900x1600）：页签行在底部 y≈1453，「个人/同盟」是另一行筛选（由 targets.allianceTab 直接点）
    TAB_WINDOWS = (
        ("全部", 0.100, 0.190),
        ("开拓", 0.300, 0.390),
        ("交战", 0.520, 0.610),
        ("收藏", 0.740, 0.830),
    )
TAB_SELECTED_GOLD = 0.05   # 选中页签金色边框比例下限（实测 0.15~0.18）
if PORTRAIT:
    # 竖屏页签更窄，选中态金色比例实测 ≈0.0486（横屏 0.15~0.18）
    TAB_SELECTED_GOLD = 0.03

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
        # 一次约 240px 手势（实测含惯性滚动 ≈360px < 视口 700px - 卡高 270px = 430px）：
        # 保证任何一张卡都会在某一屏完整露出来，不会因为步长过大被“跳过”
        # 2026-11-25 实测：+20%（0.318 屏高）会一次跨过 2 张卡，列表在 28/78 页
        # 就误判 list_end（记录 104 vs 234、展开卡 39 vs 129），已回滚。
        # 安全约束：滑动距离必须明显小于卡片高度（竖屏卡高≈400px，当前 368px）。
        "swipe": {"x": 0.36, "y1": 0.78, "y2": 0.515, "durationMs": 760},
    },
    "expand": {
        "roiX1": 0.900,
        "roiX2": 1.000,          # 金色竖条在 0.9625~0.9906，ROI 必须到屏幕右边缘
        "roiY1": 0.080,
        "roiY2": 0.960,
        "rightEdgeRatio": 0.945, # 竖条左沿必须贴最右侧（约 x=1540）；卡片金框（x≈1440）一律排除
        "templateScale": 0.9,    # 1600x900 实测（0.8/1.0 都检不到）
        "downThreshold": 0.85,   # 实测 down≈0.965 / up≈0.54；半截卡（0.46）必须排除
        "minMargin": 0.10,
        "minWidth": 14,
        "maxWidth": 56,          # 展开条实测宽 45；86 宽的是卡片场次框，误点会进详情页
        "minHeight": 150,        # 完整金色竖条约 216px，底部半截卡约 121px
        "maxHeight": 240,        # 实测 193~218；放开到 0.55*ROI 会把卡片整框吃进来
        "clickBottomInset": 26,  # 点击点距竖条底边（箭头双 V 的中心约在 h-28）
        "maxPerPage": 6,
        # 批量连点会让下方卡片随展开整体位移，确认/收起都容易错位；默认逐卡处理，
        # 稳定帧 + 箭头状态双确认，实测整体更快也更稳。
        "burstTap": False,
        "burstMax": 4,
        "burstGapMs": 80,
        "openConfirmMs": 1600,   # 等整屏静止且箭头翻成 up 的窗口
        "closeConfirmMs": 1600,  # 等整屏静止且箭头翻回 down 的窗口
        "settleMs": 220,         # 点击后短等；真正的成功判定看箭头翻面，不等整屏静止
        "retryTries": 1,         # 只有「画面完全没变」才补点一次，且必须用静止后的新坐标
        "maxRecoveriesPerPage": 2,  # 本页最多容忍 2 次误入详情并回退，避免死循环
        # 竖条底边 864px（0.96 屏高）实测点击有效；只有更靠底（>0.955）才先上滑
        "bottomSafeRatio": 0.955,
        "cutRatio": 0.97,        # 竖条被屏幕底边截断（y+h 超过该比例）先上滑
        "finalSettleMs": 350,    # 本页展开完再等列表稳定，随后才滑动
    },
    "ports": {"fallback": [20020, 20050, 20060, 20090], "min": 10000, "max": 65535},
    "capture": {
        "pageDelayMs": 900,
        "swipeDelayMs": 700,
        # 0 = 不设页数上限，必须连续多屏无变化（list_end）才结束一轮
        "maxPages": 0,
        "hardMaxPages": 500,       # 仅防界面异常死循环的安全上限
        "stalledPagesToStop": 2,   # 连续两屏滑不动 = 到底（每屏内部已含一次换手势重试）
        "stalledProbeDelayMs": 1500,  # 判“滑不动”前再等一次，排除列表仍在加载
        # 游戏偶尔会吞掉滑动（实测 40 次里 4 次“假到底”）：换更低/更慢/更长的第二次手势
        "swipeRetryY1": 0.84,
        "swipeRetryY2": 0.60,
        "swipeRetryDurationMs": 800,
        "requireListEnd": True,    # production 没翻到底 -> 保留 capture 但拒绝上传
        "visualDiffMean": 3.0,
        "changedRatio": 0.03,
        # 滑动/展开之后列表还会因为懒加载和惯性继续上顶 1.3~1.6s（实测 y 从 363 走到 284）。
        # 必须等「连续两帧几乎一致」再取点击坐标，否则落点会偏下 40~90px 点进战报详情页。
        "stableTimeoutMs": 2600,      # 等列表静止的最长时间
        "stableIntervalMs": 180,      # 静止检测的采样间隔
        "stableDiffMean": 0.9,        # 两帧平均差 <= 该值视为静止（实测静止≈0.05）
        "stableChangedRatio": 0.002,  # 两帧变化像素比例上限（实测静止≈0.000）
        "navigateRetries": 3,
        "tabsTimeoutMs": 4000,
        # 已经在战报页时先返回再重新进入：否则列表不重新请求，pcap 里没有战报帧
        "forceReload": True,
    },
    "report": {"reportEra": "auto", "label": "sanmou-auto-war-report"},
    # 每轮采集/上报后汇总交战坐标，输出最高频点（见 scripts/coordinate_stats.py）
    "coordinates": {"enabled": True, "topN": 10, "minBlobSize": 1000},
    "server": {
        "configPath": "control/server_upload.json",
        "baseUrl": "",
        # 服务器有 180s 发布静默窗口 + 约 120s 构建，180s 必然超时
        "waitVisibleSeconds": 900,
        "pollSeconds": 5,
    },
}

# 900x1600 竖屏实测覆盖（2026-11-21，见 plans/竖屏适配.md）
# 竖屏是重排布局：战报入口在左下交叉剑，页签行在底部，卡片 4 条/屏。
PORTRAIT_CONFIG: dict = {
    "screen": {"width": 900, "height": 1600},
    "targets": {
        "reportEntry": [0.420, 0.823],      # 主界面左下「军报」≈ (378,1317)
        "allianceTab": [0.613, 0.964],      # 底部筛选「同盟」≈ (552,1542)
        "combatTab": [0.561, 0.908],        # 页签「交战」≈ (505,1453)
        "pageBack": [0.159, 0.965],         # 底部「返回」≈ (143,1544)
        "tabBand": [0.0, 0.895, 1.0, 0.925],
        # 2026-11-25 实测：+20%（442px > 卡片高度≈400px）会跳卡，已回滚到 368px。
        # 提速应压缩每页的展开/静止等待，而不是加大滑动距离。
        "swipe": {"x": 0.50, "y1": 0.78, "y2": 0.55, "durationMs": 760},
    },
    "expand": {
        "roiX1": 0.930,
        "roiX2": 1.000,
        "roiY1": 0.060,
        "roiY2": 0.920,
        # 竖屏实测（2026-11-22，900x1600）：折叠卡右侧金色竖条 x≈837..875：
        # 左沿 837/900≈0.930，用 0.965 会把它整体排除（表现为 expandedCards=0）。
        "rightEdgeRatio": 0.920,
        "minWidth": 10,
        "maxWidth": 45,
        "minHeight": 100,
        # 「连战 N 场」折叠竖条实测高 311px（原上限 260 会把所有竖条漏检）
        "maxHeight": 420,
        "clickBottomInset": 20,
        "maxPerPage": 8,
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
    if PORTRAIT:
        # 方向 profile 必须最后应用：skill 目录里的 config.json 仍是横屏基准值
        _deep_merge(config, copy.deepcopy(PORTRAIT_CONFIG))
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

    def exit_dialog_visible(self) -> bool:
        """是否弹出「确认退出游戏」（com.gsc.exit.ExitActivity，按 BACK 会触发）。"""
        out = self.run(["shell", "dumpsys", "window", "windows"],
                       allow_failure=True, timeout=20)
        return "ExitActivity" in out

    def cancel_exit_dialog(self) -> bool:
        """点「取消」关掉「确认退出游戏」，绝不点「确定」。"""
        point = (716, 494)          # 1600x900 兜底坐标
        try:
            self.run(["shell", "uiautomator", "dump", "/sdcard/ui_exit.xml"],
                     allow_failure=True, timeout=30)
            xml = self.run(["shell", "cat", "/sdcard/ui_exit.xml"],
                           allow_failure=True, timeout=20)
            matched = re.search(
                r'tv_gs_exit_cancel[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if matched:
                x1, y1, x2, y2 = (int(v) for v in matched.groups())
                point = ((x1 + x2) // 2, (y1 + y2) // 2)
        except Exception:               # noqa: BLE001
            pass
        self.tap(*point, wait_ms=1200)
        return not self.exit_dialog_visible()

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

    def _grab_png(self):
        """兜底：设备 raw 格式不认识时退回 PNG（设备端编码约慢 0.4s）。"""
        cv2 = load_engine("cv2")
        import numpy as np

        data = self.run(["exec-out", "screencap", "-p"], binary=True, timeout=90)
        if not data:
            raise AdbError("screencap 返回空数据")
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise AdbError("screencap PNG 解码失败")
        return image

    def grab(self):
        """抓一帧并在内存里解成 BGR ndarray（默认走 raw，约 0.24s/帧）。

        `screencap -p` 要在设备端做 PNG 编码，实测 0.62~0.68s/帧；
        不带 `-p` 的 raw RGBA 只需 0.22~0.24s，本地 numpy 解码 <1ms。
        帧头：width(4) height(4) format(4) [colorspace(4)]，全部小端。
        返回 BGR 顺序（与 cv2 约定一致），供模板/OCR 直接复用。
        """
        import numpy as np

        data = self.run(["exec-out", "screencap"], binary=True, timeout=90)
        if not data or len(data) < 16:
            return self._grab_png()
        raw = np.frombuffer(data, dtype=np.uint8)
        width = int.from_bytes(raw[0:4].tobytes(), "little")
        height = int.from_bytes(raw[4:8].tobytes(), "little")
        fmt = int.from_bytes(raw[8:12].tobytes(), "little")
        header = len(raw) - width * height * 4
        if width <= 0 or height <= 0 or header not in (12, 16):
            return self._grab_png()
        pixels = raw[header:header + width * height * 4].reshape((height, width, 4))
        # format=1 是 RGBA_8888：BGR 取 [2,1,0]；其它按 RGBX 处理
        bgr = pixels[:, :, 2::-1] if fmt == 1 else pixels[:, :, :3]
        return np.ascontiguousarray(bgr)

    def screenshot_fast(self, path: str | Path | None = None):
        """抓一帧（内存 BGR），需要留证时再写 PNG；返回 ndarray。"""
        image = self.grab()
        if path is not None:
            save_color(image, path)
        return image

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


def to_color(image):
    """接受「路径」或「已解码 BGR ndarray」，统一返回 ndarray（避免重复解码）。"""
    if isinstance(image, (str, Path)):
        return load_color(image)
    return image


def save_color(image, path: str | Path) -> None:
    """把内存帧写成 PNG 留证（中文路径用 imencode + write_bytes 最稳）。"""
    cv2 = load_engine("cv2")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"截图编码失败：{path}")
    path.write_bytes(buffer.tobytes())


def inspect_screenshot(image, compare=None) -> dict:
    ui = load_engine("inspect_mumu_ui")
    template = TEMPLATES_DIR / "portrait_report_entry.png"
    return ui.inspect(
        to_color(image),
        to_color(compare) if compare else None,
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


def tab_status(image, config: dict) -> dict:
    """战报页判定：页签行 OCR 文字 + 每个页签的金色边框比例。

    上游 inspect_mumu_ui 的横屏页签阈值在城内视角会误报（建筑/顶栏也是金色），
    这里加一道「OCR 必须出现 交战/收藏/开拓」的硬门槛。
    `image` 可以是路径，也可以是已解码帧（采集热路径复用同一帧，省一次解码）。
    """
    image = to_color(image)
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


def page_state(image, config: dict) -> dict:
    """页签状态 + 遮挡检测，采集流程与 CLI 共用。

    同一帧只解码一次：tab_status / inspect_screenshot 共用传入的 ndarray。
    """
    image = to_color(image)
    state = tab_status(image, config)
    engine = inspect_screenshot(image)
    state["blockingOverlay"] = bool(engine.get("blockingOverlay"))
    state["dialogOverlay"] = bool(engine.get("dialogOverlay"))
    state["orientation"] = engine.get("orientation")
    return state


def map_visible(path: str | Path, config: dict) -> bool:
    """OCR 底栏「武将/同盟/征战」判断是否已回到世界地图。

    技能自带的模板 `mainScreen` 在实战界面会误判（地图上为 false、
    战报页反而为 true），所以回图判定一律以底栏 OCR 为准。
    """
    try:
        image = load_color(path)
        height, width = image.shape[:2]
        crop = image[int(0.867 * height):int(0.989 * height),
                     int(0.375 * width):int(0.9125 * width)]
        text = _ocr_text(crop)
    except Exception:               # noqa: BLE001
        return False
    flat = text.replace(" ", "").replace("证战", "征战")
    return ("武将" in flat or "同盟" in flat) and "征战" in flat


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


def visual_difference(before, after) -> dict:
    ui = load_engine("inspect_mumu_ui")
    return ui.visual_difference(to_color(before), to_color(after))


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
    "load_color", "to_color", "save_color", "inspect_screenshot", "targets_for",
    "visual_difference", "tab_status", "page_state", "TAB_WINDOWS",
    "TAB_SELECTED_GOLD",
    "pixels", "wait_until", "timestamp", "which_python", "has_module",
    "find_tcpdump", "ensure_dir", "copy_tree_hint", "shutil",
]
