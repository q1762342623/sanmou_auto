# 采集 → 解码 → 上传 全链路细节

## 1. 游戏流量与端口

游戏进程：`com.bilibili.nslg`。通信走多条 TCP 长连接，端口不固定，**每轮现探**：

```bash
adb root && adb wait-for-device          # ss -p 需要 root 才能看到进程名
adb -s 127.0.0.1:16416 shell ss -tnp | findstr nslg
# 只取「对端地址」端口（第 5 列），范围 10000~65535，排除 5555/16384
```

> 非 root 时 `ss -p` 看不到 `nslg`，会退回 `fallback`；实测 root 后本机为 `[20090]`。

探测不到时使用 `config.ports.fallback = [20020, 20050, 20060, 20090]`（上游工程长期观察到的端口）。
端口必须与游戏当前真实连接一致，否则 pcap 里没有战报帧，表现为 `uniqueBlobs=0`。
本机临时端口不能混进来，否则会把无关流量也抓进 pcap。

## 2. tcpdump（设备内，需要 root adbd）

```bash
adb root && adb wait-for-device && adb shell id       # 期望 uid=0(root)
adb shell "/system/xbin/tcpdump -i any -s 0 -U -w /data/local/tmp/x.pcap \
           'tcp port 20020 or tcp port 20050 or tcp port 20060' \
           > /data/local/tmp/x.log 2>&1 & echo $!"      # 记录 PID
# ……采集……
adb shell kill -2 <PID>                                # 先 SIGINT 让 tcpdump 刷盘
adb pull /data/local/tmp/x.pcap captures/<id>/game_traffic.pcap
```

- `-s 0` 不截断、`-U` 边抓边写；`kill -2` 后至少等 1.2s 再 pull，否则尾部帧会丢；
- 进程不退再补 `kill -15`；
- **先 pull 再删远端文件**（`stop_tcpdump` 只负责停进程），否则会拉到空文件；
- 拉回后立刻校验大小 > 24 字节（pcap 全局头 24 字节）；
- 该版 adb 在含中文目录下**新建**文件会报 `cannot create file/directory`，
  `Adb.pull` 先 `touch` 目标文件再 pull（实测可稳定覆盖）。

实测一轮（3 页、未到底）：pcap 152 KB → 27 个传输帧 → 6 个去重 BLOB / 34 条完整记录。

## 3. pcap → BLOB（`scripts/engine/extract_mumu_pcap_blobs.py`）

1. `dpkt` 逐包读 TCP，按四元组分流，按 `seq` 合并乱序/重传；
2. 只保留**服务器下发方向**（`source.port ∈ game_ports`）；
3. 按两字节**大端**长度前缀切帧（长度不含前缀），tcpdump 中途开始导致错位时做重同步；
4. 传输头 9 字节，负载用 8 字节 XOR 密钥解密；密钥可自动发现
   （默认 `3330d32fba040800`，逐帧候选投票）；
5. 解出 sproto-like 战报记录，统计 `rawRecords` / `completeRecords`；
6. 每个 **去重后**的 BLOB 落盘为
   `blobs/00001_mumu_tcp_<size>_<sha12>.bin`，并写 `binary_string_blobs.jsonl`、
   `timeline.jsonl`、`mumu_packet_extract.json`。

质量门槛（本技能强制）：

| 指标 | 要求 | 不满足说明 |
| --- | --- | --- |
| `uniqueBlobs` | > 0 | 端口错 / 没进列表 / 抓包太短 / 被弹窗挡住 |
| `completeRecords` | > 0 | 列表没加载完，或抓包窗口覆盖不到首屏请求 |

> 上游实测：打开战报页那一次响应就带着整页 10 张卡的战法 ID；滑动本身不加载新数据。
> 展开卡片会补该卡的详细交战记录。所以「展开 + 滑到底」既保证覆盖，也提高单卡完整度。

**因此采集前必须让列表重新请求一次**：如果已经停在战报页，先点左上返回再点战报入口
（`capture.forceReload=true` 的默认行为），否则 pcap 里只有心跳，`uniqueBlobs=0`。

