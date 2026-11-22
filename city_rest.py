"""低兵力部队「进城歇兵」：送到附近城池自动征兵，征满 33000 后召回主城。

口径（2026-11-22 用户确认）：满编 = **33000**；只要不满 33000 就不再出征，
而是调动到「附近我方归属的最近据点（优先城池）」，在城里征兵直到满编才回城。

自动拉线的一轮里，如果某支部队补兵后仍不满（`Mobilizer` 标记 `low-strength`），
不再只是整轮跳过，而是：

1. **其他部队至少有一支成功调动后**，把这批低兵力部队 `调动` 到「附近城池」
   （同盟城池列表里离战场最近的一座，排除本轮目标据点）——
   部队驻扎在城池期间，城池会按游戏机制自动征兵；
2. 之后每一轮**发起调动之前**，先检查这些城池里的部队兵力，
   已经征满的逐支「撤回/撤退」回主城，再走正常的「调动据点 → 进攻」流程。

状态落在 `screenshots/flow/city_rest.json`，跨轮持久化；文件里的
`rests[]` 每项记录 部队序号 / 城池 / 送出时间 / 预计到达 / 最近一次观察到的兵力。

安全约束：
- 只有「其他队伍已被成功调动」才送（`allow_send`），避免全队都塞进城里空转一轮；
- 召回只认面板/详情里的 **撤回 / 撤退 / 返回主城 / 回城** 文字，
  排除 `自动征兵 / 取消` 等字样；找不到按钮就保持原状，下一轮再试；
- 只对状态仍是本城池「驻守 / 调动」且兵力已满的部队执行召回。

用法：
    python -m sanmou_auto.city_rest --status
    python -m sanmou_auto.city_rest --recall
    python -m sanmou_auto.city_rest --clear
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import camp_cache, config, ocrengine, vision
from .adb import Adb
from .alliance import AllianceFacilities, Camp
from .mobilize import STRENGTH_FULL, Mobilizer
from .timing import STAGE_MOBILIZE, StepTimer

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = "screenshots/flow/city_rest.json"
# 历史版本把 STATE_PATH 又过了一次 vision.shot_path()，状态实际写在
# screenshots/flow/screenshots/flow/city_rest.json；这里保留只读兼容并自动迁移。
LEGACY_STATE_PATH = "screenshots/flow/screenshots/flow/city_rest.json"
STATE_SCHEMA_VERSION = 1
STALE_SECONDS = 6 * 3600          # 超过 6 小时没满的歇兵记录直接丢弃（避免死循环）
ARRIVE_GRACE_SECONDS = 20         # 预计到达后再等 20s 才做第一次满兵检查
GROWTH_MIN = 50                   # 相邻两轮兵力增长小于该值视为「城里没在征兵」
RECRUIT_TRIES = 3                 # 城里「点征兵」最多尝试次数

# 「附近城池」只认城池列表（城/水寨/军港/民港），不要同盟大营/堡垒
CITY_KIND_HINTS = ("城", "水寨", "军港", "民港")
CITY_EXCLUDE_HINTS = ("同盟大营", "堡垒", "攻城大营")

# 召回按钮文字：只认明确的「让部队回来」
RECALL_HINTS = ("撤回", "撤退", "返回主城", "返回", "回城")
RECALL_EXCLUDE = ("自动", "征兵", "消耗", "取消", "确定", "确认")
# 城池里手动征兵（城池自动征兵没生效时兜底点一次）
RECRUIT_HINTS = ("征兵", "招募")
RECRUIT_EXCLUDE = ("自动", "建筑内", "今日已征", "消耗", "取消", "确定", "确认")
# 面板右侧 + 底部按钮带 + 卡片详情页底部（竖屏为布局估算，待实测校准）
RECALL_BOXES = config._p(
    ((1020, 140, 1600, 890),
     (280, 700, 1320, 890),
     (1020, 560, 1600, 890),
     (300, 300, 1300, 780)),
    ((700, 100, 900, 1400),
     (0, 1200, 900, 1560),
     (700, 900, 900, 1560),
     (0, 300, 900, 1300)))
CONFIRM_BOX = config._p((380, 280, 1280, 820), (250, 760, 660, 920))
CONFIRM_HINTS = ("确定", "确认", "撤回", "撤退")


@dataclass
class RestEntry:
    troop: int
    city_name: str
    city_coord: tuple[int, int]
    city_kind: str = ""
    city_source: str = "city"
    city_page: int = 0          # 城池列表第几屏（加速下次定位）
    sent_at: float = 0.0
    arrive_at: str = ""          # 服务器 HH:MM:SS
    arrive_epoch: float = 0.0    # 本地 epoch 推算到达时刻
    strength: int | None = None
    status: str = ""
    attempts: int = 0

    def as_dict(self) -> dict:
        return {
            "troop": self.troop,
            "city": {"name": self.city_name, "coord": list(self.city_coord),
                     "kind": self.city_kind, "source": self.city_source,
                     "page": self.city_page},
            "sentAt": self.sent_at,
            "arriveAt": self.arrive_at,
            "arriveEpoch": self.arrive_epoch,
            "strength": self.strength,
            "status": self.status,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RestEntry":
        city = raw.get("city") or {}
        coord = city.get("coord") or (0, 0)
        return cls(
            troop=int(raw.get("troop") or 0),
            city_name=str(city.get("name") or ""),
            city_coord=(int(coord[0]), int(coord[1])),
            city_kind=str(city.get("kind") or ""),
            city_source=str(city.get("source") or "city"),
            city_page=int(city.get("page") or 0),
            sent_at=float(raw.get("sentAt") or 0),
            arrive_at=str(raw.get("arriveAt") or ""),
            arrive_epoch=float(raw.get("arriveEpoch") or 0),
            strength=raw.get("strength"),
            status=str(raw.get("status") or ""),
            attempts=int(raw.get("attempts") or 0),
        )

    def camp(self) -> Camp:
        return Camp(name=self.city_name, coord=self.city_coord, point=(0, 0),
                    source=self.city_source, kind=self.city_kind,
                    page=self.city_page)


def _state_path(path: str | Path | None = None) -> Path:
    """歇兵状态文件路径（**相对项目根目录**，不要再叠加 screenshots 前缀）。"""
    if path is None:
        target = ROOT / STATE_PATH
        legacy = ROOT / LEGACY_STATE_PATH
        if not target.is_file() and legacy.is_file():
            return legacy
        return target
    p = Path(path)
    if p.is_absolute():
        return p
    # 带目录的相对路径按项目根目录解析；只给文件名时放进 screenshots/flow
    if p.parent != Path("."):
        return ROOT / p
    return ROOT / "screenshots" / "flow" / p


def load_state(path: str | Path | None = None) -> list[RestEntry]:
    p = _state_path(path)
    if not p.is_file():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    rows = payload.get("rests") or [] if isinstance(payload, dict) else []
    out: list[RestEntry] = []
    for raw in rows:
        try:
            entry = RestEntry.from_dict(raw)
        except (TypeError, ValueError):
            continue
        if entry.troop > 0 and entry.city_coord != (0, 0):
            out.append(entry)
    return out


def save_state(entries: list[RestEntry], path: str | Path | None = None) -> Path:
    p = _state_path(path)
    if path is None:
        p = ROOT / STATE_PATH          # 首次写入统一落到规范路径
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "rests": [entry.as_dict() for entry in entries],
    }
    temporary = p.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, p)
    if path is None:
        legacy = ROOT / LEGACY_STATE_PATH
        if legacy.is_file() and legacy != p:
            try:
                legacy.unlink()        # 迁移完删掉旧位置，避免两份状态互相打架
            except OSError:
                pass
    return p


def _to_seconds(text: str) -> int | None:
    parts = [p for p in str(text).replace("：", ":").split(":") if p.strip()]
    if len(parts) == 3:
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return None
    if len(parts) == 2:
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None
    return None


def _now_seconds(clock_text: str) -> int:
    sec = _to_seconds(clock_text)
    if sec is None:
        now = time.localtime()
        return now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    return sec


def _arrive_epoch(arrive_at: str, clock_text: str) -> float:
    arrive = _to_seconds(arrive_at)
    if arrive is None:
        return 0.0
    now = _now_seconds(clock_text)
    delta = (arrive - now) % 86400
    return time.time() + delta


def _find_text_point(img, hints: tuple[str, ...], exclude: tuple[str, ...],
                     boxes=RECALL_BOXES):
    k = img.width / 1600.0
    for box in boxes:
        scaled = (int(box[0] * k), int(box[1] * k),
                  min(img.width, int(box[2] * k)), min(img.height, int(box[3] * k)))
        if scaled[2] - scaled[0] < 10 or scaled[3] - scaled[1] < 10:
            continue
        for span in ocrengine.ocr(vision.crop(img, scaled), 2.0):
            text = span.text.replace(" ", "")
            if any(bad in text for bad in exclude):
                continue
            if any(hint in text for hint in hints):
                point = (int((scaled[0] + span.cx) / k),
                         int((scaled[1] + span.cy) / k))
                return point, text
    return None, ""


class CityRest:
    """低兵力歇兵 / 征满召回。"""

    def __init__(self, adb: Adb | None = None, fac: AllianceFacilities | None = None,
                 log=print, timer: StepTimer | None = None,
                 state_path: str | Path | None = None):
        self.adb = adb or Adb()
        self.fac = fac or AllianceFacilities(self.adb, log=log)
        self.log = log
        self.timer = timer
        self.state_path = state_path

    # -- 状态 -------------------------------------------------------------
    def entries(self) -> list[RestEntry]:
        return load_state(self.state_path)

    def clear(self) -> None:
        save_state([], self.state_path)

    def _save(self, entries: list[RestEntry]) -> None:
        save_state(entries, self.state_path)

    def _mark(self, name: str, kind: str = "执行", note: str = "") -> None:
        if self.timer is not None:
            self.timer.mark(name, kind, stage=STAGE_MOBILIZE, note=note)

    # -- 选城 -------------------------------------------------------------
    @staticmethod
    def _filter_cities(camps: list[Camp], exclude: Camp | None) -> list[Camp]:
        cities: list[Camp] = []
        for c in camps:
            kind = c.kind or c.kind_text
            if any(bad in kind for bad in CITY_EXCLUDE_HINTS):
                continue
            if not any(hint in kind for hint in CITY_KIND_HINTS):
                continue
            if exclude is not None and tuple(c.coord) == tuple(exclude.coord):
                continue
            cities.append(c)
        return cities

    def _pick_city(self, battlefield: tuple[int, int],
                   exclude: Camp | None = None,
                   camps: list[Camp] | None = None) -> Camp | None:
        """离战场最近的「城池」（城/水寨/军港/民港），排除本轮目标据点。

        `camps` 传入时直接复用（来自 camp_cache 的一次性全量快照），
        既不会每轮重扫城池列表，也不会和主流程的据点缓存出现两份数据；
        只有缓存里挑不出城池时才补扫一次并刷新缓存。
        """
        if not camps:
            camps = camp_cache.load_camps()[0]
        cities = self._filter_cities(camps, exclude)
        if not cities:
            self.log("  · 缓存里没有可用的城/水寨/军港/民港 → 补扫一次据点列表")
            camps = self.fac.scan_all()
            if camps:
                camp_cache.save_camps(camps, battlefield=battlefield)
            cities = self._filter_cities(camps, exclude)
        if not cities:
            self.log("  × 城池列表里没有可用的城/水寨/军港/民港")
            return None
        # 优先「城池」，城池都不可用时才退到水寨/军港/民港；同类按距战场由近到远
        def _rank(c: Camp) -> tuple[int, float]:
            kind = c.kind or c.kind_text
            return (0 if "城" in kind else 1, c.dist(battlefield))

        best = min(cities, key=_rank)
        self.log(f"== 歇兵城池：{best.describe()}（距战场 {best.dist(battlefield):.1f}）")
        return best

    # -- 送低兵力进城 -------------------------------------------------------
    def send_low_strength(self, indexes: tuple[int, ...],
                           battlefield: tuple[int, int],
                           camp: Camp | None = None,
                           allow_send: bool = True,
                           camps: list[Camp] | None = None) -> dict:
        """把补不满的部队调动到附近城池（城池会按游戏机制自动征兵）。

        `allow_send=False` 只表示「本轮没有其他队伍出征」，不再因此放弃——
        空兵部队留在主城不会自动征兵，送进城才有机会补满，所以照样送。
        """
        indexes = tuple(int(i) for i in indexes if 1 <= int(i) <= 4)
        result: dict = {"ok": False, "reason": "", "troops": list(indexes)}
        if not indexes:
            result["reason"] = "no-low-strength-troop"
            return result
        # 已经在歇兵城池里的部队不用再送一次：城池本来就会自动征兵，
        # 重复「送城」只会多一次无效调动，还可能把状态点乱。
        held = {e.troop for e in self.entries()}
        indexes = tuple(i for i in indexes if i not in held)
        result["troops"] = list(indexes)
        if not indexes:
            result["reason"] = "already-resting"
            self.log("  · 低兵力部队已在歇兵城池，跳过重复送城")
            return result
        if not allow_send:
            self.log("  · 本轮没有其他队伍出征，仍把低兵力部队送进城征兵"
                     "（留在主城补不满）")

        city = self._pick_city(battlefield, exclude=camp, camps=camps)
        if city is None:
            result["reason"] = "no-city"
            return result

        if not self._select_city(city):
            result["reason"] = "city-select-failed"
            self.log("  × 城池选不中，跳过本轮歇兵")
            return result
        self._mark("选中歇兵城池", "执行", note=city.describe())

        mob = Mobilizer(self.adb, log=self.log,
                        reselect=lambda: self._reselect_city(city),
                        timer=self.timer)
        try:
            mr = mob.mobilize(indexes=indexes, min_strength=0,
                              replenish=False, send_stationed=True)
        finally:
            mob.close()
            self.fac.back_to_map()

        # 只替换本次送出的部队记录；同城其他仍在歇兵的部队保留
        entries = [e for e in self.entries() if e.troop not in indexes]
        sent: list[RestEntry] = []
        for troop in mr.troops:
            if not troop.dispatched:
                continue
            entry = RestEntry(
                troop=troop.index,
                city_name=city.name,
                city_coord=tuple(city.coord),
                city_kind=city.kind or city.kind_text,
                city_source=city.source,
                city_page=city.page,
                sent_at=time.time(),
                arrive_at=troop.arrive_at or "",
                arrive_epoch=_arrive_epoch(troop.arrive_at or "", mob.clock.now()),
                strength=troop.strength,
                status=troop.status,
            )
            sent.append(entry)
            entries.append(entry)
        self._save(entries)
        result.update({"ok": bool(sent), "sent": [e.as_dict() for e in sent],
                       "city": city.describe(),
                       "statuses": {t.index: t.status for t in mr.troops}})
        if not sent:
            result["reason"] = "dispatch-failed"
            self.log("  × 低兵力部队没能调动进歇兵城池")
        else:
            self.log("  ✓ 已进城歇兵: " + ", ".join(
                f"部队{e.troop}({e.city_name})" for e in sent))
        self._mark("低兵部队进城歇兵", "执行",
                   note=f"{[e.troop for e in sent]} @{city.coord}")
        return result

    # -- 召回 -------------------------------------------------------------
    def _select_city(self, city: Camp) -> bool:
        """选中歇兵城池：优先沙盘坐标直跳（~15~22s），失败回退列表 select（慢）。"""
        try:
            from .sandbox import SandboxJump
            sb = SandboxJump(self.adb, log=self.log)
            if sb.goto((int(city.coord[0]), int(city.coord[1])), tries=4):
                if self.fac.has_mobilize_button():
                    self.log(f"  ✓ 沙盘直跳选中 {city.describe()}")
                    return True
                self.log("  ! 沙盘跳转后没有「调动」，回退列表选城")
        except Exception as e:  # noqa: BLE001 - 快路径失败不阻断歇兵流程
            self.log(f"  ! 沙盘选城异常：{e!r}")
        return self.fac.select(city)

    def _reselect_city(self, city: Camp) -> bool:
        """出征后「调动」面板自动收起时重开：沙盘重跳优先，失败回退列表。"""
        try:
            from .sandbox import SandboxJump
            sb = SandboxJump(self.adb, log=self.log)
            if sb.goto((int(city.coord[0]), int(city.coord[1])), tries=4):
                if self.fac.has_mobilize_button():
                    self.log(f"  ✓ 沙盘重跳回 {city.describe()}")
                    return True
        except Exception as e:  # noqa: BLE001
            self.log(f"  ! 沙盘重选城异常：{e!r}")
        return self.fac.reselect(city)

    def _select_troop(self, mob: Mobilizer, idx: int) -> bool:
        try:
            return mob.select_card(idx)
        except Exception:  # noqa: BLE001 - 选卡失败不应打断整轮
            return False

    def _confirm(self) -> bool:
        img = self.adb.screencap()
        point, text = _find_text_point(img, CONFIRM_HINTS, ("取消",),
                                       boxes=(CONFIRM_BOX,))
        if point is None:
            return False
        self.log(f"  · 确认召回弹窗「{text}」@{point}")
        self.adb.tap(*point)
        time.sleep(1.2)
        return True

    def _recall_troop(self, mob: Mobilizer, entry: RestEntry) -> bool:
        """在城池调动面板里把部队撤回主城。只点明确的撤回类按钮。"""
        if not self._select_troop(mob, entry.troop):
            self.log(f"  ! 部队{entry.troop} 点选失败，保留歇兵状态")
            return False
        img = self.adb.screencap()
        point, text = _find_text_point(img, RECALL_HINTS, RECALL_EXCLUDE)
        if point is None:
            self.log(f"  ! 部队{entry.troop} 没有找到「撤回/撤退」按钮"
                     "（可能该城池不支持召回），保留歇兵状态")
            return False
        self.log(f"== 召回部队{entry.troop}：点击「{text}」@{point}")
        self.adb.tap(*point)
        time.sleep(1.2)
        self._confirm()
        time.sleep(1.6)
        if not mob.panel_open():
            # 撤退成功后面板常会关掉，重新打开再确认状态
            mob.open_from_camp()
        status = mob.read_status(entry.troop)
        self.log(f"  部队{entry.troop} 召回后状态「{status or '未知'}」")
        # 回城/行军 = 已在返回；空闲 = 已经到家
        return any(k in (status or "") for k in ("回城", "行军", "空闲", "出征"))

    def _try_recruit(self, entry: RestEntry) -> bool:
        """在城池调动面板里点一次「征兵」（空兵长期不增长时的手动兜底）。"""
        point, text = _find_text_point(self.adb.screencap(),
                                       RECRUIT_HINTS, RECRUIT_EXCLUDE)
        if point is None:
            return False
        self.log(f"  · 部队{entry.troop} 在歇兵城池点「{text}」征兵 @{point}")
        self.adb.tap(*point)
        time.sleep(1.2)
        self._confirm()
        return True

    def recall_ready(self) -> dict:
        """把已征满的歇兵部队召回主城；返回本轮处理结果。"""
        entries = self.entries()
        result: dict = {"ok": True, "checked": 0, "recalled": [],
                        "pending": [], "dropped": []}
        if not entries:
            result["reason"] = "no-rest-entry"
            return result

        now = time.time()
        # 过旧记录直接丢弃，避免永远卡住
        fresh: list[RestEntry] = []
        for entry in entries:
            if now - (entry.sent_at or now) > STALE_SECONDS:
                result["dropped"].append(entry.as_dict())
                self.log(f"  · 部队{entry.troop} 歇兵超过 {STALE_SECONDS // 3600}h，丢弃记录")
            else:
                fresh.append(entry)
        entries = fresh
        if not entries:
            self._save(entries)
            result["reason"] = "all-stale"
            return result

        by_city: dict[tuple[int, int], list[RestEntry]] = {}
        for entry in entries:
            by_city.setdefault(entry.city_coord, []).append(entry)

        recalled: set[int] = set()
        for coord, group in by_city.items():
            if all(now < (e.arrive_epoch or 0) + ARRIVE_GRACE_SECONDS for e in group):
                self.log(f"  · 歇兵城池 {coord} 的部队还在路上，跳过检查")
                continue
            city = group[0].camp()
            if not self._select_city(city):
                self.log(f"  ! 歇兵城池 {city.describe()} 选不中，下一轮再试")
                continue
            self._mark("选中歇兵城池", "分析", note=city.describe())
            mob = Mobilizer(self.adb, log=self.log,
                            reselect=lambda: self._reselect_city(city),
                            timer=self.timer)
            try:
                if not mob.open_from_camp():
                    self.log("  ! 城池调动面板打不开，下一轮再试")
                    continue
                for entry in group:
                    entry.attempts += 1
                    result["checked"] += 1
                    status = mob.read_status(entry.troop)
                    entry.status = status
                    if "空闲" in (status or ""):
                        # 已经在家：直接清记录
                        recalled.add(entry.troop)
                        self.log(f"  部队{entry.troop} 已在主城（空闲），清记录")
                        continue
                    strength = mob.read_strength(entry.troop)
                    if strength is None:
                        self.log(f"  ! 部队{entry.troop} 读不到兵力，保留歇兵")
                        result["pending"].append(entry.as_dict())
                        continue
                    prev = entry.strength
                    entry.strength = strength
                    if strength < STRENGTH_FULL:
                        grew = prev is not None and strength - int(prev) >= GROWTH_MIN
                        waited = now - (entry.sent_at or now)
                        if not grew and entry.attempts <= RECRUIT_TRIES:
                            # 城池自动征兵没动起来：手动点一次「征兵」再看看
                            self._try_recruit(entry)
                        if grew:
                            self.log(f"  · 部队{entry.troop} 兵力 {strength}/"
                                     f"{STRENGTH_FULL}，仍在征兵（+{strength - int(prev)}）")
                            result["pending"].append(entry.as_dict())
                            continue
                        if waited > STALE_SECONDS:
                            # 最后兜底：城池 6h 都征不满（城被夺 / 该城无法征兵）才召回
                            self.log(f"== 部队{entry.troop} 进城 "
                                     f"{int(waited // 3600)}h 仍未征满"
                                     f"（{strength}/{STRENGTH_FULL}）→ 兜底召回")
                        else:
                            # 规则（用户口径）：不满 33000 就一直留在城里征兵，
                            # 城会自动征兵；征满 33000 才召回主城。
                            self.log(f"  · 部队{entry.troop} 兵力 {strength}/"
                                     f"{STRENGTH_FULL}，继续在城里征兵（满编才回城）")
                            result["pending"].append(entry.as_dict())
                            continue
                    else:
                        self.log(f"== 部队{entry.troop} 已满编"
                                 f"（{strength}/{STRENGTH_FULL}），召回主城")
                    if self._recall_troop(mob, entry):
                        recalled.add(entry.troop)
                        result["recalled"].append(entry.as_dict())
                    else:
                        result["pending"].append(entry.as_dict())
            finally:
                mob.close()
                self.fac.back_to_map()

        entries = [e for e in entries if e.troop not in recalled]
        self._save(entries)
        result["ok"] = True
        result["remaining"] = [e.as_dict() for e in entries]
        self._mark("召回征满部队", "执行",
                   note=f"召回={sorted(recalled)} 剩余={[e.troop for e in entries]}")
        return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="只打印歇兵记录")
    parser.add_argument("--recall", action="store_true", help="检查并召回征满部队")
    parser.add_argument("--clear", action="store_true", help="清空歇兵记录")
    args = parser.parse_args(argv)

    rest = CityRest()
    if args.clear:
        rest.clear()
        print(json.dumps({"ok": True, "cleared": True}, ensure_ascii=False))
        return 0
    if args.recall:
        print(json.dumps(rest.recall_ready(), ensure_ascii=False))
        return 0
    entries = rest.entries()
    print(json.dumps({"ok": True, "count": len(entries),
                      "rests": [e.as_dict() for e in entries]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
