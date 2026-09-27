"""定义义诊后续联系路由使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParticipantProfile:
    """参与者的最少必要联系资料：渠道授权与用途授权。"""

    participant_id: str
    site_id: str
    channels: frozenset[str]
    purposes: frozenset[str]
    version: int


@dataclass(frozen=True)
class EncounterSummary:
    """义诊现场生成的最少必要摘要，只含白名单字段，不含诊断结论。"""

    encounter_id: str
    participant_id: str
    site_id: str
    fields: dict[str, str]


@dataclass(frozen=True)
class ExpertDecision:
    """专家给出的后续类别标记。这是整条链路上唯一的医学判断，且来自医生。"""

    encounter_id: str
    expert_actor_id: str
    category: str


@dataclass(frozen=True)
class Template:
    """消息模板版本。文本在排队时固化，之后更新模板不影响已排队消息。"""

    code: str
    version: int
    text: str
    active: bool


@dataclass(frozen=True)
class FollowupRecord:
    """一次义诊后续事项的持久化路由状态。"""

    followup_id: str
    encounter_id: str
    participant_id: str
    site_id: str
    category: str
    status: str
    channels: tuple[str, ...]
    created_at: str
    template_code: str | None
    template_version: int | None
    text_snapshot: str | None
    not_before: str | None
    expires_at: str | None
    handoff_due_at: str | None
    handoff_role: str | None
    handoff_actor_id: str | None
    completed_at: str | None


@dataclass(frozen=True)
class DeliveryAttempt:
    """一次发送尝试的结果；成功回执只保留不可反推健康细节的字段。"""

    attempt_id: str
    followup_id: str
    channel: str
    attempt_number: int
    status: str
    detail_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StaffTask:
    """高风险事项的授权人员交接任务。"""

    task_id: str
    followup_id: str
    role: str
    level: int
    status: str
    claimed_by: str | None
    due_at: str
    created_at: str
    claimed_at: str | None
    confirmed_at: str | None
    disposition_code: str | None


@dataclass(frozen=True)
class FollowupExplanation:
    """后台查询用：解释一条联系为何安排、跳过或转人工。"""

    followup: FollowupRecord
    timeline: tuple[dict[str, Any], ...]
    attempts: tuple[DeliveryAttempt, ...]
    tasks: tuple[StaffTask, ...]


@dataclass(frozen=True)
class ClockResult:
    """一次时钟扫描的处理结果汇总。"""

    due_sent: int
    retried: int
    escalated: int
    expired: int
