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
- 「连战 N 场」卡右侧有一条**金色竖条**（中间是场次数字，底部是双 V 箭头）；
  实测位置 `x 1540–1585`（宽 45）、高度 `193~218`（被页签行/屏幕底边裁切时会变短）。
- 点击点 = **竖条水平中心 + 距底边 26px**（双 V 箭头处），实测 `(1562, 727)`。
  竖条本身整条都可点（数字处也能展开），但**卡片本体一点就进「战果」详情页**，
  所以点击点绝不能超出竖条矩形。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `expand.roiX1/X2` | 0.90 / 1.00 | 必须到屏幕右边缘；上游默认 0.975 会把 45px 竖条裁掉一半导致检不到 |
| `expand.roiY1/Y2` | 0.08 / 0.96 | 纵向搜索范围 |
| `expand.rightEdgeRatio` | 0.945 | 竖条左沿必须在 x≥1512；卡片自己的金框在 x≈1440，否则会被当成展开条去点 |
| `expand.templateScale` | 0.9 | 箭头模板缩放；0.8/1.0 都检不到 |
| `expand.downThreshold` | 0.85 | 实测折叠态 down=0.965、up=0.539；半截卡 down=0.463 必须排除 |
| `expand.minMargin` | 0.10 | down 与 up 的分差下限 |
| `expand.minWidth/maxWidth` | 14 / 56 | 竖条实测宽 45；更宽的都是卡片场次框 |
| `expand.minHeight/maxHeight` | 150 / 240 | 竖条实测 193~218；<150 是被裁切的半截卡，>240 是误检的大框 |
| `expand.clickBottomInset` | 26 | 点击点距竖条底边的像素（双 V 箭头中心） |
| `expand.bottomSafeRatio` | 0.955 | 点击点低于 0.955 屏高才先上滑（实测 y=824/826 点击有效，门槛过严会反复滑不动打转） |
| `expand.cutRatio` | 0.97 | `y+h` 超过 0.97 屏高（真的被屏幕截断）时先上滑；滑完仍不动就把这条加入跳过，避免死循环 |
| `expand.maxPerPage` | 6 | 每屏最多点开次数（防抖/防死循环） |
| `expand.maxRecoveriesPerPage` | 2 | 本页最多容忍几次「误入详情 → 回退」，超过就放弃本页 |

### 2.1 必须先等列表静止（本项目最大的一次踩坑）

`input swipe` 命令结束后，列表**还会因为图片懒加载/惯性继续往上顶 1.3~1.6s**。
实测一次滑动后竖条 y 坐标：`0.7s→363`、`1.0s→292`、`1.3s→285`、`1.6s→284`。
若用 0.7s 那一帧算出的坐标去点，落点会整体偏下 **40~90px**，正好压在卡片本体上 ——
表现就是「没有展开，反而点进了战报详情页」（旧版一轮 22 屏出现 14 次）。

因此 `capture.wait_still()` 会一直截到「连续两帧几乎一致」才取坐标：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `capture.stableTimeoutMs` | 2600 | 等静止的最长时间 |
| `capture.stableIntervalMs` | 180 | 静止检测采样间隔 |
| `capture.stableDiffMean` | 0.9 | 两帧平均差上限（静止实测≈0.05） |
| `capture.stableChangedRatio` | 0.002 | 两帧变化像素比例上限（静止≈0.000） |

展开成功的确认方式：**等静止后**同一位置不再出现 `down` 竖条（变成 `up` 或整条消失）；
页面不再是「交战列表」（页签行消失）说明误入详情页，自动点左上返回并记一次 failure。
点开后列表会向上顶（实测约 34~64px），所以 `_find_same()` 允许纵向平移 48px 再匹配。
**只有「画面完全没变」才允许补点一次**，且必须用静止后的新坐标 —— 绝不盲点第二次。

## 3. 滑动

