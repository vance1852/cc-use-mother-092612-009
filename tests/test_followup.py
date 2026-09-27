"""义诊后续联系路由的领域测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from night_market_foundation.errors import ConflictError, PermissionDenied, ValidationError
from night_market_foundation.followup import URGENT_DEADLINE
from night_market_foundation.followup_gateway import RecordingGateway, RecordingNotifier
from night_market_foundation.followup_service import FollowupService
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database


class MovingClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class FollowupTestBase(unittest.TestCase):
    start = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)  # 11:00 上海，处于日间窗口

    def setUp(self):
        self.clock = MovingClock(self.start)
        self.database = Database()
        self.base = DomainService(self.database, self.clock)
        self.gateway = RecordingGateway()
        self.notifier = RecordingNotifier()
        self.service = FollowupService(self.database, self.clock, self.gateway, self.notifier)
        self.base.register_organization(request_id="r-org", actor_id="bootstrap",
                                        organization_id="o1", name="示范机构")
        self.base.register_actor(request_id="r-admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="r-op", actor_id="a1", new_actor_id="op1",
                                 display_name="操作员", role="operator", organization_id="o1")
        self.base.register_actor(request_id="r-rv", actor_id="a1", new_actor_id="rv1",
                                 display_name="当班专家", role="reviewer", organization_id="o1")
        self.base.register_actor(request_id="r-au", actor_id="a1", new_actor_id="au1",
                                 display_name="审计员", role="auditor", organization_id="o1")
        self.base.register_site(request_id="r-site", actor_id="op1", site_id="s1",
                                organization_id="o1", name="一号站点",
                                timezone_name="Asia/Shanghai")
        self.service.register_template(request_id="r-tw", actor_id="a1",
                                       code="wellness_reminder", version=1, text="养生提醒 V1")
        self.service.register_template(request_id="r-tr", actor_id="a1",
                                       code="recommended_recheck", version=1, text="复查提醒 V1")

    def tearDown(self):
        self.database.close()

    def participant(self, pid, *, channels=("sms",), purposes=("wellness", "recheck"),
                    request_id=None):
        self.service.register_participant(
            request_id=request_id or f"rp-{pid}", actor_id="op1", participant_id=pid,
            site_id="s1", channels=list(channels), purposes=list(purposes))

    def encounter(self, eid, pid, *, request_id=None, summary=None):
        self.service.record_encounter(
            request_id=request_id or f"re-{eid}", actor_id="op1", encounter_id=eid,
            participant_id=pid, site_id="s1",
            summary=summary or {"service_topic": "推拿体验", "self_care": "注意保暖"})

    def decide(self, eid, category, request_id=None):
        receipt = self.service.record_expert_decision(
            request_id=request_id or f"rd-{eid}", actor_id="rv1",
            encounter_id=eid, category=category)
        return receipt.resource_id


class RoutingTest(FollowupTestBase):
    def test_wellness_reminder_is_queued_within_authorization(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        record = self.service.get_followup(fid)
        self.assertEqual("queued", record.status)
        self.assertEqual(("sms",), record.channels)
        self.assertEqual(1, record.template_version)
        self.assertEqual("养生提醒 V1", record.text_snapshot)
        self.assertIsNotNone(record.not_before)
        self.assertIsNotNone(record.expires_at)

    def test_urgent_goes_to_authorized_staff_not_participant_channel(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "urgent_followup")
        record = self.service.get_followup(fid)
        self.assertEqual("in_human_handoff", record.status)
        self.assertIsNone(record.template_code)
        self.assertIsNone(record.text_snapshot)
        due = datetime.fromisoformat(record.handoff_due_at.replace("Z", "+00:00"))
        self.assertEqual(self.start + URGENT_DEADLINE, due)
        explanation = self.service.explain(actor_id="au1", followup_id=fid)
        self.assertEqual("reviewer", explanation.tasks[0].role)
        self.assertEqual("routing.human_handoff", explanation.timeline[0]["event"])
        # 高风险事项不会向参与者发送任何消息
        result = self.service.run_due()
        self.assertEqual(0, result.due_sent)
        self.assertEqual(0, len(self.gateway.sent))

    def test_missing_consent_skips_participant_message_with_reason(self):
        self.participant("p1", channels=(), purposes=())
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        record = self.service.get_followup(fid)
        self.assertEqual("skipped", record.status)
        explanation = self.service.explain(actor_id="a1", followup_id=fid)
        event = explanation.timeline[0]
        self.assertEqual("routing.skipped", event["event"])
        self.assertEqual("purpose_or_channel_not_authorized", event["detail"]["reason"])

    def test_purpose_mismatch_skips_even_with_channel_consent(self):
        self.participant("p1", channels=("sms",), purposes=("wellness",))
        self.encounter("e1", "p1")
        fid = self.decide("e1", "recommended_recheck")
        self.assertEqual("skipped", self.service.get_followup(fid).status)

    def test_system_does_not_accept_unknown_category_or_diagnosis_text(self):
        self.participant("p1")
        with self.assertRaises(ValidationError):
            self.service.record_encounter(
                request_id="re-bad", actor_id="op1", encounter_id="eX", participant_id="p1",
                site_id="s1", summary={"service_topic": "推拿", "diagnosis": "疑似重症"})
        self.encounter("e1", "p1")
        with self.assertRaises(ValidationError):
            self.decide("e1", "system_guessed_disease")

    def test_decision_conflict_cannot_be_overridden(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        self.decide("e1", "wellness_reminder")
        with self.assertRaises(ConflictError):
            self.service.record_expert_decision(
                request_id="rd-change", actor_id="rv1", encounter_id="e1",
                category="urgent_followup")

    def test_operator_cannot_mark_expert_category(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        with self.assertRaises(PermissionDenied):
            self.service.record_expert_decision(
                request_id="rd-op", actor_id="op1", encounter_id="e1",
                category="urgent_followup")


class TemplateFreezeTest(FollowupTestBase):
    def test_template_update_does_not_change_queued_text(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        self.service.register_template(request_id="r-tv2", actor_id="a1",
                                       code="wellness_reminder", version=2, text="养生提醒 V2")
        self.assertEqual("养生提醒 V1", self.service.get_followup(fid).text_snapshot)
        result = self.service.run_due()
        self.assertEqual(1, result.due_sent)
        self.assertEqual("养生提醒 V1", self.gateway.sent[0]["text"])

    def test_same_template_version_text_is_immutable(self):
        with self.assertRaises(ConflictError):
            self.service.register_template(request_id="r-tamper", actor_id="a1",
                                           code="wellness_reminder", version=1, text="被篡改")

    def test_missing_template_version_pauses_before_send(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        self.database.connection.execute("DELETE FROM message_templates")
        self.database.connection.commit()
        self.service.run_due()
        self.assertEqual("paused_template", self.service.get_followup(fid).status)
        explanation = self.service.explain(actor_id="au1", followup_id=fid)
        self.assertIn("send.paused", [e["event"] for e in explanation.timeline])


class SchedulingTest(FollowupTestBase):
    def test_message_waits_for_daytime_window(self):
        # 02:00 上海处于夜间，排队应推迟到 09:00
        self.clock.value = datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc)  # 次日 02:00
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        record = self.service.get_followup(fid)
        self.assertEqual("2026-09-26T01:00:00Z", record.not_before)
        self.assertEqual(0, self.service.run_due().due_sent)
        self.clock.advance(hours=7)  # 09:00 上海
        self.assertEqual(1, self.service.run_due().due_sent)
        self.assertEqual("delivered", self.service.get_followup(fid).status)

    def test_window_expiry_closes_without_delivery(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        self.clock.advance(days=8)
        result = self.service.run_due()
        self.assertEqual(1, result.expired)
        self.assertEqual("expired", self.service.get_followup(fid).status)
        self.assertEqual(0, len(self.gateway.sent))

    def test_retry_backoff_then_idempotent_delivery(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        self.gateway.fail_retryable_until["sms"] = 2
        result = self.service.run_due()
        self.assertEqual((0, 1), (result.due_sent, result.retried))
        # 退避未到，重复扫描不产生新尝试
        self.assertEqual(0, self.service.run_due().retried)
        self.clock.advance(minutes=1)
        self.assertEqual(1, self.service.run_due().retried)
        self.clock.advance(minutes=5)
        self.assertEqual(1, self.service.run_due().due_sent)
        self.assertEqual("delivered", self.service.get_followup(fid).status)
        # 渠道侧只真正发出一条消息
        self.assertEqual(1, len(self.gateway.sent))

    def test_duplicate_ticks_do_not_duplicate_delivery(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        self.decide("e1", "wellness_reminder")
        self.assertEqual(1, self.service.run_due().due_sent)
        second = self.service.run_due()
        self.assertEqual(0, second.due_sent)
        self.assertEqual(1, len(self.gateway.sent))

    def test_delivery_receipt_holds_no_health_detail(self):
        self.participant("p1")
        self.encounter("e1", "p1", summary={"service_topic": "隐私主题"})
        fid = self.decide("e1", "wellness_reminder")
        self.service.run_due()
        explanation = self.service.explain(actor_id="au1", followup_id=fid)
        delivered = next(e for e in explanation.timeline if e["event"] == "send.delivered")
        self.assertNotIn("text", delivered["detail"])
        self.assertNotIn("隐私主题", str(delivered))
        self.assertEqual({"channel", "attempt_number", "provider_message_id"},
                         set(delivered["detail"]))

    def test_authorization_shrunk_before_send_is_rechecked(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        self.database.connection.execute(
            "UPDATE followups SET not_before=? WHERE followup_id=?",
            ("2026-09-30T03:00:00Z", fid))
        self.database.connection.commit()
        # 参与者随后只保留复查用途授权
        self.service.register_participant(
            request_id="rp-p1b", actor_id="op1", participant_id="p1", site_id="s1",
            channels=["sms"], purposes=["recheck"])
        self.clock.advance(days=5)
        self.service.run_due()
        self.assertEqual("revoked", self.service.get_followup(fid).status)
        self.assertEqual(0, len(self.gateway.sent))


class RevocationTest(FollowupTestBase):
    def test_revocation_cancels_all_pending_messages(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        self.database.connection.execute(
            "UPDATE followups SET not_before=? WHERE followup_id=?",
            ("2026-09-30T03:00:00Z", fid))
        self.database.connection.commit()
        receipt = self.service.revoke_consent(
            request_id="r-revoke", actor_id="op1", participant_id="p1", site_id="s1")
        self.assertEqual("p1", receipt.resource_id)
        self.assertEqual("revoked", self.service.get_followup(fid).status)
        # 重复撤回请求幂等
        replayed = self.service.revoke_consent(
            request_id="r-revoke", actor_id="op1", participant_id="p1", site_id="s1")
        self.assertTrue(replayed.replayed)
        self.clock.advance(days=10)
        self.assertEqual(0, self.service.run_due().due_sent)
        self.assertEqual(0, len(self.gateway.sent))


class HumanHandoffTest(FollowupTestBase):
    def _urgent(self):
        self.participant("p1")
        self.encounter("e1", "p1")
        return self.decide("e1", "urgent_followup")

    def test_claim_and_confirm_are_idempotent(self):
        fid = self._urgent()
        task = self.service.explain(actor_id="rv1", followup_id=fid).tasks[0]
        claimed = self.service.claim_task(actor_id="rv1", task_id=task.task_id)
        self.assertEqual("claimed", claimed.status)
        again = self.service.claim_task(actor_id="rv1", task_id=task.task_id)
        self.assertEqual("claimed", again.status)
        first = self.service.confirm_received(
            request_id="r-confirm", actor_id="rv1", task_id=task.task_id,
            disposition_code="accepted")
        second = self.service.confirm_received(
            request_id="r-confirm", actor_id="rv1", task_id=task.task_id,
            disposition_code="accepted")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual("completed", self.service.get_followup(fid).status)

    def test_other_staff_member_cannot_steal_claimed_task(self):
        fid = self._urgent()
        self.base.register_actor(request_id="r-rv2", actor_id="a1", new_actor_id="rv2",
                                 display_name="另一位专家", role="reviewer",
                                 organization_id="o1")
        task = self.service.explain(actor_id="rv1", followup_id=fid).tasks[0]
        self.service.claim_task(actor_id="rv1", task_id=task.task_id)
        with self.assertRaises(ConflictError):
            self.service.claim_task(actor_id="rv2", task_id=task.task_id)

    def test_unconfirmed_high_risk_escalates_by_original_deadline(self):
        fid = self._urgent()
        # 期限前不升级，原任务仍为 open
        self.clock.advance(hours=23)
        self.assertEqual(0, self.service.run_due().escalated)
        self.assertEqual("open",
                         self.service.explain(actor_id="a1", followup_id=fid).tasks[0].status)
        # 到期未确认：升级到管理负责人，新任务沿用同一原始期限
        self.clock.advance(hours=2)
        result = self.service.run_due()
        self.assertEqual(1, result.escalated)
        explanation = self.service.explain(actor_id="a1", followup_id=fid)
        self.assertEqual("escalated", explanation.tasks[0].status)
        admin_task = explanation.tasks[1]
        self.assertEqual("admin", admin_task.role)
        self.assertEqual(self.start + URGENT_DEADLINE,
                         datetime.fromisoformat(admin_task.due_at.replace("Z", "+00:00")))
        # 管理员确认后事项闭环
        self.service.claim_task(actor_id="a1", task_id=admin_task.task_id)
        self.service.confirm_received(
            request_id="r-admin-confirm", actor_id="a1", task_id=admin_task.task_id,
            disposition_code="referred_clinic")
        self.assertEqual("completed", self.service.get_followup(fid).status)
        # 重复扫描不会再次升级
        self.assertEqual(0, self.service.run_due().escalated)

    def test_escalated_task_cannot_be_confirmed_at_old_level(self):
        fid = self._urgent()
        task = self.service.explain(actor_id="rv1", followup_id=fid).tasks[0]
        self.clock.advance(hours=25)
        self.service.run_due()
        with self.assertRaises(ConflictError):
            self.service.confirm_received(
                request_id="r-late", actor_id="rv1", task_id=task.task_id,
                disposition_code="accepted")


class ExplanationTest(FollowupTestBase):
    def test_explanation_covers_schedule_skip_and_handoff_reasons(self):
        self.participant("p1")
        self.participant("p2", channels=(), purposes=(), request_id="rp-p2")
        self.encounter("e1", "p1")
        self.encounter("e2", "p2")
        scheduled = self.decide("e1", "wellness_reminder")
        skipped = self.decide("e2", "wellness_reminder")
        self.participant("p3", request_id="rp-p3")
        self.encounter("e3", "p3")
        handoff = self.decide("e3", "urgent_followup")
        for fid, reason in ((scheduled, "routing.scheduled"),
                            (skipped, "routing.skipped"),
                            (handoff, "routing.human_handoff")):
            events = [e["event"] for e in
                      self.service.explain(actor_id="au1", followup_id=fid).timeline]
            self.assertIn(reason, events)

    def test_auditor_from_other_org_is_blocked(self):
        self.base.register_organization(request_id="r-org2", actor_id="a1",
                                        organization_id="o2", name="其他机构")
        self.base.register_actor(request_id="r-au2", actor_id="a1", new_actor_id="au2",
                                 display_name="外机构审计", role="auditor",
                                 organization_id="o2")
        self.participant("p1")
        self.encounter("e1", "p1")
        fid = self.decide("e1", "wellness_reminder")
        with self.assertRaises(PermissionDenied):
            self.service.explain(actor_id="au2", followup_id=fid)


if __name__ == "__main__":
    unittest.main()
