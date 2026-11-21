"""邮件意图解析：标题/正文关键字 -> 动作 + 目标坐标。

关键字规则（用户定义）：
1. 集结 / 保护      -> 行军（MARCH）到该坐标
2. 蹲坑            -> 驻守（GARRISON）到该目标
3. 占领 / 攻占 / 梭哈 -> 占领/攻城（OCCUPY）；若当前无法攻占则行军过去
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config

# 动作
MARCH = "march"          # 行军
GARRISON = "garrison"    # 驻守
OCCUPY = "occupy"        # 占领/攻城（不可攻占时降级为行军）

# 黑名单：命中即整条跳过（实测存在「有虫子」这类不应执行的指令）
BLACKLIST_KEYWORDS: tuple[str, ...] = ("虫子",)

# 关键字 -> 动作。顺序即优先级（先命中者生效，越具体越靠前）
# 括号里同时收录了实测中出现的 OCR 变体（如「蹲坑」常被识别成「会坑/跨会坑」）
KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    (("蹲坑", "蹲", "蹲守", "驻守", "会坑", "跨会坑"), GARRISON),
    (("占领", "攻占", "梭哈", "攻城", "攻打", "占了"), OCCUPY),
    (("集结", "保护", "集合", "拉线", "支援", "拉了"), MARCH),
]

ACTION_LABEL = {
    MARCH: "行军",
    GARRISON: "驻守",
    OCCUPY: "占领/攻城",
}

# 注意：OCR 会把逗号识别成 . 、。 等，全部纳入分隔符
_SEP = r"[,，.、:：]"
_COORD_PATTERNS = [
    re.compile(rf"[（(]\s*(\d{{2,4}})\s*{_SEP}\s*(\d{{2,4}})\s*[)）]"),
    re.compile(rf"坐标\s*[：:]?\s*(\d{{2,4}})\s*{_SEP}\s*(\d{{2,4}})"),
    re.compile(rf"(?<![\d])(\d{{3}})\s*{_SEP}\s*(\d{{3}})(?![\d])"),
]


@dataclass
class MailIntent:
    """一封邮件解析后的指令。"""
    title: str
    sender: str
    body: str
    coord: tuple[int, int] | None = None
    action: str | None = None
    keyword: str = ""
    blacklisted: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return (self.action is not None and self.coord is not None
                and not self.blacklisted)

    def describe(self) -> str:
        act = ACTION_LABEL.get(self.action or "", "无")
        pos = f"({self.coord[0]},{self.coord[1]})" if self.coord else "无坐标"
        kw = f"命中「{self.keyword}」" if self.keyword else "无关键字"
        if self.blacklisted:
            act, kw = "黑名单", "跳过"
        return f"[{act}] {self.title} -> {pos} {kw}"


def parse_coord(text: str) -> tuple[int, int] | None:
    for pat in _COORD_PATTERNS:
        m = pat.search(text)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            if 1 <= x <= 1200 and 1 <= y <= 1200:
                return x, y
    return None


def parse_action(text: str) -> tuple[str | None, str]:
    for keywords, action in KEYWORD_RULES:
        for kw in keywords:
            if kw in text:
                return action, kw
    return None, ""


def parse_mail(title: str, sender: str = "", body: str = "") -> MailIntent:
    text = f"{title}\n{body}"
    coord = parse_coord(body) or parse_coord(title)
    hit = next((b for b in BLACKLIST_KEYWORDS if b in text), "")
    if hit:
        return MailIntent(title=title.strip(), sender=sender.strip(),
                          body=body.strip(), coord=coord, action=None,
                          keyword="", blacklisted=True,
                          notes=[f"命中黑名单「{hit}」，跳过"])

    action, kw = parse_action(text)
    intent = MailIntent(title=title.strip(), sender=sender.strip(),
                        body=body.strip(), coord=coord,
                        action=action, keyword=kw)
    if intent.coord is None:
        intent.notes.append("未解析到坐标")
    if intent.action is None:
        intent.notes.append("未命中关键字")
    return intent


def plan_from_intent(intent: MailIntent) -> list[dict]:
    """把意图展开为可执行的点击步骤描述（供上层 agent 使用）。

    占领/攻城在无法攻占时需要降级：先尝试占领按钮，失败则改走行军。
    """
    if not intent.actionable:
        return []
    x, y = intent.coord
    steps = [{"op": "jump_to_coord", "x": x, "y": y}]
    if intent.action == OCCUPY:
        steps.append({"op": "occupy_or_march", "x": x, "y": y,
                      "fallback": MARCH})
    elif intent.action == GARRISON:
        steps.append({"op": "garrison", "x": x, "y": y})
    else:
        steps.append({"op": "march", "x": x, "y": y})
    return steps


def reload_rules() -> None:  # pragma: no cover - 预留配置热加载
    config.__dict__.setdefault("_noop", None)
