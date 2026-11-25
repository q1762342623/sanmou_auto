---
name: 战报自动收录
description: 三国谋定天下（com.bilibili.nslg）战报自动收录技能：用 adb 外部触控把游戏开到「战报 → 同盟 → 交战」列表，逐卡展开并滑到底，同时用模拟器内 tcpdump 抓游戏流量；拉回 pcap 后解码出原始战报 BLOB，增量打包上传到报表服务器，并等到 /api/live 可见。固定适配 1600x900 横屏，不注入、不读内存、不改包。
version: 1.0.0
requires:
  device: MuMu 模拟器 127.0.0.1:16416（1600x900 横屏）+ 游戏已登录并停在世界地图
  adb: C:\Program Files\Netease\MuMu\nx_main\adb.exe（需要 `adb root` 起 tcpdump）
  python: 3.10+；依赖 cv2 / numpy / Pillow / dpkt
  server: ListCardTeamStats 报表服务器（远端地址写在 `control/server_upload.json`，不入库）+ 与服务器一致的 ≥32 字符上传密钥
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
| `python scripts/round.py` | **一条命令跑完一轮**：采集 → 提取 → 上传 → 等可见；同时输出交战坐标热点 |
| `python scripts/capture.py --kind production` | 只采集（不提取、不上传） |
| `python scripts/extract.py --capture-dir <dir>` | 只对已有 capture 做提取与质量门槛 |
| `python scripts/upload.py --capture-id <id>` | 只上传指定 capture 并等可见 |
| `python scripts/coordinate_stats.py` | 只汇总「本次进入战报→拉到底」的 capture 坐标，输出最高交战频率点（含已提交过的战报，不累计历史） |

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

### 1.1 服务器与密钥

本机已预置远端配置（从本地端工程 `list_card_team_stats-main` 同步）：

- `control/server_upload.json`：`baseUrl=http://<报表服务器>:8080`（本机配置，勿提交）
- `control/server_upload_secret.txt`：与远端服务器一致的上传密钥
- `config.json`：`adbPath` / `serial=127.0.0.1:16416`，`server.baseUrl` 留空以读取上面的上传配置

报表入口：`http://<报表服务器>:8080/`；`python scripts/doctor.py` 的
`server-upload-config` 与 `server-live` 必须为 `true`。

换服务器或密钥时重新生成这两个运行文件（二者均被 `.gitignore` 忽略，不要提交）：

```powershell
.\scripts\configure_server_upload.ps1 -BaseUrl "http://<报表服务器>:8080" -Secret "至少32字符的随机密钥"
# 或手工：
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
   **每屏自下而上点开折叠卡 → 上滑 → 重复，直到列表真正到底**（连续两屏滑不动，且每屏
   滑后都会再等一次排除“正在请求下一批”）。`maxPages=0` 默认不设页数上限，只有
   `stopReason:"list_end"` 才是完整一轮；`--max-seconds`/`hardMaxPages` 只是防止死循环的
   安全阈值，触发时本轮标记为不完整。
2. **完整性门槛**：production 采集若 `complete:false`（`page_limit` / `max_runtime` /
   `hard_page_cap` / 弹窗 / 页签丢失），会在 capture 目录写 `incomplete.json` 并直接判失败，
   **保留 pcap 与截图但拒绝提取上传**；`upload.py` 也会拒绝带 `incomplete.json` 的 capture。
3. **提取** `extract.py`：解 BLOB、去重、算 `uniqueBlobs/rawRecords/completeRecords`。
   `uniqueBlobs=0` 或 `completeRecords=0` 直接判失败（这轮没有可统计战报）。
4. **上传** `upload.py`：只发本次 capture 新增 BLOB；服务器逐文件校验 size+sha256，批次幂等。
5. **等可见**：轮询 `/api/live`，直到 `visibleIngestVersion ≥` 本次回执 `ingestVersion`
   （默认最多 900s：服务器 180s 发布静默窗口 + 约 120s 构建，180s 必然超时；`--no-wait` 可跳过）。
   超时只代表“还没发布完”，数据已入库，不要重传。

期望输出（节选，2026-11-21 18:32 诊断实测：一直滑到列表底部才结束）：

```json
{"ok":true,"captureId":"mumu_adb_diagnostic_20261121_183234_154",
 "capture":{"pages":5,"screens":9,"complete":true,"expandedCards":3,"expandFailures":1,
            "navigationsRecovered":3,"stopReason":"list_end","pcapBytes":323106,
            "gamePorts":[20090],"screen":{"width":1600,"height":900}},
 "extract":{"uniqueBlobs":9,"rawRecords":48,"completeRecords":48,"xorKeySource":"auto"}}
