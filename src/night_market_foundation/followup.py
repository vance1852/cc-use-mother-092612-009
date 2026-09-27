"""定义义诊后续联系的固定类别、渠道、授权用途与时间窗口策略。

系统只依据专家给出的后续类别做路由，类别取值被限制在固定枚举内，
现场摘要的字段也采用白名单，系统本身不从摘要内容推断任何诊疗结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# 参与者侧的联系渠道，由参与者在现场自行勾选。
CONTACT_CHANNELS = frozenset({"sms", "wechat", "voice_call"})

# 参与者授权的用途：普通养生提醒、建议复查提醒。
CONSENT_PURPOSES = frozenset({"wellness", "recheck"})

# 专家可标记的后续类别；系统不接受枚举之外的取值。
CATEGORY_WELLNESS = "wellness_reminder"
CATEGORY_RECHECK = "recommended_recheck"
CATEGORY_URGENT = "urgent_followup"
FOLLOWUP_CATEGORIES = frozenset({CATEGORY_WELLNESS, CATEGORY_RECHECK, CATEGORY_URGENT})

# 现场最少必要摘要允许出现的字段；不允许夹带诊断文本。
ALLOWED_SUMMARY_FIELDS = frozenset({"service_topic", "self_care"})
SUMMARY_FIELD_LIMIT = 200

# 高风险事项的人工接管方式只记录代码，不记录任何病情描述。
DISPOSITION_CODES = frozenset({"accepted", "contacted_participant", "referred_clinic"})

# 站点本地时间的可联络时段（24 小时制）。
DAY_OPEN_HOUR = 9
DAY_CLOSE_HOUR = 20

# 发送失败的退避间隔与最大尝试次数。
BACKOFF_MINUTES = (1, 5, 15)
MAX_ATTEMPTS = 3

# 高风险事项的授权人员升级链：先交给当班专家，到期未确认再升级到管理负责人。
ESCALATION_ROLE_CHAIN = ("reviewer", "admin")
URGENT_DEADLINE = timedelta(hours=24)

# 卡在 sending 状态超过该时长视为认领僵死，可被重新认领（渠道侧按消息编号幂等去重）。
CLAIM_TIMEOUT = timedelta(minutes=5)


@dataclass(frozen=True)
class CategoryPolicy:
    """描述一个专家类别的固定路由策略。"""

    category: str
    participant_message: bool
    purpose: str | None
    template_code: str | None
    window_ttl: timedelta | None
    staff_handoff: bool
    handoff_deadline: timedelta | None


CATEGORY_POLICY: dict[str, CategoryPolicy] = {
    CATEGORY_WELLNESS: CategoryPolicy(
        category=CATEGORY_WELLNESS,
        participant_message=True,
        purpose="wellness",
        template_code="wellness_reminder",
        window_ttl=timedelta(days=7),
        staff_handoff=False,
        handoff_deadline=None,
    ),
    CATEGORY_RECHECK: CategoryPolicy(
        category=CATEGORY_RECHECK,
        participant_message=True,
        purpose="recheck",
        template_code="recommended_recheck",
        window_ttl=timedelta(days=3),
        staff_handoff=False,
        handoff_deadline=None,
    ),
    CATEGORY_URGENT: CategoryPolicy(
        category=CATEGORY_URGENT,
        participant_message=False,
        purpose=None,
        template_code=None,
        window_ttl=None,
        staff_handoff=True,
        handoff_deadline=URGENT_DEADLINE,
    ),
}


def next_window_start(when: datetime, timezone_name: str) -> datetime:
    """返回不早于 ``when`` 的下一个本地日间联络窗口起点（UTC）。"""

    zone = ZoneInfo(timezone_name)
    local = when.astimezone(zone)
    open_today = local.replace(hour=DAY_OPEN_HOUR, minute=0, second=0, microsecond=0)
    close_today = local.replace(hour=DAY_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    if local < open_today:
        return open_today.astimezone(timezone.utc)
    if local >= close_today:
        return (open_today + timedelta(days=1)).astimezone(timezone.utc)
    return when.astimezone(timezone.utc)


def within_daytime_window(when: datetime, timezone_name: str) -> bool:
    """判断给定时刻是否正处于站点本地的可联络时段。"""

    local = when.astimezone(ZoneInfo(timezone_name))
    return DAY_OPEN_HOUR <= local.hour < DAY_CLOSE_HOUR
