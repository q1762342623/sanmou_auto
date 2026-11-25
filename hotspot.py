"""战报「最高频次交战坐标」的读取 / 跟踪。

新流程**不再分析邮件**：每一轮先跑一次完整的战报收集（采集 → 提取 → 上传），
再从本轮战报里取交战频次最高的坐标作为行军目标；过程中的等待时间继续收集，
每次收集都用**最新的热点**更新状态（但已经开始的调动/行军不因热点变化而中断）。

数据来源（按优先级）：
1. `ReportCollector.collect()` 返回的 `coordinateHotspot.hottest`
   （由 `skills/战报自动收录/scripts/coordinate_stats.py` 生成）；
2. `sanmou_auto/skills/战报自动收录/work/latest_hotspot.json`
   （`report_collector` 实际调用的那份技能目录）；
3. 根目录 `skills/战报自动收录/work/latest_hotspot.json`（独立跑收录时）；
4. `screenshots/flow/latest_hotspot.json`（本模块写出的统一副本）。

落盘：
- `screenshots/flow/latest_hotspot.json`：最近一次热点（统一格式）；
- `screenshots/flow/hotspot_history.jsonl`：每次热点变化追加一条；
- `screenshots/flow/hotspot_state.json`：当前热点 / 变化时间 / 来源 captureId。

本模块只做纯数据读取与记录，不操作设备，便于离线自测。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LATEST_PATH = ROOT / "screenshots" / "flow" / "latest_hotspot.json"
HISTORY_PATH = ROOT / "screenshots" / "flow" / "hotspot_history.jsonl"
STATE_PATH = ROOT / "screenshots" / "flow" / "hotspot_state.json"

SKILL_HOTSPOT_PATHS = (
    ROOT / "sanmou_auto" / "skills" / "战报自动收录" / "work" / "latest_hotspot.json",
    ROOT / "skills" / "战报自动收录" / "work" / "latest_hotspot.json",
)
SKILL_HOTSPOT_TEXT_PATHS = (
    ROOT / "sanmou_auto" / "skills" / "战报自动收录" / "work" / "latest_hotspot.txt",
    ROOT / "skills" / "战报自动收录" / "work" / "latest_hotspot.txt",
)


def _int_pair(x, y) -> tuple[int, int] | None:
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return None
    if 0 < x < 2000 and 0 < y < 2000:
        return x, y
    return None


def normalize_hotspot(payload) -> dict | None:
    """把多种来源统一成 `{x,y,count,share}`；无效返回 None。"""
    if not isinstance(payload, dict):
        return None
    if "hottest" in payload:
        return normalize_hotspot(payload.get("hottest"))
    pair = _int_pair(payload.get("x"), payload.get("y"))
    if pair is None:
        return None
    try:
        count = int(payload.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        share = float(payload.get("share") or 0.0)
    except (TypeError, ValueError):
        share = 0.0
    return {"x": pair[0], "y": pair[1], "count": count,
            "share": round(share, 4)}


def hottest_from_result(result: dict | None) -> dict | None:
    """从 `ReportCollector.collect()` 的单次结果里取 hottest。"""
    if not isinstance(result, dict):
        return None
    return normalize_hotspot(result.get("coordinateHotspot"))


def parse_hotspot_text(text: str) -> dict | None:
    """解析 `latest_hotspot.txt`：`x,y,count,share` 或 `none`。"""
    flat = (text or "").strip()
    if not flat or flat.lower() == "none":
        return None
    parts = [p.strip() for p in flat.replace("，", ",").split(",")]
    if len(parts) < 2:
        return None
    pair = _int_pair(parts[0], parts[1])
    if pair is None:
        return None
    try:
        count = int(float(parts[2])) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        count = 0
    try:
        share = float(parts[3]) if len(parts) > 3 else 0.0
    except (TypeError, ValueError):
        share = 0.0
    return {"x": pair[0], "y": pair[1], "count": count,
            "share": round(share, 4)}


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def load_latest_hotspot() -> dict | None:
    """读最近一次热点（JSON 优先，其次 txt）。"""
    for path in (LATEST_PATH, *SKILL_HOTSPOT_PATHS):
        payload = _read_json(path)
        hotspot = normalize_hotspot(payload)
        if hotspot:
            return hotspot
    for path in SKILL_HOTSPOT_TEXT_PATHS:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        hotspot = parse_hotspot_text(text)
        if hotspot:
            return hotspot
    return None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def save_latest_hotspot(hotspot: dict, *, capture_id: str = "",
                        source: str = "report",
                        path: str | Path | None = None) -> dict:
    """统一落盘到 screenshots/flow/latest_hotspot.json（可指定 path 供测试隔离）。"""
    payload = dict(hotspot)
    payload["captureId"] = capture_id or ""
    payload["source"] = source
    payload["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload["updatedEpoch"] = time.time()
    _write_json(Path(path) if path else LATEST_PATH, payload)
    return payload


def same_coord(a: dict | tuple | None, b: dict | tuple | None,
               radius: float = 0.0) -> bool:
    """两个坐标是否同一个（或距离不超过 radius 格）。"""
    def _xy(value):
        if isinstance(value, dict):
            return _int_pair(value.get("x"), value.get("y"))
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            return _int_pair(value[0], value[1])
        return None

    pa, pb = _xy(a), _xy(b)
    if not pa or not pb:
        return False
    if radius <= 0:
        return pa == pb
    return ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2) ** 0.5 <= radius


class HotspotTracker:
    """跨多次战报收集跟踪「当前热点」。

    - `update(result)`：喂入一次 `ReportCollector.collect()` 结果（或直接喂热点）；
    - `current`：当前热点（dict，可能为 None）；
    - `changed`：相比上一次是否换了坐标（距离 > `radius`）；
    - 每次变化写 history（用于复盘「为什么打到这个点」）。
    """

    def __init__(self, radius: float = 6.0, log=print,
                 history_path: str | Path | None = None,
                 state_path: str | Path | None = None,
                 latest_path: str | Path | None = None,
                 reset: bool = False):
        """`reset=True` 时不读历史热点（每次启动都是独立新任务要用这个）。"""
        self.radius = float(radius)
        self.log = log
        self.history_path = Path(history_path) if history_path else HISTORY_PATH
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.latest_path = Path(latest_path) if latest_path else LATEST_PATH
        self.current: dict | None = None
        self.capture_id = ""
        self.updated_at: float | None = None
        self.changed = False
        if not reset:
            self._load()

    # ---------------------------------------------------------------- 状态
    def _load(self) -> None:
        payload = _read_json(self.state_path)
        if not payload:
            return
        hotspot = normalize_hotspot(payload.get("current"))
        if hotspot:
            self.current = hotspot
            self.capture_id = str(payload.get("captureId") or "")
            try:
                self.updated_at = float(payload.get("updatedAtEpoch") or 0) or None
            except (TypeError, ValueError):
                self.updated_at = None

    def _save(self) -> None:
        if not self.current:
            return
        _write_json(self.state_path, {
            "current": self.current,
            "captureId": self.capture_id,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updatedAtEpoch": self.updated_at or time.time(),
            "radius": self.radius,
        })

    def _append_history(self, hotspot: dict, trigger: str) -> None:
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "coord": [hotspot["x"], hotspot["y"]],
                    "count": hotspot.get("count"),
                    "share": hotspot.get("share"),
                    "captureId": self.capture_id,
                    "trigger": trigger,
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- 更新
    def update(self, result: dict | None = None, *, trigger: str = "") -> dict | None:
        """优先用本次收集结果；结果里没有热点时读技能目录的 latest 文件。"""
        hotspot = hottest_from_result(result)
        if hotspot is None:
            hotspot = load_latest_hotspot()
        if hotspot is None:
            return self.current
        capture_id = ""
        if isinstance(result, dict):
            capture_id = str(result.get("captureId") or "")
        changed = not same_coord(self.current, hotspot, self.radius)
        self.changed = changed
        if changed:
            self.current = hotspot
            self.capture_id = capture_id
            self.updated_at = time.time()
            self._append_history(hotspot, trigger or "collect")
            save_latest_hotspot(hotspot, capture_id=capture_id,
                                source=trigger or "collect",
                                path=self.latest_path)
            self._save()
            self.log(f"  [热点] ({hotspot['x']},{hotspot['y']}) "
                     f"{hotspot.get('count')}场 "
                     f"{float(hotspot.get('share') or 0) * 100:.1f}%"
                     + (f"（触发={trigger}）" if trigger else ""))
        else:
            # 坐标没变也要更新 count/share（新一轮战报场次会变化），
            # 但不写 history、不标记 changed（避免误触发换点）。
            self.current = hotspot
            self.capture_id = capture_id or self.capture_id
            self.updated_at = time.time()
            self._save()
        return self.current

    def get(self) -> dict | None:
        if self.current is None:
            self.current = load_latest_hotspot()
        return self.current

    def coord(self) -> tuple[int, int] | None:
        hotspot = self.get()
        if not hotspot:
            return None
        return int(hotspot["x"]), int(hotspot["y"])

    def describe(self) -> str:
        hotspot = self.get()
        if not hotspot:
            return "无热点"
        return (f"({hotspot['x']},{hotspot['y']}) "
                f"{hotspot.get('count')}场 "
                f"{float(hotspot.get('share') or 0) * 100:.1f}%")


__all__ = [
    "LATEST_PATH", "HISTORY_PATH", "STATE_PATH", "HotspotTracker",
    "normalize_hotspot", "hottest_from_result", "parse_hotspot_text",
    "load_latest_hotspot", "save_latest_hotspot", "same_coord",
]
