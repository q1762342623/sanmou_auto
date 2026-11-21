---
name: 战报自动收录
description: 三国谋定天下（com.bilibili.nslg）战报自动收录技能：用 adb 外部触控把游戏开到「战报 → 同盟 → 交战」列表，逐卡展开并滑到底，同时用模拟器内 tcpdump 抓游戏流量；拉回 pcap 后解码出原始战报 BLOB，增量打包上传到报表服务器，并等到 /api/live 可见。固定适配 1600x900 横屏，不注入、不读内存、不改包。
version: 1.0.0
requires:
  device: MuMu 模拟器 127.0.0.1:16416（1600x900 横屏）+ 游戏已登录并停在世界地图
  adb: C:\Program Files\Netease\MuMu\nx_main\adb.exe（需要 `adb root` 起 tcpdump）
  python: 3.10+；依赖 cv2 / numpy / Pillow / dpkt
  server: ListCardTeamStats 报表服务器（默认 http://127.0.0.1:8080）+ 与服务器一致的 ≥32 字符上传密钥
---

# 三谋 · 战报自动收录（一轮采集 → 上报）

一句话：**开列表、展开每张卡、滑到底，同时抓包；pcap 解出 BLOB，增量上传，等网页可见。**

```
adb 截图/触控 ─┐
                ├─→ 战报列表（同盟 → 交战）
设备内 tcpdump ─┘        │
        │                └─ 逐卡展开 / 滑动扫描（只做导航与状态确认）
        ↓
   game_traffic.pcap
        ↓  scripts/engine/extract_mumu_pcap_blobs.py（2 字节长度帧 + XOR + sproto-like）
   captures/<captureId>/blobs/*.bin + mumu_packet_extract.json
        ↓  scripts/engine/upload_capture_batches.py（ZIP + HMAC，32MB/500 文件一批）
   PUT /api/v1/batches/<captureId>/<batchId>
        ↓
   /api/live.visibleIngestVersion ≥ 本次 ingestVersion → 本轮完成
```

统计内容**全部来自网络 BLOB**，截图/OCR 只用于导航、状态确认和排障留证。

## 0. 命令一览（都在 skill 根目录执行）

| 命令 | 作用 |
| --- | --- |
| `python scripts/doctor.py` | 环境自检：引擎/模板/依赖/adb/root/tcpdump/上传配置/服务器 `/api/live` |
| `python scripts/screen.py preview` | 把比例目标画到当前截图上，用于首次校准 |
| `python scripts/screen.py state` | 输出当前页面状态（页签是否可见、交战是否选中、是否被弹窗遮挡） |
| `python scripts/round.py` | **一条命令跑完一轮**：采集 → 提取 → 上传 → 等可见 |
| `python scripts/capture.py --kind production` | 只采集（不提取、不上传） |
| `python scripts/extract.py --capture-dir <dir>` | 只对已有 capture 做提取与质量门槛 |
| `python scripts/upload.py --capture-id <id>` | 只上传指定 capture 并等可见 |

## 1. 前置检查（每次开工做一次）

```bash
python scripts/doctor.py
```

- 返回 `ok:true` → 继续
- `device-online:false` → 让操作员先启动 MuMu 并登录游戏
- `adbd-root:false` → 脚本会自动 `adb root`；仍失败说明该模拟器不允许 root adbd，tcpdump 不可用
- `device-tcpdump:false` → 模拟器内没有 tcpdump，需要先推一个可执行文件到 `/data/local/tmp/tcpdump`
- `python-deps:false` 且缺 `dpkt` → `python -m pip install dpkt`
- `server-upload-config:false` → 按 §1.1 配置
- `server-live:false` → 服务器没起或地址不对；本轮只能抓到本地（`--skip-upload`）

### 1.1 服务器与密钥（只做一次）

```bash
copy control\server_upload.example.json control\server_upload.json
# 编辑 baseUrl；把与服务器一致的密钥写进 control\server_upload_secret.txt（≥32 字符，不要提交）
copy config.example.json config.json
```

