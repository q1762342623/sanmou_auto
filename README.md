# sanmou_auto

## 工程目标

做一个「三谋」（三国谋定天下，`com.bilibili.nslg`）自动化程序。

- 运行载体：MuMu 模拟器 + adb（仅外部截图与模拟触摸，不注入、不读内存、不改包）
- 最终能力：**战报热点驱动的自动拉线**、**战报自动收集**、**自动化战斗**
- 交付形态：功能以可被上层 agent 调用的 skill 形式提供，而不是固定脚本流程

> **邮件链路已彻底删除**：`mail.py` / `flow_mail.py` / `flow_mobilize.py` /
> `flow_loop.py` / `flow_line_report.py`、`battle.BattleFlow` 与
> `skills/sanmou-email-rally/` 均已移除，不存在任何回退开关。
> 战场只来自战报收集的 `coordinateHotspot.hottest`。

## 协作约定

- 本仓库只存放**已验证正确**的功能代码，合入 `main` 前必须在模拟器上实测通过
- 每个功能**单独设计、单独验证**，不预先编写总方案
- 功能清单、接口、坐标与模板随功能一并提交

## 交付形态：Markdown Skill + 原子指令

- [`skills/三谋拉线与战报自动收集/`](../skills/三谋拉线与战报自动收集/)：
  `SKILL.md` + 7 条原子指令（[`commands/`](commands/__init__.py)）+ 调度器
  （[`atomic_loop.py`](atomic_loop.py)）+ 只读汇报（[`../tools/hotspot_status.py`](../tools/hotspot_status.py)）。

## 当前状态

| 功能 | 状态 | 入口 |
| --- | --- | --- |
| **三谋拉线与战报自动收集（原子指令 + 热点驱动）**：据点收集 → 主城坐标 → 战报收集（热点）→ 据点调动 → 补给兵力 → 部队进攻 → 部队撤回，任意两条业务指令之间必插一次完整战报收集 | ✅ 实机验证（2026-11-24/25） | `python -m sanmou_auto.atomic_loop --hours 13` |
| **战报自动收录**：进「战报→同盟→交战」逐卡展开 + 滑到底，设备内 tcpdump → BLOB → 增量上传，并算最高频次交战坐标 | ✅ 实测通过（2026-11-21） | `python skills/战报自动收录/scripts/round.py` |
| **13h 长跑守护**：定时汇报 / 掉线重连 / 异常与卡死重启 | ✅ 实测（`hotspot_supervisor`） | `python tools/hotspot_supervisor.py --spawn --hours 13` |

### 环境依赖

```bash
python -m pip install --no-input rapidocr-onnxruntime
# 已含：onnxruntime / opencv-python / numpy / Pillow(已装)
```

- adb：`C:\Program Files\Netease\MuMu\nx_main\adb.exe`
- 设备：`127.0.0.1:16416`（竖屏 900x1600）

### 快速开始

```bash
python -m sanmou_auto.atomic_loop --specs                    # 7 条指令契约
python -m sanmou_auto.atomic_loop --minutes 30               # 原子指令调度（默认引擎）
python -m sanmou_auto.atomic_loop --hours 13                 # 13 小时长跑
python -m sanmou_auto.atomic_loop --max-ticks 3 --dry-run    # 冒烟（不动手）

python tools/hotspot_supervisor.py --spawn --hours 13        # 后台守护 + 自动汇报
python tools/hotspot_status.py                               # 只读进度（轮次/各指令耗时/战报）
```

截图输出：`screenshots/flow/`（JPEG 宽 1000，单张 60–140KB，避免大图撑爆上下文）

### 模块

```
sanmou_auto/
  config.py       设备/分辨率/固定 UI 坐标（含右侧部队头像 / 回城按钮 / 回城弹窗）
  adb.py          screencap / tap / swipe / keyevent
  vision.py       低画质落盘、裁剪
  ocrengine.py    RapidOCR 封装 + 切块识别 ocr_tiled()
  intent.py       关键字白名单/黑名单 -> 动作，坐标正则
  atomic_loop.py  **SKILL 调度器**：7 条原子指令编排 + 状态落盘 + 汇总 JSON
  commands/       **7 条原子指令**：camps / home / reports / mobilize / replenish / attack / return
  alliance.py     同盟建筑 + 同盟城池（城/水寨/军港/民港）：列表、距离排序、跳转选中（校验「调动」）
  mapnav.py       跳转弹窗、定位面板、右侧按钮列 OCR、回到地图（含部队总览/配将助手自救）
  mobilize.py     同盟大营「调动」：选部队卡、金色出征按钮、兵力读取/白色补兵、到达时间
  march.py        **热点行军/攻城**：OCR 动作列，攻城 > 行军（绝不攻占），灰色时试相邻地块
  battle.py       地图战斗工具：红色敌对目标检测、动作按钮、回城（**不再含邮件流程**）
  home.py         回家补兵：回城 → 亲卫营 → 三队兵力检查/补兵 → 出城回地图
  homecity.py     **我的主城坐标**：定位→建筑 第一条（与「城池」据点严格区分），带缓存
  city_replenish.py **城池补兵**：离主城最近的城池 → 1-4 队全调动（沙盘直跳）
  city_rest.py    低兵力歇兵登记 / 征满召回
  netcamps.py     **抓包解同盟建筑**：tcpdump → XOR/sproto 解码（SANMOU_NETCAMPS=1）
  fastwait.py     页面就绪轮询：截图指纹判「动画结束」，替代固定 sleep
  timing.py       步骤计时器：kind（分析/执行/等待）+ stage + 汇总
  report_collector.py **战报收集调度器**：等待分片、节点安全点、失败补交队列、状态持久化
  hotspot.py      **战报热点**：从收集结果读取 hottest，跨轮跟踪与历史落盘
  troop_return.py **右侧部队头像**：状态/ready 判断、点头像 → OCR 回城 → 「今天登录不再提示」→ 确定
```

### 调试工具

```
tools/snap.py             低画质截屏 / 裁剪
tools/flow_probe.py       逐步点击 + 截图 / 区域 OCR
tools/_hotspot_logic_test.py 热点解析/坐标比较/行军候选顺序 离线自测
tools/ocr_calib.py        离线区域 OCR 校准（打印每段文字坐标）
tools/locate_probe.py     定位面板逐页探测
tools/map_label_probe.py  地图标签 OCR 探测
tools/fac_parse_probe.py  设施表格 OCR 切块参数对比
tools/red_probe.py        红色敌对目标检测离线标定（--synthetic 自检）
tools/enemy_label_probe.py 地图标签 OCR + 文字像素红色占比（识别敌方红字）
tools/label_color_probe.py 区域颜色采样（校准标签/坐标颜色阈值）
tools/report_jump_probe.py 战报绿色坐标 → 跳转 → 战斗按钮 实时探测
tools/pcap_probe.py       tcpdump 探查：--flows/--strings/--packed/--tree/--hex/--capture
tools/_flow_timing_summary.py 汇总历史 auto_line_*.jsonl 的节点/步骤耗时（旧数据）
tools/_camps_bench.py     抓包解据点 vs OCR 扫表 的耗时对比
tools/_homecity_probe.py  定位面板「建筑」第一条（主城）探测
tools/_sandbox_city_probe.py 沙盘坐标直跳 → 选中城池 → 右侧「调动」按钮 验证
tools/_city_dispatch_probe.py 城池候选（按距主城）与沙盘可调动性探测
```
