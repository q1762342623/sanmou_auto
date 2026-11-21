# sanmou_auto

## 工程目标

做一个「三谋」（三国谋定天下，`com.bilibili.nslg`）自动化程序。

- 运行载体：MuMu 模拟器 + adb（仅外部截图与模拟触摸，不注入、不读内存、不改包）
- 最终能力：**邮件指令识别**、**自动化战斗**
- 交付形态：功能以可被上层 agent「大肥鱼」调用的 skill 形式提供，而不是固定脚本流程

## 协作约定

- 本仓库只存放**已验证正确**的功能代码，合入 `main` 前必须在模拟器上实测通过
- 每个功能**单独设计、单独验证**，不预先编写总方案
- 功能清单、接口、坐标与模板随功能一并提交

## 交付形态：Markdown Skill + 少量原子脚本

**主要交付物**是 [`skills/sanmou-email-rally/`](../skills/sanmou-email-rally/)：
一份写给 agent 看的 `SKILL.md` + 4 个单职责脚本（每个只输出一行 JSON），
由上层 agent 自行编排，本仓库不包含 agent 调度逻辑。

```
skills/sanmou-email-rally/
  SKILL.md                    玩法说明书：步骤、判定门槛、失败分支、汇报契约
  reference/ui-coordinates.md 全部实测坐标与面板解剖
  reference/keyword-rules.md  关键字白/黑名单与动作映射
  scripts/screen.py           屏幕状态 / 截图 / OCR
  scripts/mail.py             读邮件 / 打开指定邮件 / 点坐标跳转
  scripts/camp.py             同盟建筑 + 同盟城池（城/水寨/军港/民港）：列据点 / 取最近 / 点选
  scripts/troop.py            调动面板：列状态 / 派兵 / 记录到达时间
```

典型编排见 SKILL.md。本包内的 `flow_mail.py` / `flow_mobilize.py` 是**一键参考实现**，
用于回归验证，不是必须的调用方式。

## 当前状态

| 功能 | 状态 | Skill | 一键参考实现 |
| --- | --- | --- | --- |
| 功能一：读邮件 → 关键字 → 跳坐标 → 最近可调动据点 | ✅ 实测通过（2026-11-20） | `skills/sanmou-email-rally/SKILL.md` §1–3 | `python -m sanmou_auto.flow_mail` |
| 功能二：最近 10 条同盟邮件 → 战场 → 最近可调动据点（大营/城/水寨/军港/民港）→ 调动 部队1-4 → 记录最后到达时间 | ✅ 实测通过（2026-11-20） | `skills/sanmou-email-rally/SKILL.md` §1–5 | `python -m sanmou_auto.flow_mobilize` |
| 功能三：调动前兵力检查（不满点白色补兵，补后仍 <30000 跳过该队）+ 到达后按邮件 集结/攻占/行军；邮件不可执行时先找红色敌对目标，视野无红敌则用「战报」绿色坐标 → 攻城；处理「未携带满战法」确认框；打完回据点 → 回城 | ✅ 实测通过（2026-11-21 14:06，4 队出征→返回→回城） | `skills/sanmou-email-rally/SKILL.md` §4.4–4.6 | `python -m sanmou_auto.flow_mobilize`（默认含战斗） |
| **自动拉线**：循环「邮件分析战场 → 调动据点 → 寻找敌方进攻 → 回家补兵」，统计每轮每个节点耗时；据点列表跨轮缓存（省掉每轮 ≈340s 的同盟列表扫描） | ✅ 实测通过（2026-11-21，单轮 + 1 小时长跑见 [`docs/自动拉线.md`](docs/自动拉线.md)） | — | `python -m sanmou_auto.flow_loop` |
| **战报自动收录**：进「战报→同盟→交战」→ 逐卡展开 + 滑到底（adb 触控），同时设备内 tcpdump 抓游戏流量 → pcap 解出 BLOB → 增量 HMAC 上传报表服务器 → 等 `/api/live` 可见；1600x900 横屏实测（3 页 / 6 BLOB / 34 条记录，29s 一轮） | ✅ 实测通过（2026-11-21，ingestVersion 10414） | [`skills/战报自动收录/SKILL.md`](skills/战报自动收录/SKILL.md) | `python skills/战报自动收录/scripts/round.py` |