`config.json` 里的机器相关项：`adbPath`、`serial`、`server.configPath`；没人改坐标就不要动 `targets`。

## 2. 首次适配 / 校准（换机器或换分辨率才需要）

1. 游戏停在世界地图，执行：

   ```bash
   python scripts/screen.py preview
   ```

   会生成 `work/preview_<时间戳>_targets.png`，上面画了 `reportEntry`（左下战报入口）、
   `allianceTab`/`combatTab`（页签带 y≈0.124）、`pageBack`（左上返回）和滑动箭头。

2. 点位有偏差时只改 `config.json` 的 `targets`（全是屏幕比例，不是像素），
   再跑一次 `preview` 复核。判定标准：十字准星落在图标/页签文字正中。

3. 进入列表后 `python scripts/screen.py state` 应看到
   `reportTabsVisible:true` 且切到交战后 `combatTabSelected:true`。
   若页签带截取偏了，调 `targets.tabBand`（视觉判定用）。

> 默认值来自 1600x900 横屏实测：`reportEntry=(344,733)`、`combatTab=(602,112)`、
> `allianceTab=(1216,112)`。竖屏设备按 [`reference/ui-coordinates.md`](reference/ui-coordinates.md) 的音量比例替换。

## 3. 跑一轮（主线）

```bash
python scripts/round.py
```

内部顺序（任一步失败即整轮失败，单行 JSON 里 `failedStep` 指明位置）：

1. **采集** `capture.py`：确认设备/root → 已在战报页则先返回（`forceReload`，否则列表不重新请求）→
   发现游戏 TCP 端口 → 起 tcpdump → 进「战报 → 同盟 → 交战」并用 OCR + 金色边框校验页签 →
   每屏自下而上点开折叠卡 → 滑动 → 连续两页画面无变化即到底（或 `--max-pages` / `--max-seconds`）
   → 停 tcpdump、拉 pcap。
2. **提取** `extract.py`：解 BLOB、去重、算 `uniqueBlobs/rawRecords/completeRecords`。
   `uniqueBlobs=0` 或 `completeRecords=0` 直接判失败（这轮没有可统计战报）。
3. **上传** `upload.py`：只发本次 capture 新增 BLOB；服务器逐文件校验 size+sha256，批次幂等。
4. **等可见**：轮询 `/api/live`，直到 `visibleIngestVersion ≥` 本次回执 `ingestVersion`
   （默认最多 180s，`--no-wait` 可跳过）。

期望输出（节选，2026-11-21 17:50 实测）：

```json
{"ok":true,"captureId":"mumu_adb_production_20261121_175045_386",
 "capture":{"pages":3,"expandedCards":3,"expandFailures":1,"navigationsRecovered":0,
            "stopReason":"page_limit","pcapBytes":152605,"gamePorts":[20090],
            "screen":{"width":1600,"height":900}},
 "extract":{"uniqueBlobs":6,"rawRecords":34,"completeRecords":34,"xorKeySource":"auto"},
 "upload":{"uploadedBatches":1,"uploadedFiles":6,"uploadedBytes":24324,
           "captures":[{"ingestVersion":10414}]}}
```

`--max-pages` 只是冒烟/限流用；不带它时一轮会一直滑到列表底部（`stopReason:"list_end"`）。
上传成功后回执里 `ingestVersion` 必须为正；等可见时再比对 `/api/live.visibleIngestVersion`。

### 3.1 只抓不传（诊断）

```bash
python scripts/capture.py --kind diagnostic --max-pages 2 --max-seconds 240
python scripts/extract.py --capture-dir captures/<captureId>
```

diagnostic 的 capture **不能上传**（服务器会拒绝），只用于验证导航/端口/解码。

### 3.2 断点补传

上传失败不用重抓：capture 与 `work/server_upload_state.json` 都在，直接

```bash
python scripts/upload.py --capture-id <captureId>
```

## 4. 判定与失败分支

