# 来源与同步说明

本技能是把另一个工程 [`list_card_team_stats`](file:///C:/Users/Administrator/Desktop/三谋大数据/list_card_team_stats)
里「ADB + tcpdump 自动记录战报」的成熟链路，按本项目分辨率（1600x900 横屏）重新编排后的**精简版**。

## 1. 内置（原样复制，勿手改）

```
scripts/engine/decode_list_card_sproto_like.py   战报 sproto-like 解码
scripts/engine/extract_mumu_pcap_blobs.py        pcap → BLOB（分流/切帧/XOR/去重）
scripts/engine/tool_contract.py                  capture_metadata schemaVersion 契约
scripts/engine/upload_capture_batches.py         增量 ZIP + HMAC 上传
scripts/engine/server_report_era.py              上传前同步服务器 era 计划
scripts/engine/report_era.py                     era 配置读写/规范化/摘要（server_report_era 依赖）
scripts/engine/report_policy.py                  报表保留策略常量
scripts/engine/detect_expand_arrow.py            箭头模板打分（本项目只借用 arrow_template_score）
scripts/engine/inspect_mumu_ui.py                页面状态检测（页签/遮挡/画面差异）
scripts/reference/tool_contract.json             tool_contract.py 按 parents[1]/reference 读取，故放这里
reference/tool_contract.json                     同上（便于人工查看）
reference/ui_templates/*.png                      可选本地模板覆盖（仓库不提交图片；
                                                  默认用 scripts/engine/arrow_templates.py 内嵌数据）
reference/current_report_era.json                统计时代表（每轮采集前会从服务器同步覆盖）
```

同步上游改动（在上游仓库更新后执行，注意别覆盖本地的 era 文件）：

```powershell
$src = "..\三谋大数据\list_card_team_stats"
copy /Y "$src\scripts\decode_list_card_sproto_like.py"  "skills\战报自动收录\scripts\engine\"
copy /Y "$src\scripts\extract_mumu_pcap_blobs.py"        "skills\战报自动收录\scripts\engine\"
copy /Y "$src\scripts\upload_capture_batches.py"         "skills\战报自动收录\scripts\engine\"
copy /Y "$src\scripts\server_report_era.py"              "skills\战报自动收录\scripts\engine\"
copy /Y "$src\scripts\report_era.py"                     "skills\战报自动收录\scripts\engine\"
copy /Y "$src\scripts\report_policy.py"                  "skills\战报自动收录\scripts\engine\"
copy /Y "$src\scripts\tool_contract.py"                  "skills\战报自动收录\scripts\engine\"
copy /Y "$src\reference\tool_contract.json"              "skills\战报自动收录\scripts\reference\"
copy /Y "$src\reference\tool_contract.json"              "skills\战报自动收录\reference\"
copy /Y "$src\scripts\detect_expand_arrow.py"            "skills\战报自动收录\scripts\engine\"
copy /Y "$src\scripts\inspect_mumu_ui.py"                "skills\战报自动收录\scripts\engine\"
# 可选：仅当要替换内嵌模板时执行（替换后需重新生成 engine/arrow_templates.py 的内嵌数据）
copy /Y "$src\reference\ui_templates\expand_down.png"    "skills\战报自动收录\reference\ui_templates\"
copy /Y "$src\reference\ui_templates\expand_up.png"      "skills\战报自动收录\reference\ui_templates\"
```

## 2. 本地改写（本技能自己的代码）

```
scripts/common.py       配置/adb/截图/比例坐标/页签 OCR 判定/引擎加载
scripts/arrow_detect.py 金色竖条 + 箭头方向检测（替代上游横屏分支）
scripts/screen.py       shot / state / preview / back-to-map
scripts/capture.py      一轮采集：端口 → tcpdump → 导航 → 逐卡展开 → 滑动到底 → 拉 pcap
scripts/extract.py      提取包装 + 质量门槛
scripts/upload.py       上传包装 + era 同步 + /api/live 可见性等待
scripts/doctor.py       环境自检
scripts/round.py        端到端编排（collect → extract → upload → visible）
```

与上游脚本的差异：

| 上游 | 本技能 | 原因 |
| --- | --- | --- |
| PowerShell 2618 行 `capture_mumu_list_cards.ps1` | Python `capture.py`（~380 行） | 只保留「抓一轮」必需路径，便于与本项目 Python 工具链合并 |
| 固定竖屏 `control/mumu_screen.json`（900x1600） | 比例坐标 + 1600x900 实测默认值 | 本项目设备为 1600x900 横屏，不强行 `wm size` 改分辨率 |
| 跨轮边界（observed anchor）与抽样质量统计 | 无，整轮全量扫描到列表底部 | 一轮任务不需要跨轮去重；重置/去重交给服务器 |
| 常驻截图流 + 视觉服务进程 | 每次 `exec-out screencap -p` | 少一个常驻进程；单轮 3~8 张截图，开销可接受 |
| 独立上传队列/后台并行 | 单轮同步上传 | 一轮一传，失败可按 captureId 断点补传 |

## 3. 许可与责任

上游为内部工具；BLOB 内含玩家名、同盟、坐标与战斗数据，`captures/`、`work/`、
`control/server_upload_secret.txt` **都不得提交或外发**（见 `.gitignore`）。
