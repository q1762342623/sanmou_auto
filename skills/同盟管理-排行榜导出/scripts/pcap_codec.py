"""pcap 解码小工具（本 skill 自带，不依赖仓库外的 `tools/`）。

链路（与 `skills/战报自动收录/scripts/engine/` 一致）：
    2 字节长度帧 → 9 字节传输头 → XOR 8 字节随机 key → 2 字节 RPC 前缀
    → sproto-like 对象（field_count + 头表 + 长度前缀数据段）

`read_tcp_flows` / `iter_incoming_frames` / `xor_bytes` / `derive_xor_key_candidates`
直接用战报收录引擎里已经验证过的实现，本模块只做「端口猜测 + key 打分 + 帧遍历」。
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))


def project_root(start: str = HERE) -> str:
    """向上找到工程根（含 `sanmou_auto/__init__.py` 的那一层）。

    兼容两种安装位置：
      `<工程根>/skills/<名>/scripts`
      `<工程根>/sanmou_auto/skills/<名>/scripts`   ← skill 随包一起分发时
    """
    cur = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(cur, "sanmou_auto", "__init__.py")):
            return cur
        if (os.path.basename(cur) == "sanmou_auto"
                and os.path.isfile(os.path.join(cur, "__init__.py"))):
            return os.path.dirname(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start)
        cur = parent


ROOT = project_root(HERE)


def engine_dir() -> str:
    """定位「战报自动收录」引擎目录（两种仓库布局都兼容）。"""
    for rel in (("skills", "战报自动收录", "scripts", "engine"),
                ("sanmou_auto", "skills", "战报自动收录", "scripts", "engine")):
        path = os.path.join(ROOT, *rel)
        if os.path.isdir(path):
            return path
    raise SystemExit(
        "找不到 `战报自动收录/scripts/engine`；"
        "本 skill 的解码链复用该引擎，需要同仓存在 skills/战报自动收录")


ENGINE = engine_dir()
for _path in (ENGINE, ROOT, HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import decode_list_card_sproto_like as cards  # noqa: E402
import extract_mumu_pcap_blobs as pc          # noqa: E402

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
PRINTABLE_RE = re.compile(r"^[\u4e00-\u9fff0-9A-Za-z（）()【】\[\]·。，,、：:\-—_/ ]+$")
CJK_RUN_RE = re.compile(rb"(?:[\xe4-\xe9][\x80-\xbf]{2}){2,}")
COORD_MIN, COORD_MAX = 1, 2000


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def decode_frame(frame: bytes, key: bytes | None = None) -> bytes | None:
    """传输帧 → sproto-like 对象字节（不含 2 字节 RPC 前缀）。"""
    key = key or pc.DEFAULT_XOR_KEY
    if len(frame) <= pc.TRANSPORT_HEADER_SIZE + pc.RPC_PREFIX_SIZE:
        return None
    decoded = pc.xor_bytes(frame[pc.TRANSPORT_HEADER_SIZE:], key)
    return decoded[pc.RPC_PREFIX_SIZE:]


def discover_key(frames) -> tuple[bytes, int, list[tuple[bytes, int]]]:
    """XOR key 每次抓包随机，先由帧头推候选，再用「能否解成对象」打分挑选。"""
    candidates = pc.derive_xor_key_candidates(frames)[:12]
    best_key = pc.DEFAULT_XOR_KEY
    best_score = -1
    for key, _support in candidates:
        score = 0
        for frame in frames:
            data = decode_frame(frame.data, key)
            if data and cards.try_parse_object(data, 0, len(data)) is not None:
                score += 1
        if score > best_score:
            best_key, best_score = key, score
    return best_key, max(0, best_score), candidates


def _is_local(ip: str) -> bool:
    return (ip.startswith("10.") or ip.startswith("192.168.")
            or ip.startswith("127.") or ip.startswith("172.1")
            or ip.startswith("172.2") or ip.startswith("172.3")
            or ip.startswith("fe80") or ip.startswith("fd") or ip == "::1")


def guess_ports(pcap_path: str) -> list[int]:
    """按流量猜「游戏服务端口」（实测出现过 20040，不能写死默认端口）。"""
    flows = pc.read_tcp_flows(pc.Path(pcap_path))
    scores: Counter[int] = Counter()
    for (src, dst), payloads in flows.items():
        size = sum(len(p.data) for p in payloads)
        if _is_local(src[0]) and not _is_local(dst[0]):
            scores[dst[1]] += size
        elif _is_local(dst[0]) and not _is_local(src[0]):
            scores[src[1]] += size
    return [port for port, _ in scores.most_common(4)]


def iter_frames(pcap_path: str, ports: list[int] | None = None):
    """只取「服务端 → 客户端」方向。"""
    game_ports = set(ports or guess_ports(pcap_path) or pc.DEFAULT_GAME_PORTS)
    frames, skipped = pc.iter_incoming_frames(pc.Path(pcap_path), game_ports)
    return frames, skipped


# --------------------------------------------------------------------------- #
# 结构与原始字节
# --------------------------------------------------------------------------- #
def coord_fields(data: bytes, fields) -> list[tuple[int, int, int]]:
    """坐标字段实测编码：长度 9 = 1 字节前缀 + u32 x + u32 y。"""
    out: list[tuple[int, int, int]] = []
    for f in fields:
        if f.kind != "data" or f.start is None or f.size is None:
            continue
        if f.size == 9:
            x = cards.read_u32(data, f.start + 1)
            y = cards.read_u32(data, f.start + 5)
        elif f.size in (8, 12):
            x = cards.read_u32(data, f.start)
            y = cards.read_u32(data, f.start + 4)
        else:
            continue
        if COORD_MIN <= x <= COORD_MAX and COORD_MIN <= y <= COORD_MAX:
            out.append((f.tag, x, y))
    return out


def walk_object(data: bytes, base: int, size: int, depth: int = 0,
                out: dict | None = None, max_depth: int = 8) -> dict:
    """递归展开 sproto-like 对象/数组，收集 (coords, ints, strings, children)。"""
    out = out or {"coords": [], "ints": [], "strings": [],
                  "children": [], "depth": depth}
    if depth > max_depth:
        return out
    fields = cards.try_parse_object(data, base, size)
    if fields is None:
        return out
    for tag, x, y in coord_fields(data, fields):
        out["coords"].append({"tag": tag, "x": x, "y": y, "depth": depth})
    for f in fields:
        if f.kind == "small":
            out["ints"].append(int(f.value))
            continue
        if f.start is None or f.size is None:
            continue
        text = cards.decode_utf8(data, f.start, f.size)
        if text and PRINTABLE_RE.match(text.strip()):
            clean = text.strip()
            if CJK_RE.search(clean) or len(clean) >= 2:
                out["strings"].append(clean)
        sub = walk_object(data, f.start, f.size, depth + 1, None, max_depth)
        if sub is not None and (sub["coords"] or sub["ints"] or sub["strings"]
                                or sub["children"]):
            out["children"].append({"tag": f.tag, "kind": "object", **sub})
            continue
        items = cards.try_parse_array(data, f.start, f.size)
        if items:
            node = {"tag": f.tag, "kind": "array", "count": len(items),
                    "coords": [], "ints": [], "strings": [],
                    "children": [], "depth": depth}
            for idx, (ib, isz, _fields) in enumerate(items):
                child = walk_object(data, ib, isz, depth + 1, None, max_depth)
                if child is None:
                    continue
                node["coords"] += child["coords"]
                node["ints"] += child["ints"]
                node["strings"] += child["strings"]
                child["index"] = idx
                node["children"].append(child)
            out["children"].append(node)
    return out


def utf8_runs(data: bytes) -> list[str]:
    """不依赖 sproto 结构，直接从原始字节里抓连续中文串。"""
    out = []
    for match in CJK_RUN_RE.finditer(data):
        try:
            out.append(match.group(0).decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return out
