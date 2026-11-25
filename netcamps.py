"""从 tcpdump 抓包直接解出「我方据点（同盟建筑）」坐标，免去扫表 OCR。

实测结论（2026-11-23，竖屏 900x1600，MuMu + 设备内 root tcpdump）：
- 打开「同盟」主页面时，服务端一次响应里就同时带「同盟成员」和「同盟建筑」：
    建筑条目：tag1(u32) = x*10000 + y   （如 6370770 → (637,770)）
              tag3/tag10(utf8) = 建筑名   （同盟大营 / 同盟大营2 / …）
              tag0(small) = 类型          （5002 = 同盟大营）
- 4 个同盟大营 + 1 个器械/工事全部一次拿全，和 OCR 扫表的结果逐一吻合
  （637,770 / 700,809 / 814,791 / 838,780）。
- 「城池列表」在抓包里**没有**观察到对应响应（客户端会缓存，登录期一次性下发），
  所以城池仍走 OCR；本模块只负责同盟建筑。

解码链与 `skills/战报自动收录` 完全一致：
    2 字节帧长 → 9 字节传输头 → 每次抓包随机 8 字节 XOR key → 2 字节 RPC 前缀
    → sproto-like 对象（field_count + 头表 + 长度前缀数据段）
XOR key 每次抓包都不同，这里用 `derive_xor_key_candidates` 出候选，
再用「能否解成 sproto-like 对象」打分自动挑，不依赖任何写死的 key。

用法：
    from sanmou_auto.netcamps import capture_camps, camps_from_pcap
    camps = camps_from_pcap("x.pcap")            # 离线解析
    camps = capture_camps(adb)                   # 抓 6s + 解析（点了「同盟」）
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import config
from .alliance import Camp

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "skills" / "战报自动收录" / "scripts" / "engine"
RPC_PREFIX = 2
TRANSPORT_HEADER = 9
DEFAULT_PORTS = (20010, 20020, 20040, 20050, 20060, 20070, 20080, 20090, 20100)
# 建筑名里常见的字（用来挑「这一条是据点」）
CAMP_NAME_HINTS = ("营", "寨", "港", "城", "仓", "台", "垒", "屯", "关", "隘")


def _engine():
    """惰性导入战报收录的解码引擎（缺 dpkt 时返回 None）。"""
    import sys

    for path in (str(ENGINE), str(ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import decode_list_card_sproto_like as cards   # noqa: F401
        import extract_mumu_pcap_blobs as pcx          # noqa: F401
    except Exception:                                  # noqa: BLE001
        return None
    return cards, pcx


def _is_local(ip: str) -> bool:
    return (ip.startswith("10.") or ip.startswith("192.168.")
            or ip.startswith("127.") or ip.startswith("172.1")
            or ip.startswith("172.2") or ip.startswith("172.3")
            or ip.startswith("fe80") or ip.startswith("fd") or ip == "::1")


def guess_ports(pcx, pcap_path: str) -> list[int]:
    """按流量猜服务端端口（实测 20040），避免写死端口漏抓。"""
    flows = pcx.read_tcp_flows(Path(pcap_path))
    scores: dict[int, int] = {}
    for (src, dst), payloads in flows.items():
        size = sum(len(p.data) for p in payloads)
        if _is_local(src[0]) and not _is_local(dst[0]):
            scores[dst[1]] = scores.get(dst[1], 0) + size
        elif _is_local(dst[0]) and not _is_local(src[0]):
            scores[src[1]] = scores.get(src[1], 0) + size
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [port for port, _ in ordered[:4]]


def _decode(frame_data: bytes, key: bytes, pcx) -> bytes:
    decoded = pcx.xor_bytes(frame_data[TRANSPORT_HEADER:], key)
    return decoded[RPC_PREFIX:]


def _pick_key(frames, cards, pcx) -> bytes:
    best_key, best = pcx.DEFAULT_XOR_KEY, -1
    for key, _support in pcx.derive_xor_key_candidates(frames)[:12]:
        score = 0
        for frame in frames:
            data = _decode(frame.data, key, pcx)
            if data and cards.try_parse_object(data, 0, len(data)) is not None:
                score += 1
        if score > best:
            best_key, best = key, score
    return best_key


def _packed_coord(value: int) -> tuple[int, int] | None:
    x, y = divmod(int(value), 10000)
    if 1 <= x <= 2000 and 1 <= y <= 2000:
        return x, y
    return None


def _walk(data: bytes, base: int, size: int, cards, depth: int = 0,
          out: list | None = None) -> list[dict]:
    """扁平收集所有「对象级」条目：{tag→值}，含 name / coord。"""
    out = [] if out is None else out
    if depth > 8:
        return out
    fields = cards.try_parse_object(data, base, size)
    if fields is None:
        return out
    names: list[str] = []
    coords: list[tuple[int, int]] = []
    smalls: list[int] = []
    long_text = False
    for f in fields:
        if f.kind == "small":
            smalls.append(int(f.value))
            continue
        if f.start is None or f.size is None:
            continue
        if f.size == 4:
            coord = _packed_coord(cards.read_u32(data, f.start))
            if coord:
                coords.append(coord)
        text = cards.decode_utf8(data, f.start, f.size)
        if text:
            if len(text) > 16:
                long_text = True           # 聊天/邮件正文：整条对象不可信
            elif 2 <= len(text) <= 12 and any(h in text for h in CAMP_NAME_HINTS):
                names.append(text)
        sub = cards.try_parse_object(data, f.start, f.size)
        if sub is not None:
            _walk(data, f.start, f.size, cards, depth + 1, out)
            continue
        items = cards.try_parse_array(data, f.start, f.size)
        if items:
            for ib, isz, _fl in items:
                _walk(data, ib, isz, cards, depth + 1, out)
    # 只有「短名 + 打包坐标 + 类型 id(5002/5020… 这类 4~6k 小整数)」才算据点条目，
    # 否则会把世界频道里的「民港(620,747)」之类聊天文本当成坐标。
    has_type = any(4000 <= v <= 6100 for v in smalls)
    if names and coords and has_type and not long_text:
        out.append({"name": max(names, key=len), "coord": coords[0]})
    return out


def camps_from_pcap(pcap_path: str) -> list[Camp]:
    """解析 pcap → 同盟建筑 Camp 列表（按坐标去重）。"""
    engine = _engine()
    if engine is None:
        return []
    cards, pcx = engine
    try:
        paths = [pcap_path] if pcap_path else []
        if not paths or not os.path.isfile(pcap_path):
            return []
        ports = guess_ports(pcx, pcap_path) or list(DEFAULT_PORTS)
        frames, _skipped = pcx.iter_incoming_frames(Path(pcap_path), set(ports))
        if not frames:
            return []
        key = _pick_key(frames, cards, pcx)
        found: dict[tuple[int, int], Camp] = {}
        for frame in frames:
            data = _decode(frame.data, key, pcx)
            if not data:
                continue
            for item in _walk(data, 0, len(data), cards):
                coord = item["coord"]
                if coord in found:
                    continue
                found[coord] = Camp(name=item["name"], coord=coord,
                                    point=(0, 0), source="union-build",
                                    kind=_kind_of(item["name"]))
    except Exception:                        # noqa: BLE001 - 解析失败当作没有
        return []
    return list(found.values())


def _kind_of(name: str) -> str:
    if "大营" in name:
        return "同盟大营"
    for kind in ("水寨", "军港", "民港", "堡垒", "营寨", "城池"):
        if kind in name:
            return kind
    return "同盟建筑"


# --------------------------------------------------------------------------- #
# 真机抓包
# --------------------------------------------------------------------------- #
def _discover_ports(adb) -> list[int]:
    for command in (("ss", "-tnp"), ("netstat", "-tnp")):
        try:
            out = adb.shell(*command) or ""
        except Exception:                   # noqa: BLE001
            continue
        ports: list[int] = []
        for line in out.splitlines():
            if "nslg" not in line:
                continue
            tokens = [t for t in line.split() if ":" in t]
            if len(tokens) < 2:
                continue
            peer = tokens[1].replace("[", "").replace("]", "")
            _ip, _, port = peer.rpartition(":")
            if port.isdigit() and 10000 <= int(port) <= 30000:
                ports.append(int(port))
        if ports:
            return sorted(set(ports))
    return []


def capture_camps(adb=None, seconds: float = 6.0, log=print,
                  out_dir: str | None = None) -> list[Camp]:
    """抓一段「同盟主页面」的流量并解析出同盟建筑。

    前置：`adb root` 可用、设备内有 tcpdump（实测 /system/bin/tcpdump）。
    流程：起 tcpdump → 打开「同盟」主页面（已有页面会被刷新）→ 停抓 → 拉回解析。
    """
    from .adb import Adb

    adb = adb or Adb()
    ports = _discover_ports(adb) or list(DEFAULT_PORTS)
    tag = time.strftime("%Y%m%d_%H%M%S")
    remote = f"/data/local/tmp/netcamps_{tag}.pcap"
    filt = " or ".join(f"tcp port {p}" for p in ports)
    cmd = f"tcpdump -i any -s 0 -U -w {remote} '{filt}' > /dev/null 2>&1 & echo $!"
    try:
        pid = (adb.shell(cmd) or "").strip().splitlines()[-1:]
    except Exception as exc:                # noqa: BLE001
        log(f"  ! tcpdump 启动失败：{exc}")
        return []
    pid = pid[0].strip() if pid else ""
    if not pid.isdigit():
        log("  ! tcpdump 启动失败（需要 adb root 与设备内 tcpdump）")
        return []
    local = os.path.join(out_dir or
                         os.path.join(config.SHOT_DIR, "pcap"), f"{tag}.pcap")
    pulled = False
    from .alliance import AllianceFacilities

    fac = AllianceFacilities(adb, log=log)
    try:
        fac._page = "map"
        fac.open_alliance_home()            # 触发「同盟」主页面响应
        time.sleep(max(1.0, seconds))
    finally:
        try:
            adb.shell("kill", "-2", pid)
        except Exception:                   # noqa: BLE001
            pass
        time.sleep(1.0)
        # 抓完把界面送回地图，避免把自动拉线留在「同盟」页
        try:
            fac.back_to_map()
            fac.nav.ensure_map()
        except Exception:                   # noqa: BLE001
            pass
        try:
            os.makedirs(os.path.dirname(local), exist_ok=True)
            adb._run("pull", remote, local)
            pulled = os.path.isfile(local)
        except Exception as exc:            # noqa: BLE001
            log(f"  ! pcap 拉回失败：{exc}")
        try:
            adb.shell("rm", "-f", remote)
        except Exception:                   # noqa: BLE001
            pass
    if not pulled:
        return []
    camps = camps_from_pcap(local)
    log(f"== 抓包解析：{len(camps)} 个同盟建筑（{local}）")
    return camps


def sync_cache(camps: list[Camp], battlefield: tuple[int, int] | None = None,
               keep_cities: bool = True) -> int:
    """把抓包得到的同盟建筑并入 `camp_cache.json`（默认保留原有城池记录）。"""
    from . import camp_cache

    old, _meta = camp_cache.load_camps()
    merged: dict[tuple[int, int], Camp] = {}
    if keep_cities:
        for c in old:
            if c.source == "city":
                merged[(int(c.coord[0]), int(c.coord[1]))] = c
    for c in camps:
        merged[(int(c.coord[0]), int(c.coord[1]))] = c
    camp_cache.save_camps(list(merged.values()), battlefield=battlefield)
    return len(merged)


def main(argv: list[str] | None = None) -> int:
    """CLI：离线解析 / 真机抓包 / 写入据点缓存。

    python -m sanmou_auto.netcamps --pcap x.pcap        # 只解析
    python -m sanmou_auto.netcamps --capture --sync     # 抓 6s 并写缓存
    """
    import argparse

    from . import cli

    cli.setup_console()
    ap = argparse.ArgumentParser(description="从 tcpdump 解出同盟建筑（我方据点）坐标")
    ap.add_argument("--pcap", default="", help="已有 pcap 路径（离线解析）")
    ap.add_argument("--capture", action="store_true", help="真机抓一次（需 adb root）")
    ap.add_argument("--seconds", type=float, default=6.0, help="抓包时长（默认 6s）")
    ap.add_argument("--sync", action="store_true",
                    help="写回 screenshots/flow/camp_cache.json（保留原城池记录）")
    args = ap.parse_args(argv)

    camps: list[Camp] = []
    if args.pcap:
        camps = camps_from_pcap(args.pcap)
    elif args.capture:
        camps = capture_camps(seconds=args.seconds)
    else:
        ap.print_help()
        return 1
    if not camps:
        print("× 没有解析到同盟建筑（端口/XOR key/是否真的打开过同盟页？）")
        return 1
    print(f"== 同盟建筑 {len(camps)} 个：")
    for c in camps:
        print(f"   {c.describe()}")
    if args.sync:
        total = sync_cache(camps)
        print(f"== 已写入 camp_cache.json（合计 {total} 条，含原有城池）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["camps_from_pcap", "capture_camps", "sync_cache", "guess_ports"]
