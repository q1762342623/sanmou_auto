"""收集「我的主城」坐标（与「城池」是两个概念，别混用）。

来源（实测 2026-11-23，竖屏 900x1600）：
    点地图右上角「定位」箭头 → 面板页签「建筑」→ **列表第一条就是我的主城**
    行样式：`<玩家名>`（第一行）+ `<地块名>(x,y)`（第二行）
    点这一行后底栏地块信息会显示 `鄱阳-视田-乘风台(648,915) 整编(6)`，
    其中的 `(x,y)` 就是主城所在格坐标，可用来校验 OCR 读到的数字。

注意：
- 主城 = 自己的城（第一行，名字是玩家名）；
- 城池 = 同盟「城池」页签里的城/水寨/军港/民港（那是据点，不是主城）。
  两者分开存：主城 -> `screenshots/flow/home_city.json`，
  据点 -> `screenshots/flow/camp_cache.json`。

用法：
    python -m sanmou_auto.homecity                 # 读一次并打印
    python -m sanmou_auto.homecity --verify        # 读 + 点第一行用底栏校验
    python -m sanmou_auto.homecity --store         # 读并写入 home_city.json

代码调用：
    from sanmou_auto.homecity import HomeCity
    hc = HomeCity(adb)
    info = hc.collect(verify=True)      # {"name":..., "tile":..., "coord":(x,y)}
    info = hc.load_or_read(max_age=3600)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import config, fastwait, ocrengine, vision
from .adb import Adb
from .mapnav import COORD_RE, MapNav

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "screenshots" / "flow" / "home_city.json"
# 点开主城后底栏/浮层里的地块信息条（竖屏实测两处都可能出现）
TILE_INFO_BOXES = ((0, 830, 900, 985), (0, 1370, 900, 1490))
TILE_COORD_RE = re.compile(r"[(（]\s*(\d{1,4})\s*[,，]\s*(\d{1,4})\s*[)）]")


class HomeCity:
    """读「定位 → 建筑」第一条 = 我的主城。"""

    def __init__(self, adb: Adb | None = None, log=print, nav: MapNav | None = None):
        self.adb = adb or Adb()
        self.nav = nav or MapNav(self.adb, log=log)
        self.log = log

    # -- 读取 -------------------------------------------------------------
    def read(self, tries: int = 2) -> dict | None:
        """读定位面板第一条：返回 {"name","tile","coord","point"} 或 None。"""
        rows = []
        img = None
        for _ in range(max(1, tries)):
            self.nav.ensure_map()
            self.nav.open_locate()
            try:
                self.nav.select_locate_tab("建筑")
                fastwait.wait_settle(self.adb, box=config.LOCATE_PANEL_BOX,
                                     timeout=1.6, min_wait=0.35)
                img = self.adb.screencap()
                rows = self.nav._parse_locates(img)
            finally:
                self.nav.close_locate()
            if rows:
                break
            self.log("  · 定位面板「建筑」没解析到条目，重试一次")
        if not rows or img is None:
            self.log("  ! 定位面板「建筑」里没有条目（主城读不到）")
            return None
        try:
            first = rows[0]
            tile = ""
            for row in ocrengine.ocr_rows(
                    vision.crop(img, config.LOCATE_PANEL_BOX),
                    config.LOCATE_OCR_SCALE):
                text = "".join(s.text for s in row).strip()
                if "坐标" in text:
                    continue
                m = COORD_RE.search(text)
                if m and (int(m.group(1)), int(m.group(2))) == first.coord:
                    tile = COORD_RE.sub("", text).strip(" ()（），,")
                    break
            return {"name": first.name, "tile": tile, "coord": first.coord,
                    "point": first.point}
        finally:
            self.nav.close_locate()

    def collect(self, verify: bool = True) -> dict | None:
        """读主城；`verify=True` 时点一下第一条，用底栏坐标校验 OCR 数字。"""
        info = self.read()
        if not info:
            return None
        coord = info["coord"]
        if verify:
            checked = self._verify_by_click(info)
            if checked and checked != coord:
                self.log(f"  ! 面板坐标 {coord} 与底栏 {checked} 不一致，采用底栏")
                info["panel_coord"] = list(coord)
                info["coord"] = checked
        info["coordText"] = f"({info['coord'][0]},{info['coord'][1]})"
        info["collectedAt"] = time.time()
        info["collectedAtIso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log(f"== 我的主城：{info['name']}"
                 f"{('·' + info['tile']) if info['tile'] else ''}"
                 f"{info['coordText']}")
        return info

    def _verify_by_click(self, info: dict) -> tuple[int, int] | None:
        """点第一条 → 底栏地块信息里的 (x,y)；读不到返回 None。"""
        try:
            self.nav.open_locate()
            self.nav.select_locate_tab("建筑")
            fastwait.wait_settle(self.adb, box=config.LOCATE_PANEL_BOX,
                                 timeout=1.6, min_wait=0.35)
            rows = self.nav._parse_locates(self.adb.screencap())
            if not rows:
                return None
            self.adb.tap(*rows[0].point)
            fastwait.wait_settle(self.adb, box=TILE_INFO_BOXES[0],
                                 timeout=2.4, min_wait=0.5)
            got = self.read_tile_coord()
            self.log(f"  · 底栏校验坐标：{got if got else '未读到'}")
            return got
        except Exception as e:                  # noqa: BLE001
            self.log(f"  ! 主城坐标校验失败：{e!r}")
            return None
        finally:
            try:
                self.nav.ensure_map()
            except Exception:                   # noqa: BLE001
                pass

    def read_tile_coord(self) -> tuple[int, int] | None:
        """读当前选中地块信息里的 (x,y)（底栏 / 浮层两处都试）。"""
        img = self.adb.screencap()
        for box in TILE_INFO_BOXES:
            text = ocrengine.ocr_text(vision.crop(img, box), 2.0)
            m = TILE_COORD_RE.search(text.replace(" ", ""))
            if m:
                try:
                    return int(m.group(1)), int(m.group(2))
                except ValueError:
                    continue
        return None

    # -- 缓存 -------------------------------------------------------------
    @staticmethod
    def load() -> dict | None:
        try:
            payload = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        coord = payload.get("coord") or []
        if len(coord) != 2:
            return None
        try:
            payload["coord"] = (int(coord[0]), int(coord[1]))
        except (TypeError, ValueError):
            return None
        return payload

    def load_or_read(self, max_age: float = 3600.0,
                     verify: bool = False) -> dict | None:
        """缓存未过期就直接用（迁城很罕见），否则读一次并落盘。"""
        cached = self.load()
        if cached:
            age = time.time() - float(cached.get("collectedAt") or 0)
            if 0 <= age <= max_age:
                return cached
        info = self.collect(verify=verify)
        if info:
            self.save(info)
        return info

    @staticmethod
    def save(info: dict) -> Path:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(info)
        coord = payload.get("coord")
        if isinstance(coord, (tuple, list)):
            payload["coord"] = [int(coord[0]), int(coord[1])]
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(STATE_PATH)
        return STATE_PATH

    def distance_to(self, coord: tuple[int, int],
                    max_age: float = 3600.0) -> float | None:
        """我的主城到某坐标的直线距离（格）；没有主城信息返回 None。"""
        info = self.load_or_read(max_age=max_age)
        if not info:
            return None
        hx, hy = info["coord"]
        return ((hx - coord[0]) ** 2 + (hy - coord[1]) ** 2) ** 0.5


def main(argv: list[str] | None = None) -> int:
    import argparse

    from . import cli

    cli.setup_console()
    ap = argparse.ArgumentParser(description="收集我的主城坐标（定位→建筑第一条）")
    ap.add_argument("--verify", action="store_true",
                    help="点开第一条，用底栏地块坐标校验 OCR 数字")
    ap.add_argument("--store", action="store_true",
                    help=f"写入 {STATE_PATH.relative_to(ROOT)}")
    ap.add_argument("--max-age", type=float, default=0.0,
                    help="缓存有效期秒数（默认 0 = 强制重读）")
    args = ap.parse_args(argv)

    hc = HomeCity()
    if args.max_age > 0:
        info = hc.load_or_read(max_age=args.max_age, verify=args.verify)
    else:
        info = hc.collect(verify=args.verify)
        if info and args.store:
            print("== 已写入", HomeCity.save(info))
    if not info:
        print("× 没读到主城坐标")
        return 1
    print(f"主城：{info.get('name')}"
          f"（{info.get('tile') or '-'}）{info.get('coordText')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
