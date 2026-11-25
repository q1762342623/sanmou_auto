"""把排行抓包结果生成**单文件静态 HTML**报表。

功能
----
1. 个人繁荣榜 / 个人武勋榜：按同盟汇聚（上榜人数、总分、平均、最高/最低），**各 300 人**；
2. 个人首占榜：**只做分类**（同盟 / 上榜人数 / 占比）+ 表格上方标出第 100 名的占领时间；
3. 阵营：**每个阵营一张表**。按「阵营内排行名次」分配名额：
   前 X 名 = 问鼎；紧接着的 Y 名（第 X+1 ~ X+Y 名）= 霸业；其余未获得。
   表内逐盟统计问鼎人数 / 问鼎占比 / 霸业人数 / 霸业占比 / 问鼎+霸业 / 合计占比。

数据来源：`screenshots/flow/rank_probe/rank_<批次>_*.pcap`（`scripts/probe.py` 抓的）。

用法
----
    python scripts/report.py                     # 最新批次
    python scripts/report.py --batch 20261126_003448
    python scripts/report.py --level 9 --out reports/rank.html
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcap_codec import ROOT, decode_frame                       # noqa: E402
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from probe import (CAMP_NAMES, OUT_DIR, decode_with_opcode,    # noqa: E402
                   discover_key, iter_frames, scan_rows)

BATCH_RE = re.compile(r"^rank_(\d{8}_\d{6})_(.+)\.pcap$")
MERIT_MIN = 1_000_000          # 武勋值量级（繁荣度目前 6 位数）
TIMESTAMP_MIN = 1_600_000_000  # 首占榜列是「占领时间」的 unix 秒
UNION_NAME_RE = re.compile(rb"unionName\x00([^\x00]{1,64})\x00")
# 各榜人数是固定的：繁荣/武勋每页 50 行 × 6 页 = 300，首占榜每页 50 × 2 = 100
EXPECTED_ROWS = {"prosperity": 300, "merit": 300, "first_occ": 100}


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #
def list_batch(batch: str | None = None) -> tuple[str, list[tuple[str, str]]]:
    """返回 (批次号, [(榜单slug, pcap路径)])。不指定批次时取 mtime 最新的一批。"""
    entries: list[tuple[str, str, str]] = []
    if not os.path.isdir(OUT_DIR):
        raise SystemExit(f"目录不存在：{OUT_DIR}")
    for name in os.listdir(OUT_DIR):
        match = BATCH_RE.match(name)
        if match:
            entries.append((match.group(1), match.group(2),
                            os.path.join(OUT_DIR, name)))
    if not entries:
        raise SystemExit(f"{OUT_DIR} 里没有 rank_*.pcap，先跑 scripts/probe.py")
    if not batch:
        batch = max(entries, key=lambda e: os.path.getmtime(e[2]))[0]
    picked = [(slug, path) for ts, slug, path in entries if ts == batch]
    picked.sort(key=lambda item: item[1])
    return batch, picked


def classify(slug: str) -> str:
    if "繁荣" in slug:
        return "prosperity"
    if "武勋" in slug:
        return "merit"
    if "首占" in slug:
        return "first_occ"
    # 阵营内排行（文件名形如 rank_<批次>_x01讨逆军.pcap）
    if slug.startswith("x01") or "阵营内" in slug:
        for camp in CAMP_NAMES:
            if camp in slug:
                return f"camp_rank:{camp}"
        return "camp_rank"
    if "阵营" in slug:
        return "camp"
    return "other"


def keep(kind: str, score: int) -> bool:
    if kind == "prosperity":
        return 1000 <= score < MERIT_MIN
    if kind == "merit":
        return MERIT_MIN <= score < TIMESTAMP_MIN
    if kind == "first_occ":
        return score >= TIMESTAMP_MIN
    return False


def extract_unions_ordered(data: bytes) -> list[str]:
    """按出现顺序取每一行的同盟名（一行一个 unionName）= 阵营内名次序列。"""
    names: list[str] = []
    for match in UNION_NAME_RE.finditer(data):
        try:
            name = match.group(1).decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if name:
            names.append(name)
    return names


def last_batch_ordered(frame_rows: list[tuple[str, list[str]]]) -> list[str]:
    """一段 pcap 里其实有**两个阵营**的数据（窗口内先切另一阵营再切回来）。

    同一批请求的帧内容互不相同；重复帧（内容哈希相同）代表下一批开始。
    按「签名重复」切批后只取**最后一批** = 目标阵营，并保持名次顺序。
    """
    batches: list[list[str]] = []
    current: list[str] = []
    seen: set[str] = set()
    for digest, names in frame_rows:
        if digest in seen:
            if current:
                batches.append(current)
            current = []
            seen = set()
        seen.add(digest)
        current.extend(names)
    if current:
        batches.append(current)
    return batches[-1] if batches else []


def load_rows(batch: str) -> tuple[dict[str, list[dict]], dict]:
    """解码整批 pcap，返回 {分类: [record...]} 与元信息（含阵营名次序列）。

    行数据走 `scan_rows`（sproto 数组解析）：每页固定 50 行，繁荣/武勋 6 页 =
    **完整 300 人**，首占榜 2 页 = **完整 100 人**（旧的行标记切法会漏 1~2 行/页）。
    同一页可能因重复请求被抓多次，按帧内容的 md5 去重后再计数/入库。
    """
    _batch, files = list_batch(batch)
    loaded = []
    all_frames = []
    for slug, path in files:
        frames, _skipped = iter_frames(path)
        loaded.append((slug, path, frames))
        all_frames.extend(frames)
    if not all_frames:
        raise SystemExit("这批 pcap 里没有可解码帧")
    key, decoded, _cands = discover_key(all_frames)

    buckets: dict[str, list[dict]] = defaultdict(list)
    contributed: dict[str, list[int]] = defaultdict(list)   # 每帧贡献的行数
    seen_digest: dict[str, set[str]] = defaultdict(set)
    camp_orders: dict[str, list[str]] = {}
    meta = {"key": key.hex(), "decodedFrames": decoded,
            "totalFrames": len(all_frames), "files": []}

    for slug, path, frames in loaded:
        kind = classify(slug)
        before = len(buckets.get(kind, []))
        row_frames = 0
        keep_all = kind.startswith("camp_rank")
        frame_rows: list[tuple[str, list[str]]] = []
        for frame in frames:
            if len(frame.data) < 3000:      # 小帧没有榜单行
                continue
            _opcode, data = decode_with_opcode(frame, key)
            if not data:
                continue
            digest = hashlib.md5(data).hexdigest()
            if keep_all:
                # 阵营榜要靠「重复帧」切批次，**不能**在这里按 md5 丢帧
                frame_rows.append((digest, extract_unions_ordered(data)))
            elif digest in seen_digest[kind]:
                continue                    # 同一页的重复请求，跳过避免重复计数
            else:
                seen_digest[kind].add(digest)
            records = scan_rows(data)
            if not records:
                continue
            row_frames += 1
            kept = 0
            for rec in records:
                if keep_all or keep(kind, rec["score"]):
                    buckets[kind].append(rec)
                    kept += 1
            if kept:
                contributed[kind].append(kept)
        if keep_all:
            camp = kind.split(":", 1)[1] if ":" in kind else "未知"
            camp_orders[camp] = last_batch_ordered(frame_rows)
        meta["files"].append({
            "slug": slug, "kind": kind, "frames": len(frames),
            "rowFrames": row_frames,
            "rows": len(buckets.get(kind, [])) - before,
        })

    meta["perFrame"] = {k: (max(v), len(v)) for k, v in contributed.items() if v}
    meta["campOrders"] = camp_orders
    return buckets, meta


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def dedupe(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for rec in rows:
        if not rec.get("union"):
            continue
        key = (rec["score"], rec["a"], rec["b"], rec["union"])
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def fmt_time(value) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return "-"
    if ts < TIMESTAMP_MIN:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def agg_by_union(rows: list[dict], is_time: bool = False) -> list[dict]:
    groups: dict[str, dict] = defaultdict(
        lambda: {"union": "", "count": 0, "total": 0, "max": 0, "min": 0})
    for rec in rows:
        group = groups[rec["union"]]
        group["union"] = rec["union"]
        group["count"] += 1
        group["total"] += rec["score"]
        group["max"] = max(group["max"], rec["score"])
        group["min"] = (rec["score"] if not group["min"]
                        else min(group["min"], rec["score"]))
    out = list(groups.values())
    for group in out:
        group["avg"] = (round(group["total"] / group["count"], 1)
                        if group["count"] else 0)
    if is_time:
        # 时间戳相加没有意义：按「最后一次占领时间」从新到旧排，合计列留空
        out.sort(key=lambda g: g["max"], reverse=True)
        for group in out:
            group["avg"] = fmt_time(group["avg"])
            group["max"] = fmt_time(group["max"])
            group["min"] = fmt_time(group["min"])
    else:
        out.sort(key=lambda g: g["total"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# 片段
# --------------------------------------------------------------------------- #
def table_union(rows: list[dict], value_title: str, is_time: bool = False) -> str:
    if not rows:
        return '<p class="empty">本批次没有解析到该榜数据</p>'
    if is_time:
        head = ("<tr><th>#</th><th>同盟</th><th class='num'>上榜人数</th>"
                "<th class='num'>最后占领时间</th>"
                "<th class='num'>平均占领时间</th>"
                "<th class='num'>最早占领时间</th></tr>")
    else:
        head = (f"<tr><th>#</th><th>同盟</th><th class='num'>上榜人数</th>"
                f"<th class='num'>{value_title}合计</th>"
                f"<th class='num'>平均</th>"
                f"<th class='num'>最高</th><th class='num'>最低</th></tr>")
    body = []
    for idx, row in enumerate(rows, 1):
        cells = [f"<td>{idx}</td>",
                 f"<td class='u'>{html.escape(row['union'])}</td>",
                 f"<td class='n'>{row['count']}</td>"]
        if is_time:
            cells += [f"<td class='n'>{row['max']}</td>",
                      f"<td class='n'>{row['avg']}</td>",
                      f"<td class='n'>{row['min']}</td>"]
        else:
            cells += [f"<td class='n'>{row['total']:,}</td>",
                      f"<td class='n'>{row['avg']}</td>",
                      f"<td class='n'>{row['max']:,}</td>",
                      f"<td class='n'>{row['min']:,}</td>"]
        body.append("<tr>" + "".join(cells) + "</tr>")

    count = sum(row["count"] for row in rows)
    if is_time:
        total_cell = f"<td class='n'>{count}</td><td colspan='3'></td>"
    else:
        total = sum(row["total"] for row in rows)
        total_cell = (f"<td class='n'>{count}</td><td class='n'>{total:,}</td>"
                      f"<td colspan='3'></td>")
    return (f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody>"
            f"<tfoot><tr><td></td><td>合计</td>{total_cell}</tr></tfoot></table>")


def table_firstocc(rows: list[dict], rank_total: int, last_time: str) -> str:
    """首占榜只要「按同盟分类」，外加标注最后一位（第 N 名）的占领时间。"""
    note = (f"<p class='camp-note' style='margin:0 0 10px'>"
            f"最后一位（第 {rank_total} 名）占领时间：<b>{last_time}</b></p>")
    if not rows:
        return note + '<p class="empty">本批次没有解析到首占榜数据</p>'
    total = sum(row["count"] for row in rows)
    head = ("<tr><th>#</th><th>同盟</th><th class='num'>上榜人数</th>"
            "<th class='num'>占比</th></tr>")
    body = []
    for idx, row in enumerate(rows, 1):
        share = (row["count"] * 100 / total) if total else 0
        body.append(
            "<tr>"
            f"<td>{idx}</td><td class='u'>{html.escape(row['union'])}</td>"
            f"<td class='n'>{row['count']}</td>"
            f"<td class='n'>{share:.2f}%</td></tr>")
    return (note + f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody>"
            f"<tfoot><tr><td></td><td>合计</td>"
            f"<td class='n'>{total}</td><td></td></tr></tfoot></table>")


def camp_section(camp_orders: dict[str, list[str]]) -> str:
    """每个阵营一张表：按名次区间分配问鼎 / 霸业。"""
    blocks = []
    for name in CAMP_NAMES:
        order = camp_orders.get(name) or []
        title = html.escape(name)
        if not order:
            blocks.append(
                f"<h3 class='camp-title'>{title}</h3>"
                "<p class='empty'>本批次没有该阵营的「阵营内排行」数据"
                "（先跑一次 <code>--extra</code>）</p>")
            continue

        counts = Counter(order)
        total = len(order)
        rows = []
        for union, _cnt in counts.most_common():
            rows.append(
                f"<tr class='u-row' data-camp='{title}' "
                f"data-union='{html.escape(union)}'>"
                f"<td class='u'>{html.escape(union)}</td>"
                "<td class='n u-w'>0</td><td class='n u-wp'>-</td>"
                "<td class='n u-b'>0</td><td class='n u-bp'>-</td>"
                "<td class='n u-s'>0</td><td class='n u-sp'>-</td></tr>")
        blocks.append(f"""