| 现象 | 判定 | 处理 |
| --- | --- | --- |
| `combatTabSelected:false` 连续 3 次 | 页签没点中/被活动弹窗挡住 | `screen.py state` 看 `blockingOverlay`；`screen.py back-to-map` 后重试；必要时调 `targets` |
| `expandFailures` 偏高、`expandedCards` 少 | 展开条模板尺度不匹配 | 调 `expand.templateScale`（0.8~1.1）、`threshold`、`minFrameRows`；`detect_arrows` 的 rejected 里有原因 |
| `stopReason:"blocking_overlay"` | 中途弹窗/公告 | 留证截图在 captureDir，人工清一次再重跑 |
| `stopReason:"list_end"` 但 `pages` 很小 | 列表本来就是旧内容（没新战报） | 正常；本轮 `uniqueBlobs` 可能为 0，属“无新增”，不是故障 |
| `stopReason:"combat_tab_lost"` | 误进了「战果」详情页且返回失败 | 看 `screen_page_*_after_swipe.png`；脚本已自动返回一次，连续失败再人工清页面 |
| `navigationsRecovered > 0` | 展开时点到卡片本体、进了详情页 | 已自动返回，不影响本轮；若频繁出现调 `expand.minHeight`/`downThreshold` |
| `no-blobs` | 端口错/没进列表/抓包太短/列表未重新请求 | 先确认 `capture.forceReload=true`；再看 `tcpdump.log` 与 `gamePorts`，必要时延长 `--max-seconds` |
| `visible-wait-timeout` | 服务器还没发布完 | 数据已入库，稍后 `/api/live` 会追上；不要重传 |
| pcap ≤24 字节 | 抓包进程没起来 | 确认 `adb root`、设备内有 tcpdump、端口发现结果非空 |
| `pull_error.txt` 里 `cannot create file/directory` | 中文目录 + 该版 adb 的已知问题 | `common.Adb.pull` 已先 `touch` 目标文件；如仍出现，改用纯 ASCII 输出目录 |

## 5. 关键资产

- `scripts/engine/`：解码与上传引擎（与上游 `list_card_team_stats` 同源，见
  [`reference/upstream.md`](reference/upstream.md)）
- `scripts/arrow_detect.py`：金色竖条 + 箭头方向检测（本项目按 1600x900 重写）
- `scripts/common.py`：`tab_status()` / `page_state()`（OCR + 金色边框的战报页判定）
- `reference/ui_templates/expand_down.png` / `expand_up.png`：右侧金色展开条模板
- `reference/current_report_era.json`：统计时代表；`report.reportEra:"auto"` 时取最新，
  且每轮采集前会先从服务器 `/api/report-era-schedule` 同步
- `captures/<captureId>/`：`game_traffic.pcap`、`blobs/`、`capture_metadata.json`、截图、`tcpdump.log`
- `work/rounds/round_*.json`：每轮汇总（含耗时、页数、展开数、停止原因）
- `work/server_upload_state.json`：上传断点（按 capture 记 `maxBlobSeq` / `ingestVersion`）

## 6. 边界与约束

1. **同一台模拟器同一时间只能跑一个自动化**：与「自动拉线」(`flow_loop`) 互斥，
   否则触控会互相打断；先停一边。
2. 只做外部截图 + `input tap/swipe`，不注入、不 hook、不改包。
3. 不按 Android BACK 关面板（会弹「确认退出」），一律点页面自带返回箭头。
4. pcap、截图、tcpdump.log **只留在本地**，上传的只有 `blobs/` + 元数据。
5. 服务器接受时间偏差 5 分钟内的签名请求；采集机与服务器时钟要大致同步。
6. 跨统计时代（era）的 capture 会被服务器拒绝：确认 `current_report_era.json` 与服务器一致。

## 7. 汇报契约

调用方只需读最后一行的 JSON：

- `ok:true` → 本轮完成，报告 `capture.pages`、`capture.expandedCards`、
  `extract.uniqueBlobs`、`extract.completeRecords`、`visibility.visibleIngestVersion`
- `ok:false` → 报告 `failedStep` / `reason` / `error`，并按 §4 给出下一步建议