| 配置项 | 值 | 说明 |
| --- | --- | --- |
| `targets.swipe.x` | 0.36（576px） | 从卡片中部拖动；实测该轨道能正常滚动列表 |
| `y1 → y2` | 0.78 → 0.515（702 → 464，手势 238px） | **实测含惯性只滚 ≈365px**：必须满足 `步长 < 视口(700) - 卡高(270) = 430px`，否则中间的卡会被整屏跨过去（旧值 0.18 = 486px 手势，实际约 700px，会漏卡） |
| `durationMs` | 760 | 手势别太短（甩动大）、也别太长（>1000ms 会被游戏当成点击，直接进详情页） |
| `capture.swipeRetryY1/Y2` | 0.84 / 0.60（216px） | 第一次滑动没生效时的第二次手势（起点更低，同样小步长，避免漏卡） |
| `capture.swipeRetryDurationMs` | 800 | 第二次手势也不超过 1000ms |
| `capture.maxPages` | 0 | 0 = 不设页数上限，必须滑到列表真正到底 |
| `capture.hardMaxPages` | 500 | 仅防界面异常死循环 |
| `capture.stalledPagesToStop` | 2 | 连续两屏滑不动 = 到底（每屏内部已含一次换手势重试） |
| `capture.stalledProbeDelayMs` | 1500 | 判“滑不动”前再等 1.5s 复检，排除列表仍在加载 |
| `capture.requireListEnd` | true | production 没到底 → 写 `incomplete.json`，保留但不提取/不上传 |

**手势会被吞掉**：同一屏连续滑 40 次里实测有 4 次「一点没动」（mean≈0.05），
换个起点/时长立刻又能滑（mean≈40）。所以 `swipe_page()` 在判「到底」前一定会用
`swipeRetryY1/Y2` 再滑一次，然后再等 `stalledProbeDelayMs` 复检 ——
一次没动就判 end 会漏掉大量战报（旧版一轮 22 屏里有 2 次是假到底）。
滑动后同样要 `wait_still()` 才做位移判断，否则会拿半途的帧比较。

「移动了一屏」= `visualDiffMean ≥ 3.0` 且 `changedRatio ≥ 0.03`。
实测一次正常滑动：`mean≈40`、`changedRatio≈0.50`。

**为什么步长必须 < 视口 − 卡高**：整轮是「展开当前屏 → 小步滑 → 再展开」。
视口约 700px、卡高 216~270px，所以步长必须 < ~430px：
相邻两屏的内容窗口才不会出现空档，任何一张卡都会在某一屏里**完整出现**并被展开。
旧配置（0.72→0.18，手势 486px）实测一步滚了 ~700px，正好等于视口高度，中间的卡成片漏掉 ——
这就是「战报没打开 / 被跳过」的直接原因。

2026-11-21 20:50 实测（`0.78→0.515 / 760ms`）：**实测步长稳定在 -362~-383px**
（`pageLog.shiftPx`，42 屏只有 4 次因列表自动刷新出现 -105/+255 之类的小偏差），
40 屏有效滑动 + 2 次到底复检，432s，`expandedCards 19`（旧大步长同样内容只有 5~12 张），
`pageLog` 每屏 `leftoverDowns` 都是 0、`expandFailures=0`、`navigationsRecovered=0`、
`stopReason:"list_end"`、`complete:true`，42 张 `*_expanded` 截图全部仍在「交战」页。

`capture_result.json` 的 `pageLog` 每项含
`strips / downsSeen / opened / skipped / leftoverDowns / moved / swipeAttempts / shiftPx`，
排查「漏展开」直接看 `leftoverDowns`（应为 0）与 `shiftPx`（应 < -430）即可。
截图命名也改成「干净帧」：`screen_page_XX_expanded.png` 是展开后的样子、
`screen_page_XX_after_swipe.png` 是滑动后的样子，工作截图只写 `work_current.png`。

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
3. **误入「战果」详情页**（页面标题仍是「战报」、但页签行消失、无金色竖条）：
   根因不是「半截竖条」，而是**拿还没停稳的截图算点击坐标** —— 滑动后列表会继续
   上顶 1.3~1.6s，坐标偏下 40~90px 就压到卡片本体。现在 `wait_still()` 等静止再算，
   且点击点固定在竖条内（`clickBottomInset`）；检测到 `reportPage/combatTabSelected`
   变 false 会自动点左上返回并计一次 failure（正常应为 0）；
4. 滑动轨道如果在竖条上起手会触发卡片点击，必须走卡片中部（x≈0.36）；
5. 该版 adb 在**含中文目录下新建文件**会报 `cannot create file/directory`，
   `pull` 前先 `touch` 空文件即可（`common.Adb.pull` 已处理）；
6. 卡片自己的金色边框在 x≈1440、高 150~170，曾被当成展开条去点（直接进详情页）；
   现在 `expand.rightEdgeRatio=0.945` + `maxWidth/maxHeight` 会把它排除。

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
python scripts/capture.py --kind diagnostic --max-seconds 240   # 不设页数上限，跑到 list_end
python scripts/extract.py --capture-dir captures/<captureId>   # 期望 uniqueBlobs > 0
```
