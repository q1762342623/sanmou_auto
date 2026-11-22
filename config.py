"""全局配置：设备、分辨率、固定 UI 坐标（横屏 / 竖屏两套）。

坐标系说明
----------
- 横屏（landscape）：1600x900，adb 物理触摸坐标。
- 竖屏（portrait）：900x1600。游戏竖屏是**重排布局**，不是旋转，
  因此两套坐标必须分别实测，不能互相换算。
- 本模块在导入时确定当前方向（`PORTRAIT`），并用 `_p(横屏值, 竖屏值)`
  选择实际使用的常量。业务模块继续引用原常量名即可，
  外部脚本可用 `python tools/portrait_probe.py` 复测竖屏点位。

方向判定优先级：
1. 环境变量 `SANMOU_ORIENT=portrait|landscape`（强制，便于离线测试）
2. 缓存文件 `screenshots/flow/_orientation.json`（由探测/启动脚本写入）
3. adb `wm size` 实时探测（失败则回退横屏，保持旧行为）
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ADB_PATH = r"C:\Program Files\Netease\MuMu\nx_main\adb.exe"
DEVICE = "127.0.0.1:16416"
PKG = "com.bilibili.nslg"

SCREEN_LANDSCAPE = (1600, 900)
SCREEN_PORTRAIT = (900, 1600)

_ROOT = Path(__file__).resolve().parents[1]
_ORIENT_CACHE = _ROOT / "screenshots" / "flow" / "_orientation.json"


def _detect_orientation() -> str:
    forced = (os.environ.get("SANMOU_ORIENT") or "").strip().lower()
    if forced in ("portrait", "landscape"):
        return forced
    try:
        data = json.loads(_ORIENT_CACHE.read_text(encoding="utf-8"))
        value = str(data.get("orientation") or "").lower()
        if value in ("portrait", "landscape"):
            return value
    except Exception:          # noqa: BLE001 - 缓存缺失/损坏都走实时探测
        pass
    try:
        out = subprocess.run(
            [ADB_PATH, "-s", DEVICE, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=6,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
        for token in out.replace("Physical size:", " ").split():
            if "x" in token:
                w, _, h = token.partition("x")
                if w.isdigit() and h.isdigit():
                    return "portrait" if int(h) > int(w) else "landscape"
    except Exception:          # noqa: BLE001
        pass
    return "landscape"


ORIENTATION = _detect_orientation()
PORTRAIT = ORIENTATION == "portrait"

# 当前设备逻辑分辨率
SCREEN = SCREEN_PORTRAIT if PORTRAIT else SCREEN_LANDSCAPE


def _p(landscape, portrait):
    """按当前方向取坐标 / 区域。"""
    return portrait if PORTRAIT else landscape


def px(value: float) -> int:
    """把「当前方向基准坐标」换算到截图实际宽度（截图被缩放时使用）。"""
    return int(round(value))


def size_scale(img_width: int) -> float:
    """截图宽度 / 当前方向基准宽度。业务代码用它裁剪 OCR 区域。"""
    return img_width / float(SCREEN[0])


# ---------------------------------------------------------------------------
# 邮件（信件）页
# ---------------------------------------------------------------------------
MAIL_ICON = _p((201, 743), (185, 1317))          # 主界面左下「邮件」

# 二级页「◀ 返回」真实位置：横屏左上角、竖屏底部左侧。
# 注意（2026-11-22 实测事故）：竖屏 (115,1548) 在地图上并不存在「返回」，
# 那个位置是底栏「武将」按钮 —— 盲点会打开「武将库」，导致既回不了地图、
# 也点不到邮件，整轮空转。所以只有 OCR 真的看到「返回」时才允许点这个坐标
# （见 MapNav.back_button_visible / ensure_map）。
MAIL_BACK = _p((100, 25), (110, 1512))

# 底栏「武将 / 同盟 / 征战」条（判断是否在世界地图）
MAP_BOTTOM_SLOT = _p((600, 780, 1460, 890), (0, 1490, 900, 1570))
# 地图空白处（用来取消选中地块 / 收起右侧动作列；避开据点标签与底栏）
MAP_BLANK_POINT = _p((800, 200), (760, 240))
# 「◀ 返回」按钮带（二级页才有；横屏在左上角，竖屏在最底部一行）
BACK_BTN_BOX = _p((0, 0, 150, 60), (0, 1440, 300, 1600))
# 左下「回城 / 出城」按钮带（地图与城内是同一个位置）
# 竖屏实测（2026-11-22 截图）：圆形图标 + 「回城」文字在 (30~95, 1395~1425)，
# 往上 y<1370 是聊天/世界频道文字，不能框进来，否则 OCR 会读成「世界」。
HOME_BTN_BOX = _p((10, 730, 190, 830), (10, 1370, 210, 1445))

# 页签行：法令 / 同盟 / 分组 / 系统 / 公告 / 收藏
MAIL_TAB_Y = _p(112, 1455)
MAIL_TABS = _p(
    {"法令": 131, "同盟": 355, "分组": 581, "系统": 806, "公告": 1032, "收藏": 1264},
    {"法令": 105, "同盟": 305, "分组": 500, "系统": 690, "公告": 800, "收藏": 860},
)

# 列表：首条中心 y、条目步长、可见条数
MAIL_LIST_X = _p(190, 200)
MAIL_ITEM_Y0 = _p(223, 880)
MAIL_ITEM_DY = _p(130, 130)
MAIL_ITEMS_VISIBLE = _p(5, 4)

MAIL_LIST_BOX = _p((10, 110, 300, 780), (10, 800, 890, 1300))
MAIL_LIST_SCROLL = _p((190, 730, 190, 250), (450, 1250, 450, 850))
MAIL_LIST_SCROLL_DOWN = _p((190, 250, 190, 730), (450, 850, 450, 1250))

# 正文区域（竖屏：正文在上、列表在下）
MAIL_BODY_BOX = _p((500, 150, 1600, 770), (10, 60, 890, 700))
# 正文内「标题 / 发件人 / 正文」分段阈值（相对整图 y）：横屏 245/305，竖屏 140/205
MAIL_BODY_SPLIT = _p((245, 305), (140, 205))
# 竖屏字号更大，OCR 倍率可下调（明显提速）
MAIL_OCR_SCALE = _p(2.0, 1.6)
MAIL_COORD_SCALE = _p(2.5, 1.8)

MAIL_COORD_RE = r"[（(]\s*(\d{2,4})\s*[,，]\s*(\d{2,4})\s*[)）]"

# 「一键读取」/「一键删除」
MAIL_READ_ALL = _p((120, 835), (95, 1340))
MAIL_DELETE_ALL = _p((365, 835), (305, 1340))

# 地块/同盟大营右侧竖排操作按钮列
ACTION_BOX = _p((1075, 140, 1215, 500), (735, 555, 900, 945))

# ---------------------------------------------------------------------------
# 地图侧
# ---------------------------------------------------------------------------
MAP_LOCATE_ICON = _p((1510, 125), (790, 208))     # 「定位」
MAP_SEARCH_ICON = _p((1408, 125), (834, 1238))    # 「搜索」
MAP_EYE_ICON = _p((1326, 124), (825, 1110))       # 「视野」

# 主界面左下新建 / 军报（战报）入口
MAP_REPORT_ICON = _p((95, 743), (378, 1317))
MAP_CHAT_ICON = _p((135, 743), (293, 1317))
MAP_HOME_ICON = _p((60, 772), (75, 1440))

# 地图可视区域（去顶栏 / 底栏 / 右侧部队头像列）
MAP_BOX = _p((135, 160, 1440, 700), (0, 280, 890, 1290))
MAP_TILE = (400, 260)
MAP_TILE_OVERLAP = 40

# 定位面板
LOCATE_TAB_Y = _p(181, 304)
LOCATE_TABS = _p(
    {"建筑": 1216, "收藏": 1336, "同盟": 1472},
    {"建筑": 499, "收藏": 646, "同盟": 792},
)
LOCATE_TAB_BOX = _p((1140, 150, 1600, 220), (440, 270, 900, 340))
LOCATE_PANEL_BOX = _p((1140, 205, 1600, 555), (460, 340, 900, 700))
LOCATE_SCROLL = _p((1380, 520, 1380, 280), (700, 660, 700, 380))
LOCATE_SCROLL_DOWN = _p((1380, 280, 1380, 520), (700, 380, 700, 660))
# 点击定位条目时的 x（横屏 1380；竖屏面板在 460..900，取列中心）
LOCATE_ITEM_X = _p(1380, 690)
LOCATE_OCR_SCALE = _p(3.0, 2.0)

# 「是否跳转到坐标(x,y)」弹窗
# 竖屏实测（2026-11-22）：标题「提示」y≈673、正文 y≈800、按钮行 y≈897，
# 即「确定」@(640,897)、「取消」@(259,897)。
# 旧值 (540,845) 落在按钮上方空白 → 点了不生效 → 弹框永远盖住地图，
# 后续所有点击被吃掉（表现为「无法打开邮件页」）。区域放宽到覆盖正文 + 按钮行。
CONFIRM_JUMP = _p((992, 550), (640, 897))
CANCEL_JUMP = _p((608, 550), (259, 897))
JUMP_DIALOG_BOX = _p((520, 300, 1120, 640), (60, 650, 840, 1000))

# ---------------------------------------------------------------------------
# 战报页（军报）
# ---------------------------------------------------------------------------
REPORT_TITLE_BOX = _p((0, 20, 460, 90), (0, 0, 300, 60))
REPORT_TAB_BOX = _p((0, 95, 900, 140), (0, 1415, 900, 1485))
REPORT_TABS = _p(
    {"全部": 120, "开拓": 340, "交战": 560, "收藏": 780},
    {"全部": 127, "开拓": 310, "交战": 505, "收藏": 700},
)
# 底部筛选行：个人 / 同盟 / 一键阅读
REPORT_FILTER_Y = _p(895, 1542)
REPORT_FILTERS = _p(
    {"个人": 300, "同盟": 520, "一键阅读": 1520},
    {"个人": 375, "同盟": 552, "一键阅读": 805},
)
# 卡片：首条头部 y 与步长（竖屏 4 条/屏；交战筛选多一行统计时整体下移约 75）
REPORT_CARD_Y0 = _p(140, 101)
REPORT_CARD_DY = _p(272, 375)
REPORT_CARD_MARGIN = _p(0, 75)

# ---------------------------------------------------------------------------
# 截图输出目录
# ---------------------------------------------------------------------------
SHOT_DIR = "screenshots/flow"
