"""定义诊后联系分流使用的固定取值与原因代码。

系统只根据专家给出的后续类别做路由，类别词汇必须事先登记，
系统不自行解读现场摘要、不产生任何诊疗判断。
"""

from __future__ import annotations

CHANNEL_SMS = "sms"
CHANNEL_WECHAT = "wechat"
CHANNEL_PUSH = "push"

ALLOWED_CHANNELS = frozenset({CHANNEL_SMS, CHANNEL_WECHAT, CHANNEL_PUSH})

TIER_ROUTINE = "routine"
TIER_URGENT = "urgent"
ALLOWED_TIERS = frozenset({TIER_ROUTINE, TIER_URGENT})

# 消息排队状态
MSG_QUEUED = "queued"
MSG_SENDING = "sending"
MSG_SENT = "sent"
MSG_CANCELLED = "cancelled"
MSG_BLOCKED = "blocked"
MSG_EXPIRED = "expired"

# 人工交接状态
HANDOFF_OPEN = "open"
HANDOFF_ACKNOWLEDGED = "acknowledged"
HANDOFF_ESCALATED = "escalated"
HANDOFF_BREACHED = "breached"

# 升级层级与授权角色：第一层交由复核专家，超时升级到管理员，
# 层级数量固定，系统不会自行扩大知情人范围。
HANDOFF_ROLE_BY_LEVEL = {1: "reviewer", 2: "admin"}
MAX_HANDOFF_LEVEL = 2

# 模板正文允许使用的占位符，只允许与健康细节无关的字段。
ALLOWED_TEMPLATE_VARIABLES = frozenset({"site_name"})

# 原因代码：后台解释一条联系为何安排、跳过或转人工
REASON_ROUTINE_SCHEDULED = "routine_scheduled"
REASON_NO_CONSENTED_CHANNEL = "no_consented_channel"
REASON_NO_PUBLISHED_TEMPLATE = "no_published_template"
REASON_URGENT_HANDOFF = "expert_urgent_category"
REASON_CONSENT_REVOKED = "consent_revoked"
REASON_CONSENT_SCOPE_CHANGED = "consent_scope_changed"
REASON_TEMPLATE_RETIRED = "template_version_retired"
REASON_TEMPLATE_MISSING = "template_version_missing"
REASON_DELIVERY_EXHAUSTED = "delivery_exhausted"
REASON_DELIVERY_REJECTED = "delivery_rejected"
REASON_SEND_WINDOW_EXPIRED = "send_window_expired"
REASON_MANUAL_TAKEOVER = "manual_takeover"

REASON_TEXT = {
    REASON_ROUTINE_SCHEDULED: "专家分类为常规养生提醒，参与者已授权该渠道，消息按发送窗口排队",
    REASON_NO_CONSENTED_CHANNEL: "参与者未授权任何联系渠道，常规养生提醒跳过",
    REASON_NO_PUBLISHED_TEMPLATE: "该渠道没有已发布模板，该渠道跳过",
    REASON_URGENT_HANDOFF: "专家标记需尽快进一步检查，事项在期限内转交授权人员",
    REASON_CONSENT_REVOKED: "参与者已撤回联系授权，尚未发送的内容立即取消",
    REASON_CONSENT_SCOPE_CHANGED: "参与者缩小了授权渠道范围，该渠道尚未发送的内容取消",
    REASON_TEMPLATE_RETIRED: "排队时固定的模板版本已召回，转人工处理，不替换为新版本",
    REASON_TEMPLATE_MISSING: "排队时固定的模板版本已不存在，转人工处理",
    REASON_DELIVERY_EXHAUSTED: "自动重试已超过发送窗口，转人工处理",
    REASON_DELIVERY_REJECTED: "渠道网关明确拒绝该地址，转人工处理",
    REASON_SEND_WINDOW_EXPIRED: "已超过发送窗口仍未成功发出，转人工处理",
    REASON_MANUAL_TAKEOVER: "工作人员人工接管，自动发送取消",
}

# 定时任务流转中使用的补充原因代码（不属于跳过/转人工解释集合）
REASON_DELIVERED = "delivered"
REASON_TRANSIENT_RETRY = "transient_retry"
REASON_ESCALATED_ON_DUE = "escalated_on_original_due"
REASON_FINAL_BREACHED = "final_level_breached"
REASON_ACKNOWLEDGED = "acknowledged"
# 网关受理与撤回/接管几乎同时发生：内容可能已实际发出，保留最小回执并转人工核对
REASON_DELIVERY_RACE = "delivery_race_after_cancellation"
