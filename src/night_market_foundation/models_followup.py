"""定义诊后联系分流在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Consent:
    """描述参与者选择的联系渠道范围。"""

    participant_ref: str
    channels: frozenset[str]
    version: int
    updated_at: str


@dataclass(frozen=True)
class Template:
    """描述一个已发布模板版本，版本一经发布正文不可变。"""

    template_key: str
    channel: str
    version: int
    body: str
    published_at: str
    active: bool


@dataclass(frozen=True)
class FollowupSummary:
    """现场服务生成的最少必要摘要，不含诊断文本与健康细节。"""

    summary_ref: str
    site_id: str
    participant_ref: str
    recorded_by: str
    recorded_at: str
    tier: str
    category: str
    followup_window_hours: int
    send_window_hours: int
    template_key: str
    message_variables: dict[str, Any] = field(default_factory=dict)
    handoff_due_hours: int | None = None


@dataclass(frozen=True)
class ScheduledMessage:
    """一条排队中的常规养生提醒，正文与模板版本在排队时固化。"""

    message_id: str
    summary_ref: str
    participant_ref: str
    channel: str
    template_key: str
    template_version: int
    body_snapshot: str
    state: str
    scheduled_for: str
    send_before: str
    attempts: int
    reason_code: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Handoff:
    """一条列入授权人员名单、带期限与升级轨迹的高风险事项。"""

    handoff_id: str
    summary_ref: str
    participant_ref: str
    category: str
    tier: str
    state: str
    level: int
    assigned_role: str
    assigned_actor_id: str | None
    due_at: str
    escalated_at: str | None
    acknowledged_at: str | None
    acknowledged_by: str | None
    reason_code: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DeliveryReceipt:
    """成功投递后的最小回执，无法反推出参与者的健康细节。"""

    receipt_id: str
    message_id: str
    channel: str
    provider_message_ref: str
    delivered_at: str


@dataclass(frozen=True)
class Explanation:
    """后台对一条联系为何安排、跳过或转人工的可解释结果。"""

    summary_ref: str
    participant_ref: str
    tier: str
    category: str
    decisions: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    handoff: dict[str, Any] | None
