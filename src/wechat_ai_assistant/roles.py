"""整理角色：同一个"智能整理"按钮，按选的角色用不同的整理逻辑。

## 为什么是角色而不是改名

工具要从"只给微商接单"变成多用途，改名是最浅的一层 —— 真正不同的是
"看到一堆群消息时要产出什么"：订单助手产出订单草稿，活动助手产出
报名/时间地点/物资归类，通用整理只做话题归纳。角色就是这层"产出定义"。

## 两层实现

* **订单助手**：走原来的订单链路（模型解析 → 规则校验 → 订单草稿），
  本模块只登记，不碰它的逻辑。
* **活动助手 / 通用整理**：本机规则引擎（关键词 + 正则 + 时间地点抽取），
  不调模型、不扣额度、不建单 —— 产出只进人工核对。以后接上模型直连或
  网关新任务类型时，同样的角色接口换引擎即可，调用方不用改。

## 诚实边界

规则引擎认得出"接龙""+1""几点在哪集合"，认不出 sarcasm 和长上下文指代。
拿不准的一律进"闲聊/待看"，不硬归类 —— 归错比没归严重。
非文字消息（图片/语音）直接进"非文字"桶并写明要人工看，不猜内容。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

ROLE_ORDER = "order"
ROLE_ACTIVITY = "activity"
ROLE_GENERIC = "generic"

ALL_ROLES = (ROLE_ORDER, ROLE_ACTIVITY, ROLE_GENERIC)


@dataclass(frozen=True)
class CategoryDef:
    """一个归类桶：命中关键词即进，排前面的优先。"""

    key: str
    label: str
    keywords: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleDef:
    """一个整理角色。order 是占位（走订单老链路），其余走本机规则引擎。"""

    id: str
    name: str
    description: str
    version: str = "1"
    categories: tuple[CategoryDef, ...] = ()
    fallback_label: str = "闲聊"


@dataclass
class RoleItem:
    """一条被归类的消息。facts 是抽出来的结构（时间/地点），没有就空。"""

    message_id: str
    group: str
    sender: str
    content: str
    sent_at: str
    category: str = ""
    category_label: str = ""
    facts: dict = field(default_factory=dict)


@dataclass
class RoleReport:
    """一次整理的完整产出：按桶分组 + 计数头。界面直接照这个画。"""

    role_id: str
    role_name: str
    total: int = 0
    buckets: dict = field(default_factory=dict)  # label -> list[RoleItem]

    def headline(self) -> str:
        parts = [f"{label} {len(items)} 条" for label, items in self.buckets.items()]
        return f"「{self.role_name}」整理 {self.total} 条：" + "；".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "role_name": self.role_name,
            "total": self.total,
            "buckets": {
                label: [vars(item) for item in items]
                for label, items in self.buckets.items()
            },
        }


# ------------------------------------------------------------ 事实抽取


_TIME_PATTERNS = (
    r"(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]?",
    r"(?P<weekday>周[一二三四五六日天]|星期[一二三四五六日天])",
    r"(?P<relative>今天|明天|后天|大后天)",
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})",
    r"(?P<ampm>上午|下午|晚上|中午)\s*(?P<h2>\d{1,2})\s*点",
)

_PLACE_PATTERNS = (
    r"(?:地点|地址|集合|集合点)[：:]\s*(?P<place>.{2,24})",
    r"在(?P<place2>.{2,12}?)(?:集合|见|碰头|门口|门口见)",
)


def extract_facts(text: str) -> dict[str, str]:
    """从正文里抠时间地点。抠不出就空 dict，不编造。"""
    facts: dict[str, str] = {}
    text = str(text or "")
    times: list[str] = []
    for pattern in _TIME_PATTERNS:
        match = re.search(pattern, text)
        if match:
            times.append(match.group(0).strip())
    if times:
        facts["time"] = " ".join(dict.fromkeys(times))[:60]
    for pattern in _PLACE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            place = next((g for g in match.groups() if g), "")
            place = re.sub(r"\s+", "", place).strip("，。；、")[:24]
            if place:
                facts["place"] = place
                break
    return facts


# ------------------------------------------------------------ 内置角色


def _activity_role() -> RoleDef:
    return RoleDef(
        id=ROLE_ACTIVITY,
        name="活动助手",
        description="义工群/活动群：报名接龙、时间地点、物资需求分开归类",
        version="activity-v1",
        fallback_label="闲聊",
        categories=(
            # 顺序即优先级：接龙最强（报名的动作信号），其次显式通知框定
            # （"通知：周六改周日"首先是一条通知），再到时间地点、物资。
            CategoryDef("signup", "报名接龙",
                        ("接龙", "报名", "参加", "我要去", "算我一个", "算我一份",
                         "接一个", "接两个", "+1", "+2", "+3", "跟上", "同去"),
                        (r"^\s*\+\d", r"接龙.{0,6}第.{1,4}[位个]")),
            CategoryDef("notice", "通知公告",
                        ("通知", "公告", "周知", "请大家", "提醒大家", "请注意",
                         "务必", "请查收"),
                        ()),
            CategoryDef("schedule", "时间地点",
                        ("时间", "地点", "集合", "几点", "地址", "哪天", "周几",
                         "明天", "后天", "上午", "下午", "晚上", "准时", "别迟到"),
                        (r"\d{1,2}月\d{1,2}[日号 ]", r"\d{1,2}[:：]\d{2}",
                         r"周[一二三四五六日天]")),
            CategoryDef("supply", "物资需求",
                        ("需要", "缺", "带上", "带来", "准备", "购买", "租赁", "借",
                         "谁有", "求带", "物资", "道具", "服装", "车辆", "用车"),
                        ()),
        ),
    )


def _generic_role() -> RoleDef:
    return RoleDef(
        id=ROLE_GENERIC,
        name="通用整理",
        description="不预设业务：提问、事项、通知、闲聊四类归纳",
        version="generic-v1",
        fallback_label="闲聊",
        categories=(
            CategoryDef("question", "有人提问",
                        ("请问", "怎么", "如何", "哪里", "哪儿", "谁", "什么",
                         "为什么", "吗？", "呢？", "？", "求助", "求问"),
                        ()),
            CategoryDef("task", "待办事项",
                        ("帮忙", "麻烦", "记得", "别忘", "不要忘", "截止", "之前",
                         "尽快", "今天内", "请", "需要你", "辛苦"),
                        ()),
            CategoryDef("notice", "通知事项",
                        ("通知", "公告", "周知", "提醒", "请大家", "请注意",
                         "转发", "扩散"),
                        ()),
        ),
    )


def _order_role() -> RoleDef:
    return RoleDef(
        id=ROLE_ORDER,
        name="订单助手",
        description="接单群：分析订货意图、商品数量、客户说法和待确认信息",
        version="order-v1",
    )


_ROLES: dict[str, RoleDef] = {role.id: role for role in (_order_role(), _activity_role(), _generic_role())}


def list_roles() -> tuple[RoleDef, ...]:
    """全部内置角色（界面单选用）。顺序即展示顺序。"""
    return tuple(_ROLES[key] for key in ALL_ROLES)


def get_role(role_id: str) -> RoleDef:
    """取角色定义。未知 id 回通用整理，不抛错（界面传错不该崩）。"""
    return _ROLES.get(str(role_id or ""), _ROLES[ROLE_GENERIC])


def analysis_instruction(role_id: str) -> str:
    """Build the analysis instruction from the selected role definition."""
    role = get_role(role_id)
    if role.id == ROLE_ORDER:
        return (
            "你是微信群接单群的订单意向分析助手。你的任务是从聊天记录中找出客户表达的订货需求，"
            "并整理成可供店主核对的明细；你不在系统中创建、确认或发送订单。\n"
            "识别时不要只搜索‘订单/订货/下单’这些词。‘要两箱’‘给我留一些’‘再加三件’‘明天拿’等口语，"
            "只要结合上下文能看出购买或预留意图，也要列为候选；信息不完整时降低置信度并写明待确认项，"
            "不要因为缺少数量、时间或正式下单用语就直接漏掉。单纯询价、闲聊或没有购买意图的内容不列为订单意向。\n"
            "一行代表一个客户的一种商品意向。不同客户、不同商品或无法确认属于同一笔需求的内容不要合并。"
            "客户列填写对应消息的发送人；客户和商品尽量保留原话，不能确认客户时填写‘未识别’。"
            "没有说清的字段填写‘未明确’，不得猜测。数量保留原数值和单位；"
            "时间没有提到就填‘未提及’。\n"
            "置信度只能填写‘高/中/低’，不要编造百分比：高表示购买意图和商品明确；"
            "中表示购买意图明确但部分细节缺失；低表示可能是订货意向但仍需核实。\n"
            "消息行中带有发送时间时，订单意向表的‘消息时间’必须逐字复制支持该意向的消息时间；"
            "若依据多条消息，可列出多个时间，以‘；’分隔。没有可靠来源时间就填‘未定位’，不要猜。\n"
            "只输出下面格式的 Markdown 表格，不写长篇前言、总结或反复声明‘没有订单’。"
            "单元格中的竖线请改写为斜线，避免破坏表格。列名必须保持一致：\n"
            "| 置信度 | 客户 | 商品 | 数量 | 交付时间 | 待确认项 | 原话依据 | 消息时间 |\n"
            "|---|---|---|---|---|---|---|\n"
            "如果确实没有任何可识别的订货意向，只输出一句：未发现可识别的订货意向。"
        )
    return (
        f"你是「{role.name}」。角色说明：{role.description}。\n"
        "请依据这个角色的职责，分析下面给出的微信群消息，并输出清晰、具体的分析结果。"
        "保留发送人、原话中的数量与时间；不确定的信息明确标注，不要猜测。"
        "这是分析报告，不要生成订单 JSON、订单草稿或执行任何业务操作。"
    )


def order_report_merge_instruction() -> str:
    """Prompt used only to consolidate reports that have already been analyzed.

    Reports are not raw WeChat messages. Keeping this separate prevents labels
    such as “分析分段 1” from being mistaken for customer names during reduce.
    """
    return (
        analysis_instruction(ROLE_ORDER)
        + "\n当前输入是同一会话的订单意向分析报告，不是聊天原文。"
        "报告中的‘分析分段’等标签不是客户，不能填入客户列；客户、商品、数量、时间和原话依据"
        "必须沿用报告中已有的信息。只合并证据明确相同的重复行；不同客户或商品分别保留。"
        "不得把原报告没有的内容补成订单，也不得把已有意向删掉。若至少一份报告有意向表格，"
        "只输出合并后的同格式表格；只有所有报告都明确没有意向时，才输出‘未发现可识别的订货意向。’"
    )


# ------------------------------------------------------------ 规则引擎


def _categorize(role: RoleDef, text: str) -> tuple[str, str]:
    """正文 → (桶 key, 桶显示名)。排前面的桶优先，命中即停。"""
    lowered = str(text or "")
    for category in role.categories:
        for keyword in category.keywords:
            if keyword and keyword in lowered:
                return category.key, category.label
        for pattern in category.patterns:
            try:
                if pattern and re.search(pattern, lowered):
                    return category.key, category.label
            except re.error:
                continue
    return "", role.fallback_label


def organize(role_id: str, messages: Sequence[Mapping[str, Any]]) -> RoleReport:
    """本机规则整理：输入消息行，输出按桶分组的报告。

    消息行字段：id/group/sender/content/sent_at/kind（kind 非文本进非文字桶）。
    订单角色不走这里 —— 调用方看到 ROLE_ORDER 应该走订单链路；
    误传进来直接按通用整理跑，不静默失败。
    """
    role = get_role(role_id)
    if role.id == ROLE_ORDER:
        role = get_role(ROLE_GENERIC)
    buckets: dict[str, list[RoleItem]] = {}
    total = 0
    for message in messages or ():
        total += 1
        message_id = str(message.get("id", ""))
        group = str(message.get("group", ""))
        sender = str(message.get("sender", ""))
        content = str(message.get("content", ""))
        sent_at = str(message.get("sent_at", ""))
        kind = str(message.get("kind", "text"))
        if kind not in ("text", "文本", ""):
            key, label = "non_text", "非文字（图片/语音等，需人工看）"
            facts: dict[str, str] = {}
        else:
            key, label = _categorize(role, content)
            facts = extract_facts(content)
        item = RoleItem(message_id=message_id, group=group, sender=sender,
                        content=content, sent_at=sent_at,
                        category=key or "fallback", category_label=label,
                        facts=facts)
        buckets.setdefault(label, []).append(item)
    return RoleReport(role_id=role.id, role_name=role.name,
                      total=total, buckets=buckets)


__all__ = [
    "ALL_ROLES",
    "ROLE_ACTIVITY",
    "ROLE_GENERIC",
    "ROLE_ORDER",
    "CategoryDef",
    "RoleDef",
    "RoleItem",
    "RoleReport",
    "extract_facts",
    "get_role",
    "list_roles",
    "organize",
]
