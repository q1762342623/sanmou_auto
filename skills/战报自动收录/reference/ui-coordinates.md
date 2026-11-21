# 1600x900 横屏 UI 坐标与判定阈值（实测）

所有坐标存成**屏幕比例**（`config.json → targets`），像素 = `round(比例 × 截图宽/高)`。
下表同时给出 1600x900 实测算得的像素，方便肉眼核对。

## 1. 入口与页签

| 名称 | 比例 | 1600x900 像素 | 说明 |
| --- | --- | --- | --- |
| `reportEntry` | (0.215, 0.814) | (344, 733) | 主界面/城内左下「战报」入口（D5）。实测点 (363,743) 也能进；左侧 `(201,743)` 是「邮件」，**不要点错** |
| `allianceTab` | (0.769, 0.118) | (1230, 106) | 战报页「同盟」页签（与「个人」互斥） |
| `combatTab` | (0.3375, 0.118) | (540, 106) | 战报页「交战」页签 |
| `pageBack` | (0.056, 0.028) | (90, 25) | 全屏页左上返回箭头；**替代 Android BACK** |
| `tabBand` | (0.0, 0.075, 1.0, 0.165) | y 67–148 | 页签判定带（OCR + 金色边框统计） |

实测页签行（y≈106，整行 OCR 会合并成一串「全部开拓交战收藏个人同照一键阅读」）：

| 页签 | 中心 x（像素） | x 比例区间 |
| --- | --- | --- |
| 全部 | 145 | 0.020–0.190 |
| 开拓 | 355 | 0.200–0.310 |
| 交战 | 540 | 0.320–0.430 |
| 收藏 | 815 | 0.440–0.580 |
| 个人 | 1070 | 0.620–0.740 |
| 同盟 | 1230 | 0.740–0.860 |

**选中判定**：该页签窗口内的金色边框比例。实测选中「全部」=0.1496、选中「交战」=0.1843，
未选中页签接近 0（「个人」因红点约 0.028）。阈值 `TAB_SELECTED_GOLD=0.05`。
页签行在城内视角也会是金色（建筑/顶栏），所以必须**同时**满足 OCR 出现「交战/收藏/开拓」
才认定在战报页——纯颜色阈值会误报。

## 2. 列表与展开条

- 每屏约 3 张整卡，卡头 y≈140 / 412 / 684，纵向间距约 272px。
- 「连战 N 场」卡右侧有一条**金色竖条**，底部是展开箭头；实测位置
  `x 1540–1585`（宽 45）、完整高度约 `216`、半截卡约 `121`。
- 点击点 = 竖条底部箭头：`(x + w/2, y + h - 28)`，实测 `(1562, 727)`。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `expand.roiX1/X2` | 0.90 / 1.00 | 必须到屏幕右边缘；上游默认 0.975 会把 45px 竖条裁掉一半导致检不到 |
| `expand.roiY1/Y2` | 0.08 / 0.96 | 纵向搜索范围 |
| `expand.templateScale` | 0.9 | 箭头模板缩放；0.8/1.0 都检不到 |
| `expand.downThreshold` | 0.85 | 实测折叠态 down=0.965、up=0.539；半截卡 down=0.463 必须排除 |
| `expand.minMargin` | 0.10 | down 与 up 的分差下限 |
| `expand.minWidth/maxWidth` | 14 / 90 | 竖条宽度范围 |
| `expand.minHeight` | 150 | 完整竖条约 216px；半截卡约 121px，排除 |
| `expand.maxPerPage` | 6 | 每屏最多点开次数（防抖/防死循环） |

展开成功的确认方式：同一位置不再出现 `down` 竖条（变成 `up` 或整条消失）。
点开后列表会向上顶，**同一屏可能露出下一张半截卡** —— 半截卡高度不足 `minHeight`，
不会被再次点击，避免误点进「战果」详情页。

## 3. 滑动

| 配置项 | 值 | 说明 |
| --- | --- | --- |
| `targets.swipe.x` | 0.36（576px） | 从卡片中部拖动；实测该轨道能正常滚动列表 |
| `y1 → y2` | 0.72 → 0.39（648 → 351） | 从下往上拖，看更旧的战报 |
| `durationMs` | 700 | 太快会惯性甩动跳过卡片 |
| `capture.stalledPagesToStop` | 2 | 连续两页画面无变化 = 到底 |

「移动了一页」= `visualDiffMean ≥ 3.0` 且 `changedRatio ≥ 0.03`。
实测一次正常滑动：`mean≈40`、`changedRatio≈0.50`。

## 4. 状态判定接口

`python scripts/screen.py state` 输出：

```json
{"reportPage":true,"selectedTab":"交战","combatTabSelected":true,
 "tabGold":{"全部":0.0,"开拓":0.0511,"交战":0.1857,"收藏":0.0,"个人":0.0281,"同盟":0.0},
 "tabOcrText":"全部开拓交战个人同照","blockingOverlay":false,"dialogOverlay":false}
```

- `reportPage`：OCR 命中页签词 ⇒ 在战报页
- `combatTabSelected`：选中页签 == 交战
- `blockingOverlay` / `dialogOverlay`：来自 `inspect_mumu_ui`（中央模态/亮色对话框）

## 5. 已知坑（实测）

1. **城内视角会被上游视觉误判成战报页**（金色顶栏/建筑），所以加了 OCR 硬门槛；
2. **已停在战报页时直接抓包 = 0 流量**：列表不会重新请求。`capture.forceReload=true`
   会先点左上返回、再点战报入口重新进列表，首屏响应才有战报帧；
3. 点开「连战」卡后，第二次盲点半截竖条会点进「战果」详情页（页面标题仍是「战报」、
   但页签行消失）。检测到 `reportPage=false` 时脚本会自动点左上返回；
4. 滑动轨道如果在竖条上起手会触发卡片点击，必须走卡片中部（x≈0.36）；
5. 该版 adb 在**含中文目录下新建文件**会报 `cannot create file/directory`，
   `pull` 前先 `touch` 空文件即可（`common.Adb.pull` 已处理）。

## 6. 竖屏 / 其他分辨率

上游 portrait（900x1600）比例：

```text
report   (0.435, 0.839)    alliance (0.620, 0.963)    combat (0.612, 0.905)
swipe    (0.360, 0.720) -> (0.360, 0.390)
```

横竖屏是**重排**不是旋转，不能机械换算。换分辨率时按 §7 重新校准。

## 7. 校准流程（3 分钟）

```bash
python scripts/screen.py preview          # work/preview_*_targets.png：十字准星 + 滑动箭头 + 页签带
# 改 config.json 的 targets（比例），再 preview 一次
python scripts/screen.py state            # 进列表后确认 reportPage / combatTabSelected
python scripts/capture.py --kind diagnostic --max-pages 1 --max-seconds 120
python scripts/extract.py --capture-dir captures/<captureId>   # 期望 uniqueBlobs > 0
```