<div class='camp-block' data-camp='{title}'>
  <h3 class='camp-title'>{title}
    <span class='camp-sub'>阵营内 {total} 人 · {len(counts)} 个同盟</span></h3>
  <div class='camp-ctrl'>
    <label>问鼎人数 <input class='q-w in' type='number' min='0' step='1' value='0'></label>
    <label>紧接的霸业人数 <input class='q-b in' type='number' min='0' step='1' value='0'></label>
    <span class='camp-note'></span>
  </div>
  <table class='camptable'>
    <thead>
      <tr>
        <th>同盟</th>
        <th class='num'>问鼎人数</th><th class='num'>问鼎占比</th>
        <th class='num'>霸业人数</th><th class='num'>霸业占比</th>
        <th class='num'>问鼎+霸业</th><th class='num'>合计占比</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>""")

    hint = ("<p class='hint'>按<b>阵营内排行名次</b>顺位分配：前 X 名 = 问鼎；"
            "紧接着的 Y 名（第 X+1 ~ X+Y 名）= 霸业。"
            "占比都在本阵营内算：问鼎占比 = 该盟问鼎 ÷ X，"
            "霸业占比 = 该盟霸业 ÷ Y，"
            "合计占比 = 该盟（问鼎+霸业）÷ 本阵营（问鼎+霸业）总名额。"
            "输入存本机浏览器。</p>")
    return hint + "".join(blocks)


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>三谋排行榜汇总 · {batch}</title>
<style>
  :root {{
    --bg:#12151c; --panel:#1a1f29; --line:#2b3342; --text:#e8edf6;
    --muted:#8b97ab; --gold:#e0b970; --accent:#5aa9e6;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; padding:24px 18px 60px; background:var(--bg); color:var(--text);
    font:14px/1.6 "Microsoft YaHei",system-ui,-apple-system,sans-serif;
  }}
  h1 {{ font-size:20px; margin:0 0 6px; color:var(--gold); }}
  h2 {{ font-size:16px; margin:28px 0 10px; padding-left:8px;
       border-left:3px solid var(--accent); }}
  h3.camp-title {{ font-size:15px; margin:18px 0 10px; color:var(--gold); }}
  .camp-sub {{ color:var(--muted); font-size:12px; font-weight:400;
               margin-left:8px; }}
  .meta {{ color:var(--muted); font-size:12px; margin-bottom:18px; }}
  .meta code {{ color:var(--accent); }}
  .panel {{ background:var(--panel); border:1px solid var(--line);
            border-radius:8px; padding:14px 16px; margin-bottom:18px;
            overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px;
           table-layout:auto; }}
  th, td {{ padding:7px 10px; border-bottom:1px solid var(--line);
            text-align:left; vertical-align:middle; white-space:nowrap; }}
  thead th {{ color:var(--muted); font-weight:500; background:#151a23; }}
  tfoot td {{ color:var(--gold); border-top:1px solid var(--line); }}
  /* 数值列：表头与内容统一右对齐，否则「表头左 / 数字右」看着错位 */
  th.num, td.n {{ text-align:right; font-variant-numeric:tabular-nums;
                  padding-right:10px; }}
  td.u {{ color:var(--gold); }}
  input.in {{ width:90px; padding:5px 8px; border-radius:5px;
              border:1px solid var(--line); background:#0e1219;
              color:var(--text); text-align:right; }}
  input.in:focus {{ outline:1px solid var(--accent); }}
  .camp-ctrl {{ display:flex; gap:16px; align-items:center; flex-wrap:wrap;
                margin-bottom:10px; color:var(--muted); font-size:13px; }}
  .camp-note {{ color:var(--accent); }}
  .hint {{ color:var(--muted); font-size:12px; margin:8px 0 12px; }}
  .empty {{ color:var(--muted); }}
  .legend {{ color:var(--muted); font-size:12px; }}
  .legend b {{ color:var(--text); font-weight:500; }}
</style>
</head>
<body>
<h1>三谋排行榜汇总</h1>
<div class="meta">
  批次 <code>{batch}</code> · 生成于 {generated} · 解码帧 {decoded}/{total}
  · XOR key <code>{key}</code>
</div>

<div class="panel legend">
  <b>统计口径</b>：繁荣榜 / 武勋榜每次下发 300 行（6 帧 × 约 50 行）；
  首占榜<b>每个等级只有 100 人</b>（2 帧 × 约 50 行），本页是所选等级（{level} 级）的数据。
  所有数字都来自设备内 tcpdump（opcode 58624），一行 = 榜单一条记录。
  <ul style="margin:8px 0 0;padding-left:20px">{summary_html}</ul>
</div>

<h2>个人繁荣榜 · 按同盟汇聚</h2>
<div class="panel">{prosperity_table}</div>

<h2>个人武勋榜 · 按同盟汇聚</h2>
<div class="panel">{merit_table}</div>

<h2>{firstocc_title} · 按同盟分类</h2>
<div class="panel">{firstocc_table}</div>

<h2>阵营 · 问鼎 / 霸业 名额分配</h2>
<div class="panel">{camp_table}</div>

<div class="panel legend">
  数据来源：<code>screenshots/flow/rank_probe/rank_{batch}_*.pcap</code><br>
  抓取命令：<code>python scripts/probe.py --only 个人繁荣榜,个人武勋榜,个人首占榜 --firstocc9 --extra</code><br>
  生成命令：<code>python scripts/report.py --batch {batch}</code>
</div>

<script>
const STORE_KEY = 'sanmou-rank-quota-' + {batch_json};
const CAMP_ORDER = {camp_order_json};

function readStore() {{
  try {{ return JSON.parse(localStorage.getItem(STORE_KEY) || '{{}}') || {{}}; }}
  catch (e) {{ return {{}}; }}
}}
function writeStore(data) {{
  try {{ localStorage.setItem(STORE_KEY, JSON.stringify(data)); }} catch (e) {{}}
}}
function pct(part, whole) {{
  if (!whole) return '-';
  return (part * 100 / whole).toFixed(2) + '%';
}}

function recalcCamp(block) {{
  const camp = block.dataset.camp;
  const order = CAMP_ORDER[camp] || [];
  const xEl = block.querySelector('.q-w');
  const yEl = block.querySelector('.q-b');
  const total = order.length;

  // 语义：前 x 人问鼎；紧接着的 y 人（第 x+1 ~ x+y 名）霸业；其余未获得
  let x = Math.max(0, Math.floor(Number(xEl.value || 0)));
  let y = Math.max(0, Math.floor(Number(yEl.value || 0)));
  x = Math.min(x, total);
  y = Math.min(y, total - x);
  if (xEl.value !== '' && String(x) !== xEl.value) xEl.value = x;
  if (yEl.value !== '' && String(y) !== yEl.value) yEl.value = y;

  const cutW = x;                 // 问鼎截止名次
  const cutB = x + y;             // 霸业截止名次

  const stat = {{}};
  order.forEach((union, idx) => {{
    const rank = idx + 1;
    const s = stat[union] || (stat[union] = {{ w: 0, b: 0, n: 0 }});
    if (rank <= cutW) s.w += 1;
    else if (rank <= cutB) s.b += 1;
    else s.n += 1;
  }});

  const totalW = x;               // 问鼎名额
  const totalB = y;               // 霸业名额
  const totalQuota = totalW + totalB;   // 本阵营问鼎+霸业总名额

  block.querySelectorAll('tr.u-row').forEach(row => {{
    const s = stat[row.dataset.union] || {{ w: 0, b: 0, n: 0 }};
    row.querySelector('.u-w').textContent = s.w;
    row.querySelector('.u-wp').textContent = pct(s.w, totalW);
    row.querySelector('.u-b').textContent = s.b;
    row.querySelector('.u-bp').textContent = pct(s.b, totalB);
    row.querySelector('.u-s').textContent = s.w + s.b;
    // 合计占比 = 该盟(问鼎+霸业) ÷ 本阵营(问鼎+霸业)总名额
    row.querySelector('.u-sp').textContent = pct(s.w + s.b, totalQuota);
  }});

  block.querySelector('.camp-note').textContent =
    `问鼎 ${{totalW}} 人 · 霸业 ${{totalB}} 人（阵营共 ${{total}} 人）`;
}}

const saved = readStore();
document.querySelectorAll('.camp-block').forEach(block => {{
  const camp = block.dataset.camp;
  const wEl = block.querySelector('.q-w');
  const bEl = block.querySelector('.q-b');
  if (saved[camp]) {{
    if (saved[camp].w !== undefined) wEl.value = saved[camp].w;
    if (saved[camp].b !== undefined) bEl.value = saved[camp].b;
  }}
  const save = () => {{
    const data = readStore();
    data[camp] = {{ w: wEl.value, b: bEl.value }};
    writeStore(data);
    recalcCamp(block);
  }};
  wEl.addEventListener('input', save);
  bEl.addEventListener('input', save);
  recalcCamp(block);
}});
</script>
</body>
</html>
"""