本机实测（2026-11-21 17:50）：3 页 + 3 次展开 → pcap 152 KB、27 个传输帧、
6 个去重 BLOB、34 条完整记录；整轮 29 秒（含上传）。

## 4. capture 目录结构

```
captures/mumu_adb_production_<ts>_<ms>/
  capture_metadata.json      # captureKind/reportEra/gamePorts/screen/orientation/source
  game_traffic.pcap          # 只留本地，不上传
  tcpdump.log                # 只留本地
  blobs/*.bin                # 上传内容
  binary_string_blobs.jsonl
  timeline.jsonl
  mumu_packet_extract.json
  screen_main.png / screen_list.png / screen_page_*.png   # 导航留证
  capture_result.json        # 本轮页数/展开数/停止原因
```

目录名必须匹配 `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`（上传器强校验）。

## 5. 上传协议（`scripts/engine/upload_capture_batches.py`）

```
PUT {baseUrl}/api/v1/batches/<captureId>/<batchId>
Content-Type: application/zip
X-Upload-Timestamp: <unix 秒>
X-Upload-Content-SHA256: <ZIP 的 sha256>
X-Upload-Signature: HMAC-SHA256(secret, "PUT\n<path>\n<timestamp>\n<contentSha256>")
```

- 每批最多 32 MiB / 500 个文件（`control/server_upload.json` 可调）；
- ZIP 固定时间戳、deflate-3，内含 `batch_manifest.json`、`capture_metadata.json`、`blobs/<name>`；
- 服务器校验：只收 `captureKind=production`、era 匹配、清单内文件、size+sha256 一致；
- 同一 `(captureId,batchId)` 重复提交幂等，返回同一 `ingestVersion`；
- 时间偏差 >5 分钟拒绝（`X-Upload-Timestamp`）。

回执必须满足才能推进断点：

```text
accepted = true
batch.serverInstanceId == 服务器当前实例
batch.captureId == captureId
batch.batchId == batchId
batch.maxBlobSeq == 本批最大 seq
batch.ingestVersion > 0
```

断点存 `work/server_upload_state.json`（按 capture 记 `maxBlobSeq`、`ingestVersion`、服务器地址与实例）。
服务器换实例或换地址后旧断点自动作废、重传。

## 6. 可见性判定

```bash
curl http://124.221.206.94:8080/api/live
# { "ingestVersion": 10414, "visibleIngestVersion": 10413, "publishing": false,
#   "buildId": "...", "caughtUp": false, "lastError": "" }
```

- 上传成功 = 已入库排队；
- `visibleIngestVersion ≥ 本次 ingestVersion` = 已进入可访问 generation；
- 服务器按静默窗口合并发布（默认 180s 静默 / 900s 上限），所以可见可能滞后几分钟；
- 生成失败会保留上一代报表并按 5~300s 退避重试。

`/api/ready` 200、`/api/live.caughtUp=true`、`lastError` 为空 = 报表健康。

## 7. 统计时代（era）

`reference/current_report_era.json`（schemaVersion 2）按 `startedAt` 排序，
当前 era = 最新一条。`report.reportEra="auto"` 取最新，也可 `--report-era <id>` 显式指定。

服务器与客户端 era 必须一致；服务器还会通过 `/api/report-era-schedule` 下发计划供客户端同步。
跨 era 的 capture 会被拒绝（`skippedCaptures` 返回原因），此时不要改 era 硬传。

## 8. 常见故障对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `ss` 无 nslg 行 | 游戏没连上/在登录界面 | 停在已登录的地图再抓 |
| pcap 有包但 `transportFrames=0` | 端口不是游戏业务端口 | 重新发现端口；调 `ports.fallback` |
| `xorKeySource=auto` 且 `uniqueBlobs=0` | 密钥/帧同步失败 | 检查是否抓到首屏；延长抓包窗口 |
| 上传 HTTP 401/403 | 密钥不一致或时钟偏差 >5 分钟 | 同步密钥/时钟 |
| 上传 HTTP 409/422 | era/清单/内容冲突 | 看响应 `error`，不要改 BLOB 重传 |
| `visible-wait-timeout` | 服务器积压/发布失败 | 查 `/api/status` 的 `lastError` |
