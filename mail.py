"""邮件（信件）读取：打开邮件页 -> 选页签 -> 逐条点开 -> OCR 正文。

实测要点（plans/UI功能点实测.md 第 9 节 + 本轮实测）：
- 邮件页左侧列表 5 条可见，条目点击中心 y = 223 + 130*i
- 右侧正文标题 y≈210、发送人行 y≈276、正文从 y≈300 开始
- 正文中的坐标是**可点击**的绿色文字，点击弹「是否跳转到坐标(x,y)」
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from PIL import Image

from . import config, ocrengine, vision
from .adb import Adb
from .intent import MailIntent, parse_mail

COORD_RES = [
    re.compile(r"[（(]\s*(\d{2,4})\s*[,，:]\s*(\d{2,4})\s*[)）]"),
    re.compile(r"(\d{3})\s*[,，]\s*(\d{3})"),
]

SENDER_HINTS = ("盟主", "副盟主", "成员", "官员", "指挥", "太守")


@dataclass
class MailBody:
    """一封邮件的内容 + 在列表中的位置 + 解析出的意图。"""
    title: str
    sender: str
    lines: list[str] = field(default_factory=list)
    coord_point: tuple[int, int] | None = None   # 正文中坐标文字的屏幕坐标
    intent: MailIntent | None = None
    tab: str = ""
    page: int = 0
    slot: int = 0

    @property
    def body(self) -> str:
        return "\n".join(self.lines)


def find_coord_span(img: Image.Image, box: tuple[int, int, int, int], rows=None):
    """在指定区域找坐标文字片段，返回 (坐标, 屏幕点击点)。

    rows: 可选，复用调用方已做的 OCR 结果（坐标为相对 box），避免同一区域识别两遍。
    """
    if rows is None:
        rows = ocrengine.ocr_rows(vision.crop(img, box), config.MAIL_COORD_SCALE)
    best = None
    for row in rows:
        text = "".join(s.text for s in row)
        for pat in COORD_RES:
            m = pat.search(text)
            if not m:
                continue
            x, y = int(m.group(1)), int(m.group(2))
            if not (1 <= x <= 1200 and 1 <= y <= 1200):
                continue
            # 用命中片段的中心作为点击点（优先取包含括号的那个片段）
            span = next((s for s in row if pat.search(s.text)), row[0])
            point = (int(box[0] + span.cx), int(box[1] + row[0].cy))
            if best is None or len(point) and span.score > best[2].score:
                best = ((x, y), point, span)
    if best is None:
        return None, None
    return best[0], best[1]


class MailReader:
    def __init__(self, adb: Adb | None = None, log=print):
        self.adb = adb or Adb()
        self.log = log

    # -- 页面状态 ---------------------------------------------------------
    def is_mail_page(self, img: Image.Image | None = None) -> bool:
        img = img or self.adb.screencap()
        box = config._p((0, 70, 1600, 150), (0, 0, 420, 72))
        texts = ocrengine.ocr_text(vision.crop(img, box), 2.0)
        return "信件" in texts or "同盟" in texts

    def clear_jump_dialog(self, img: Image.Image | None = None) -> bool:
        """「是否跳转到坐标」弹框会吃掉所有点击（连邮件图标都点不开）。

        检测到就点「确定」把它关掉。返回是否真的点过。
        """
        img = img or self.adb.screencap()
        box = config.JUMP_DIALOG_BOX
        scale = 1.6 if config.PORTRAIT else 2.0
        spans = ocrengine.ocr(vision.crop(img, box), scale)
        flat = "".join(s.text for s in spans)
        if "跳转到坐标" not in flat and "是否跳转" not in flat:
            return False
        point = config.CONFIRM_JUMP
        for s in spans:
            if s.text.replace(" ", "").strip() == "确定":
                point = (int(box[0] + s.cx), int(box[1] + s.cy))
                break
        self.log(f"  ! 弹框「是否跳转到坐标」→ 先点「确定」@{point}")
        self.adb.tap(*point)
        time.sleep(1.4)
        return True

    def open(self, wait: float = 2.0, retries: int = 2) -> bool:
        # 已经在邮件页就不要重复点图标（图标是开关，会关掉页面）
        img = self.adb.screencap()
        if self.is_mail_page(img):
            return True
        if self.clear_jump_dialog(img):
            img = self.adb.screencap()
            if self.is_mail_page(img):
                return True
        for i in range(retries + 1):
            self.adb.tap(*config.MAIL_ICON)
            time.sleep(wait)
            img = self.adb.screencap()
            if self.is_mail_page(img):
                return True
            # 点击可能触发了「是否跳转到坐标」弹框（点在坐标文字上），关掉再重试
            if self.clear_jump_dialog(img):
                time.sleep(wait)
                img = self.adb.screencap()
                if self.is_mail_page(img):
                    return True
            self.log(f"  邮件页未打开，重试 {i + 1}/{retries}")
        return False

    def back(self, wait: float = 1.5) -> None:
        self.adb.tap(*config.MAIL_BACK)
        time.sleep(wait)

    def select_tab(self, name: str, wait: float = 1.4) -> None:
        x = config.MAIL_TABS.get(name)
        if x is None:
            raise KeyError(f"未知页签: {name}（可选 {list(config.MAIL_TABS)}）")
        self.adb.tap(x, config.MAIL_TAB_Y)
        time.sleep(wait)

    def tap_slot(self, slot: int, wait: float = 1.2) -> None:
        y = config.MAIL_ITEM_Y0 + config.MAIL_ITEM_DY * slot
        self.adb.tap(config.MAIL_LIST_X, y)
        time.sleep(wait)

    def scroll_list(self, wait: float = 1.2) -> None:
        x1, y1, x2, y2 = config.MAIL_LIST_SCROLL
        self.adb.swipe(x1, y1, x2, y2, 450)
        time.sleep(wait)

    def scroll_to_top(self, times: int = 5, wait: float = 0.7) -> None:
        """邮件列表会记住上次的滚动位置，读取前必须回滚到顶部。"""
        x1, y1, x2, y2 = config.MAIL_LIST_SCROLL_DOWN
        for _ in range(times):
            self.adb.swipe(x1, y1, x2, y2, 380)
            time.sleep(wait)

    # -- 正文 -------------------------------------------------------------
    def read_body(self, img: Image.Image | None = None) -> MailBody:
        img = img or self.adb.screencap()
        box = config.MAIL_BODY_BOX
        rows = ocrengine.ocr_rows(vision.crop(img, box), config.MAIL_OCR_SCALE)
        title_end, sender_end = config.MAIL_BODY_SPLIT

        title, sender, body_lines = "", "", []
        for row in rows:
            text = "".join(s.text for s in row).strip()
            if not text:
                continue
            y = box[1] + row[0].cy
            if y < title_end and not title:
                title = text
            elif title_end <= y < sender_end and not sender:
                sender = text
            elif y >= sender_end:
                body_lines.append(text)

        coord, point = find_coord_span(img, box, rows)
        intent = parse_mail(title, sender, "\n".join(body_lines))
        return MailBody(title=title, sender=sender, lines=body_lines,
                        coord_point=point, intent=intent)

    # -- 批量读取 ---------------------------------------------------------
    def read_latest(self, limit: int = 10, tabs: tuple[str, ...] = ("同盟",),
                    stop_on_first_actionable: bool = False) -> list[MailBody]:
        """读最近 limit 封邮件（按页签顺序、列表从上到下）。

        返回的 MailBody 带 tab/page/slot，可再次用 open_mail_at() 定位。
        """
        if not self.open():
            raise RuntimeError("无法打开邮件页")

        mails: list[MailBody] = []
        seen: set[tuple[str, str]] = set()

        for tab in tabs:
            self.select_tab(tab)
            # 列表会记住上次滚动位置：先回顶，否则会漏掉最新邮件
            self.scroll_to_top()
            stalled_pages = 0
            for page in range(6):
                new_this_page = 0
                for slot in range(config.MAIL_ITEMS_VISIBLE):
                    if len(mails) >= limit:
                        break
                    self.tap_slot(slot)
                    body = self.read_body()
                    # 同标题的不同邮件（如两封「集合」）坐标不同，不能一刀切去重
                    key = (tab, body.title,
                           body.intent.coord if body.intent else None)
                    if not body.title or key in seen:
                        continue
                    seen.add(key)
                    body.tab, body.page, body.slot = tab, page, slot
                    mails.append(body)
                    new_this_page += 1
                    self.log(f"  [{len(mails):02d}] {tab} {body.title} | "
                             f"{body.intent.describe()}")
                    if stop_on_first_actionable and body.intent.actionable:
                        return mails
                if len(mails) >= limit:
                    break
                if new_this_page == 0:
                    stalled_pages += 1
                    if stalled_pages >= 1:
                        break
                self.scroll_list()
        return mails

    @staticmethod
    def _same_mail(a: MailBody, b: MailBody) -> bool:
        if not b.title:
            return False
        if a.title and (a.title in b.title or b.title in a.title):
            return True
        # 新邮件到达会让列表整体位移，用「坐标 + 正文」二次确认
        return bool(a.intent and b.intent
                    and a.intent.coord and a.intent.coord == b.intent.coord
                    and a.body == b.body)

    def tap_and_read(self, slot: int) -> MailBody:
        self.tap_slot(slot)
        return self.read_body()

    def open_slot(self, tab: str, page: int = 0, slot: int = 0) -> MailBody:
        """按「页签 / 页 / 槽位」直接打开邮件（不做标题校验，供上层原子脚本用）。"""
        if not self.open():
            raise RuntimeError("无法打开邮件页")
        self.select_tab(tab)
        self.scroll_to_top()
        for _ in range(page):
            self.scroll_list()
        body = self.tap_and_read(slot)
        body.tab, body.page, body.slot = tab, page, slot
        return body

    def open_mail_at(self, mail: MailBody) -> MailBody:
        """重新定位到指定邮件并返回其正文（含坐标点击点）。

        列表会因新邮件到达而整体下移，所以点开后要校验，必要时在本页
        其它槽位里找回同一封。
        """
        if not self.open():
            raise RuntimeError("无法打开邮件页")
        self.select_tab(mail.tab)
        self.scroll_to_top()
        for _ in range(mail.page):
            self.scroll_list()
        body = self.tap_and_read(mail.slot)
        if self._same_mail(mail, body):
            return body

        self.log(f"  ! 邮件位置变化（期望「{mail.title}」实际「{body.title}」），"
                 f"本页重新查找")
        for slot in range(config.MAIL_ITEMS_VISIBLE):
            if slot == mail.slot:
                continue
            cand = self.tap_and_read(slot)
            if self._same_mail(mail, cand):
                self.log(f"  · 在槽位 {slot} 找回「{cand.title}」")
                return cand
        self.log("  ! 本页未找到目标邮件，沿用当前正文")
        return body