```

`pages` 是真正滑动的次数，`screens` 是处理过的屏数（含最后两屏“滑不动”的确认）。
`--max-pages` 只用于冒烟/校准，任何 >0 的值都会让本轮 `complete:false`、无法上传。
上传成功后回执里 `ingestVersion` 必须为正；等可见时再比对 `/api/live.visibleIngestVersion`。
`--allow-partial`：没翻到 `list_end` 也继续上传（整合调度器 `report_collector` 使用，
服务器按 BLOB 增量去重；独立跑默认严格，必须有 `list_end`）。

采集性能与完整性（2026-11-23 优化后）：

- 截图走设备 raw（`screencap` 不带 `-p`，约 0.24s/帧；旧 PNG 约 0.65s/帧），帧在内存里复用，只有留证帧才写 PNG；`capture.visualTiming` 给出 `frames / frameSeconds / avgFrameMs / expandSeconds / swipeSeconds / pageAvgSeconds`。
- 展开/收起统一用「整屏静止帧 + 箭头 up/down」双确认（`expand.openConfirmMs` / `closeConfirmMs`），去掉重复等待；默认逐卡处理（`burstTap=false`）。
- 每屏收尾都用新帧审计：`pageLog[].leftoverDowns > 0` 表示还有未展开的完整卡片（正常为 0）；`leftoverUps > 0` 表示遗留展开态，二者都会兜底处理。
- 历史遗留的展开卡会先收起再重新展开，确保本轮 tcpdump 窗口内重新下发详情（`pageLog[].preCollapsed`）。
- `capture.requireListEnd=true`（默认）：没滑到 `list_end` 的 production capture 会写 `incomplete.json` 并拒绝上传，避免半轮数据被当成完整战报。

### 3.1 只抓不传（诊断）

```bash
python scripts/capture.py --kind diagnostic --max-seconds 240   # 同样翻到列表底
python scripts/extract.py --capture-dir captures/<captureId>
```

diagnostic 的 capture **不能上传**（服务器会拒绝），只用于验证导航/端口/解码。
需要快速看某一屏时才用 `--max-pages N`；这种采集会标记 `complete:false`。

### 3.2 断点补传

上传失败不用重抓：capture 与 `work/server_upload_state.json` 都在，直接

```bash
python scripts/upload.py --capture-id <captureId>
```

## 4. 判定与失败分支

| 现象 | 判定 | 处理 |
| --- | --- | --- |
| `combatTabSelected:false` 连续 3 次 | 页签没点中/被活动弹窗挡住 | `screen.py state` 看 `blockingOverlay`；`screen.py back-to-map` 后重试；必要时调 `targets` |
| `expandFailures` 偏高、`expandedCards` 少 | 展开条没点中：多为「列表还没停稳就取坐标」 | 确认 `capture.stableTimeoutMs≥2000` 生效（`capture.py` 的 `wait_still`）；再调 `expand.templateScale`（0.8~1.1）与 `maxWidth/maxHeight` |
| `stopReason:"blocking_overlay"` | 中途弹窗/公告 | 留证截图在 captureDir，人工清一次再重跑 |
| `stopReason:"list_end"` 但 `pages` 很小 | 列表本来就是旧内容（没新战报），或滑动被游戏吞掉 | 先看 `capture_result.json → pageLog`：`shiftPx` 应稳定在 -300~-430，若多次出现 0/正值说明手势被吞/列表刷新；`leftoverDowns` 不为 0 说明本屏还有折叠卡没展开 |
| `complete:false`（`page_limit`/`max_runtime`/`hard_page_cap`） | 没翻到底，本轮不完整 | 增加 `--max-seconds` 后重跑；不要手工上传该 capture（`incomplete.json` 已被 upload 拒绝） |
| 滑到底后仍缺最后一张折叠卡 | 末尾卡高度不足 `expand.minHeight`，未被点击 | 先看 `screen_page_*_after_swipe.png`；必要时调小 `expand.minHeight` 或人工补收 |
| `stopReason:"combat_tab_lost"` | 误进了「战果」详情页且返回失败 | 看 `screen_page_*_after_swipe.png`；脚本已自动返回一次，连续失败再人工清页面 |
| `navigationsRecovered > 0` | 展开时点到卡片本体、进了详情页（坐标已过期：滑动后列表仍在动） | 已自动返回；正常应为 0。若频繁出现，检查 `capture.stable*` 与 `expand.clickBottomInset`，并确认点击点没离开金色竖条 |
| `no-blobs` | 端口错/没进列表/抓包太短/列表未重新请求 | 先确认 `capture.forceReload=true`；再看 `tcpdump.log` 与 `gamePorts`，必要时延长 `--max-seconds` |
| `visible-wait-timeout` | 服务器还没发布完 | 数据已入库，稍后 `/api/live` 会追上；不要重传 |
| pcap ≤24 字节 | 抓包进程没起来 | 确认 `adb root`、设备内有 tcpdump、端口发现结果非空 |
| `pull_error.txt` 里 `cannot create file/directory` | 中文目录 + 该版 adb 的已知问题 | `common.Adb.pull` 已先 `touch` 目标文件；如仍出现，改用纯 ASCII 输出目录 |

## 5. 关键资产

- `scripts/engine/`：解码与上传引擎（与上游 `list_card_team_stats` 同源，见
  [`reference/upstream.md`](reference/upstream.md)）
- `scripts/arrow_detect.py`：金色竖条 + 箭头方向检测（本项目按 1600x900 重写）
- `scripts/common.py`：`tab_status()` / `page_state()`（OCR + 金色边框的战报页判定）
- 展开条模板：内嵌在 `scripts/engine/arrow_templates.py`（zlib+base64，**仓库不提交图片**）；
  本地若放 `reference/ui_templates/expand_*.png` 会优先使用（方便现场替换）
- `reference/current_report_era.json`：统计时代表；`report.reportEra:"auto"` 时取最新，
  且每轮采集前会先从服务器 `/api/report-era-schedule` 同步
- `captures/<captureId>/`：`game_traffic.pcap`、`blobs/`、`capture_metadata.json`、截图、`tcpdump.log`
- `work/rounds/round_*.json`：每轮汇总（含耗时、页数、展开数、停止原因）
- `work/server_upload_state.json`：上传断点（按 capture 记 `maxBlobSeq` / `ingestVersion`）
- `work/latest_hotspot.json` / `work/latest_hotspot.txt`：本次 capture 汇总出的最高交战频率点

## 6. 边界与约束

1. **同一台模拟器同一时间只能跑一个自动化**：与
   「三谋拉线与战报自动收集」(`python -m sanmou_auto.atomic_loop`) 互斥，
   否则触控会互相打断；先停一边。
   原子调度器已把本技能串行化（收集前后自动回世界地图，失败进补交队列）；
   长跑期间不要再手动并发跑 `round.py`。
2. 只做外部截图 + `input tap/swipe`，不注入、不 hook、不改包。
3. 不按 Android BACK 关面板（会弹「确认退出」），一律点页面自带返回箭头。
4. pcap、截图、tcpdump.log **只留在本地**，上传的只有 `blobs/` + 元数据。
5. 服务器接受时间偏差 5 分钟内的签名请求；采集机与服务器时钟要大致同步。
6. 跨统计时代（era）的 capture 会被服务器拒绝：确认 `current_report_era.json` 与服务器一致。

## 7. 汇报契约

调用方只需读最后一行的 JSON：

- `ok:true` → 本轮完成，报告 `capture.pages`、`capture.expandedCards`、
  `extract.uniqueBlobs`、`extract.completeRecords`、`visibility.visibleIngestVersion`
- `coordinateHotspot` → **只统计本轮 capture** 的交战坐标热点
  （本轮 = 本次从进入战报到拉到底抓到的全部战报，含之前已提交过的战报；
  与上传状态、历史 capture 无关）：
  - `scope:"round"`、`captureId`、`captureKind`、`reportEra`；
  - `records`：本轮解出的完整战报记录数（含分页重复带回）；
  - `battles`：本轮内去重后的战报场次总数；`duplicates`：两者之差；
  - `hottest` / `top[]`：`x`、`y`、`count`（该点战报场次）、`share`（占比）。
  同时会覆盖写 `work/latest_hotspot.json` 与 `work/latest_hotspot.txt`
  （格式 `x,y,count,share`），并在 stderr 打一行 `[交战热点] ...`
  （stdout 最后一行仍是本轮 JSON）。
- `ok:false` → 报告 `failedStep` / `reason` / `error`，并按 §4 给出下一步建议