def build(batch: str, out_path: str, level: str = "9") -> tuple[str, dict]:
    buckets, meta = load_rows(batch)
    prosperity = dedupe(buckets.get("prosperity", []))
    merit = dedupe(buckets.get("merit", []))
    firstocc = dedupe(buckets.get("first_occ", []))

    prosperity_unions = agg_by_union(prosperity)
    merit_unions = agg_by_union(merit)
    firstocc_unions = agg_by_union(firstocc, is_time=True)
    # 首占榜只做分类：按上榜人数从多到少排（时间列另算，不参与排序）
    firstocc_unions.sort(key=lambda row: row["count"], reverse=True)

    per_frame = meta.get("perFrame") or {}

    def frame_note(kind: str, rows: int) -> str:
        info = per_frame.get(kind)
        each, count = info if info else (0, 0)
        expect = EXPECTED_ROWS.get(kind)
        if expect:
            mark = "完整" if rows >= expect else f"缺 {expect - rows} 行"
            target = f"目标 {expect} 行 · {mark}"
        else:
            target = ""
        span = f"{count} 帧 × 每帧 {each} 行" if count else "无行帧"
        return f"{span} → {rows} 行" + (f"（{target}）" if target else "")

    camp_orders = meta.get("campOrders") or {}
    camp_note = "；".join(
        f"{name} {len(camp_orders.get(name) or [])} 人" for name in CAMP_NAMES)

    summary = [
        f"个人繁荣榜：{frame_note('prosperity', len(prosperity))} / "
        f"{len(prosperity_unions)} 个同盟",
        f"个人武勋榜：{frame_note('merit', len(merit))} / "
        f"{len(merit_unions)} 个同盟",
        f"个人首占榜（{level} 级）：{frame_note('first_occ', len(firstocc))} / "
        f"{len(firstocc_unions)} 个同盟",
        f"阵营内排行：{camp_note}",
    ]
    summary_html = "".join(f"<li>{html.escape(line)}</li>" for line in summary)

    # 首占榜：只按同盟分类 + 标注最后一位（榜尾）占领时间
    occ_total = len(firstocc)
    last_ts = max((rec["score"] for rec in firstocc), default=0)

    page = HTML_TMPL.format(
        batch=batch,
        batch_json=json.dumps(batch),
        camp_order_json=json.dumps(camp_orders, ensure_ascii=False),
        generated=time.strftime("%Y-%m-%d %H:%M:%S"),
        decoded=meta["decodedFrames"],
        total=meta["totalFrames"],
        key=meta["key"],
        level=html.escape(level),
        summary_html=summary_html,
        firstocc_title=f"个人首占榜（{html.escape(level)} 级）",
        prosperity_table=table_union(prosperity_unions, "繁荣度"),
        merit_table=table_union(merit_unions, "武勋"),
        firstocc_table=table_firstocc(firstocc_unions, occ_total,
                                      fmt_time(last_ts)),
        camp_table=camp_section(camp_orders),
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(page)

    stats = {"prosperity": (len(prosperity), len(prosperity_unions)),
             "merit": (len(merit), len(merit_unions)),
             "first_occ": (len(firstocc), len(firstocc_unions)),
             "camp": camp_note}
    return out_path, stats


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="排行榜静态 HTML 报表")
    parser.add_argument("--batch", default="", help="批次号，如 20261126_003448")
    parser.add_argument("--out", default="", help="输出 html 路径")
    parser.add_argument("--level", default="9", help="首占榜使用的等级（默认 9）")
    args = parser.parse_args(argv)

    batch = args.batch or list_batch()[0]
    out = args.out or os.path.join(OUT_DIR, f"rank_report_{batch}.html")
    path, stats = build(batch, out, args.level)
    latest = os.path.join(OUT_DIR, "rank_report_latest.html")
    build(batch, latest, args.level)

    print(f"HTML -> {path}")
    print(f"最新 -> {latest}")
    titles = {"prosperity": "繁荣榜", "merit": "武勋榜", "first_occ": "首占榜"}
    for kind, value in stats.items():
        if kind == "camp":
            print(f"阵营：{value}")
        else:
            rows, unions = value
            print(f"{titles[kind]}: 行 {rows}，同盟 {unions} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