### 环境依赖

```bash
python -m pip install --no-input rapidocr-onnxruntime
# 已含：onnxruntime / opencv-python / numpy / Pillow(已装)
```

- adb：`C:\Program Files\Netease\MuMu\nx_main\adb.exe`
- 设备：`127.0.0.1:16416`（1600x900 横屏）

### 快速开始

```bash
python -m sanmou_auto.flow_mail --dry-run    # 只读 10 封邮件并解析关键字
python -m sanmou_auto.flow_mail              # 读邮件 → 跳最近指令坐标 → 点最近可调动据点 → 截图
python -m sanmou_auto.flow_mail --mode action --limit 5   # 跳坐标后点 攻城/占领/行军/驻守（不确认）

python -m sanmou_auto.flow_mobilize                    # 邮件 → 战场 → 据点 → 调动 1-4 → 正面战斗 → 回城
python -m sanmou_auto.flow_mobilize --troops 1,2,3     # 只调动 1-3 队（范围固定 1-4）
python -m sanmou_auto.flow_mobilize --no-battle        # 只调动，不进入战斗
python -m sanmou_auto.flow_mobilize --no-wait          # 出征后不等待战斗结束
python -m sanmou_auto.flow_mobilize --min-strength 30000 --no-replenish  # 兵力门槛/关闭补兵
python -m sanmou_auto.flow_mobilize --dry-run          # 只到点选最近可调动据点

python -m sanmou_auto.flow_loop                        # 自动拉线：连跑 1 小时并统计每轮节点耗时
python -m sanmou_auto.flow_loop --minutes 10           # 跑 10 分钟
python -m sanmou_auto.flow_loop --max-rounds 1         # 只跑一轮（冒烟）
python -m sanmou_auto.home                              # 只跑「回家补兵」节点（回城→亲卫营→检查/补兵→出城）
```

截图输出：`screenshots/flow/`（JPEG 宽 1000，单张 60–140KB，避免大图撑爆上下文）

### 模块

```
sanmou_auto/
  config.py       设备/分辨率/固定 UI 坐标
  adb.py          screencap / tap / swipe / keyevent
  vision.py       低画质落盘、裁剪
  ocrengine.py    RapidOCR 封装 + 切块识别 ocr_tiled()
  intent.py       关键字白名单/黑名单 -> 动作，坐标正则
  mail.py         邮件页读取（页签/翻页/正文/坐标点击点）
  alliance.py     同盟建筑 + 同盟城池（城/水寨/军港/民港）：列表、距离排序、跳转选中（校验「调动」）
  mapnav.py       跳转弹窗、定位面板、右侧按钮列 OCR、截图、回到地图
  mobilize.py     同盟大营「调动」：选部队卡、金色出征按钮、兵力读取/白色补兵、到达时间
  battle.py       正面战斗：邮件按钮（集结/攻占/行军）、红色敌对目标检测、等待结束、回城
  home.py         回家补兵：回城 → 亲卫营 → 三队兵力检查/补兵 → 出城回地图
  timing.py       步骤计时器：kind（分析/执行/等待）+ stage（四个节点）+ 汇总
  flow_mail.py    功能一完整流程 + CLI
  flow_mobilize.py 功能二+三完整流程 + CLI（战场 -> 大营 -> 调动 -> 战斗 -> 回城 -> 记录）
  flow_loop.py    **自动拉线**循环：复用 flow_mobilize，逐轮统计节点耗时，写 json/jsonl
```

### 调试工具

```
tools/snap.py             低画质截屏 / 裁剪
tools/flow_probe.py       逐步点击 + 截图 / 区域 OCR
tools/ocr_calib.py        离线区域 OCR 校准（打印每段文字坐标）
tools/locate_probe.py     定位面板逐页探测
tools/map_label_probe.py  地图标签 OCR 探测
tools/fac_parse_probe.py  设施表格 OCR 切块参数对比
tools/red_probe.py        红色敌对目标检测离线标定（--synthetic 自检）
tools/enemy_label_probe.py 地图标签 OCR + 文字像素红色占比（识别敌方红字）
tools/label_color_probe.py 区域颜色采样（校准标签/坐标颜色阈值）
tools/report_jump_probe.py 战报绿色坐标 → 跳转 → 战斗按钮 实时探测
```
