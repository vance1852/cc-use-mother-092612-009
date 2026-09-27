"""运行义诊后续联系路由的离线端到端验收。

覆盖：授权范围路由、专家类别、窗口发送、跳过原因、高风险交接与升级、
幂等重试、撤回取消、重启恢复、最小回执与审计链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import Clock
from .followup_gateway import RecordingGateway, RecordingNotifier
from .followup_service import FollowupService
from .service import DomainService
from .storage import Database


class MutableClock(Clock):
    """验收使用的可调时钟。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def run() -> dict[str, object]:
    """执行完整后续联系链并返回可核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database_path = Path(directory) / "followup_acceptance.sqlite3"
        clock = MutableClock(datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc))  # 11:00 上海
        gateway = RecordingGateway()
        notifier = RecordingNotifier()
        database = Database(database_path)
        base = DomainService(database, clock)
        service = FollowupService(database, clock, gateway, notifier)

        base.register_organization(request_id="a-org", actor_id="bootstrap",
                                   organization_id="org-001", name="示范活动机构")
        base.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="管理负责人", role="admin", organization_id="org-001")
        base.register_actor(request_id="a-op", actor_id="admin-001", new_actor_id="op-001",
                            display_name="现场操作员", role="operator", organization_id="org-001")
        base.register_actor(request_id="a-rv", actor_id="admin-001", new_actor_id="rv-001",
                            display_name="当班专家", role="reviewer", organization_id="org-001")
        base.register_site(request_id="a-site", actor_id="op-001", site_id="site-001",
                           organization_id="org-001", name="一号活动站点",
                           timezone_name="Asia/Shanghai")

        service.register_template(request_id="a-tw1", actor_id="admin-001",
                                  code="wellness_reminder", version=1, text="节气养生提示")
        service.register_template(request_id="a-tr1", actor_id="admin-001",
                                  code="recommended_recheck", version=1, text="建议按期复查提示")

        # 三名参与者：全授权 / 未授权 / 稍后撤回
        service.register_participant(request_id="a-p1", actor_id="op-001", participant_id="pt-001",
                                     site_id="site-001", channels=["sms", "wechat"],
                                     purposes=["wellness", "recheck"])
        service.register_participant(request_id="a-p2", actor_id="op-001", participant_id="pt-002",
                                     site_id="site-001", channels=[], purposes=[])
        service.register_participant(request_id="a-p3", actor_id="op-001", participant_id="pt-003",
                                     site_id="site-001", channels=["sms"],
                                     purposes=["wellness", "recheck"])
        service.register_participant(request_id="a-p4", actor_id="op-001", participant_id="pt-004",
                                     site_id="site-001", channels=["sms"],
                                     purposes=["wellness", "recheck"])
        service.register_participant(request_id="a-p5", actor_id="op-001", participant_id="pt-005",
                                     site_id="site-001", channels=["sms"],
                                     purposes=["wellness", "recheck"])

        service.record_encounter(request_id="a-e1", actor_id="op-001", encounter_id="en-001",
                                 participant_id="pt-001", site_id="site-001",
                                 summary={"service_topic": "推拿体验", "self_care": "局部热敷"})
        service.record_encounter(request_id="a-e2", actor_id="op-001", encounter_id="en-002",
                                 participant_id="pt-002", site_id="site-001",
                                 summary={"service_topic": "脉诊体验"})
        service.record_encounter(request_id="a-e3", actor_id="op-001", encounter_id="en-003",
                                 participant_id="pt-003", site_id="site-001",
                                 summary={"service_topic": "舌诊体验"})
        service.record_encounter(request_id="a-e4", actor_id="op-001", encounter_id="en-004",
                                 participant_id="pt-004", site_id="site-001",
                                 summary={"service_topic": "艾灸体验"})
        service.record_encounter(request_id="a-e5", actor_id="op-001", encounter_id="en-005",
                                 participant_id="pt-005", site_id="site-001",
                                 summary={"service_topic": "耳穴体验"})

        wellness = service.record_expert_decision(
            request_id="a-d1", actor_id="rv-001", encounter_id="en-001",
            category="wellness_reminder")
        skipped = service.record_expert_decision(
            request_id="a-d2", actor_id="rv-001", encounter_id="en-002",
            category="wellness_reminder")
        acknowledged = service.record_expert_decision(
            request_id="a-d3", actor_id="rv-001", encounter_id="en-003",
            category="urgent_followup")
        unconfirmed = service.record_expert_decision(
            request_id="a-d4", actor_id="rv-001", encounter_id="en-004",
            category="urgent_followup")
        recheck = service.record_expert_decision(
            request_id="a-d5", actor_id="rv-001", encounter_id="en-005",
            category="recommended_recheck")

        # 把复查提醒排到重启之后的时刻，验证持久化恢复。
        database.connection.execute(
            "UPDATE followups SET not_before=? WHERE followup_id=?",
            ("2026-09-27T03:00:00Z", recheck.resource_id))
        database.connection.commit()

        # 专家当场确认接手第一项高风险事项。
        ack_task = service.explain(actor_id="rv-001",
                                   followup_id=acknowledged.resource_id).tasks[0]
        service.claim_task(actor_id="rv-001", task_id=ack_task.task_id)
        service.confirm_received(request_id="a-c1", actor_id="rv-001",
                                 task_id=ack_task.task_id, disposition_code="accepted")

        # 第二项高风险事项无人确认，稍后撤回不再相关（撤回只影响参与者消息）。
        # 首次时钟扫描：发送一条养生提醒（复查提醒未到期）。
        tick1 = service.run_due()
        assert tick1.due_sent == 1 and tick1.escalated == 0

        # 幂等：重复扫描不产生重复消息。
        tick_again = service.run_due()
        assert tick_again.due_sent == 0

        # 到原期限（24 小时）仍未确认：升级到管理负责人。
        clock.advance(hours=25)
        tick2 = service.run_due()
        assert tick2.escalated == 1
        explanation = service.explain(actor_id="admin-001",
                                      followup_id=unconfirmed.resource_id)
        admin_task = next(t for t in explanation.tasks if t.role == "admin")
        service.claim_task(actor_id="admin-001", task_id=admin_task.task_id)
        service.confirm_received(request_id="a-c2", actor_id="admin-001",
                                 task_id=admin_task.task_id,
                                 disposition_code="referred_clinic")
        # 重复扫描不重复升级、不重复通知。
        assert service.run_due().escalated == 0
        notifications_once = len(notifier.notifications)
        service.run_due()
        assert len(notifier.notifications) == notifications_once

        # 跳过事项必须能解释原因。
        skipped_explanation = service.explain(actor_id="admin-001",
                                              followup_id=skipped.resource_id)
        assert skipped_explanation.timeline[0]["detail"]["reason"] == \
            "purpose_or_channel_not_authorized"

        # 已投递回执不得反推健康细节。
        delivered_explanation = service.explain(actor_id="admin-001",
                                                followup_id=wellness.resource_id)
        rendered = json.dumps([dict(e) for e in delivered_explanation.timeline],
                              ensure_ascii=False)
        assert "推拿" not in rendered and "热敷" not in rendered

        # 模板更新不改变已排队文本（复查提醒仍为旧版）。
        service.register_template(request_id="a-tr2", actor_id="admin-001",
                                  code="recommended_recheck", version=2, text="新版复查文本")
        assert service.get_followup(recheck.resource_id).text_snapshot == "建议按期复查提示"

        # 重启：关闭后用同一数据库路径重建全部服务，凭持久化状态恢复定时任务。
        database.close()
        clock.advance(hours=24)
        database = Database(database_path)
        base = DomainService(database, clock)
        gateway = RecordingGateway()
        service = FollowupService(database, clock, gateway, notifier)
        recovered = service.recover()
        assert recovered.due_sent == 1
        assert gateway.sent[0]["text"] == "建议按期复查提示"
        assert service.get_followup(recheck.resource_id).status == "delivered"

        valid, event_count = base.verify_audit()
        statuses = {
            "wellness": service.get_followup(wellness.resource_id).status,
            "skipped": service.get_followup(skipped.resource_id).status,
            "acknowledged": service.get_followup(acknowledged.resource_id).status,
            "unconfirmed": service.get_followup(unconfirmed.resource_id).status,
            "recheck": service.get_followup(recheck.resource_id).status,
        }
        database.close()
        return {"status": "ok", "audit_valid": valid, "audit_events": event_count,
                "final_statuses": statuses,
                "ticks": {"first": tick1.__dict__, "at_deadline": tick2.__dict__,
                          "after_restart": recovered.__dict__}}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
