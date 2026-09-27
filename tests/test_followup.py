import unittest
from datetime import datetime, timedelta, timezone

from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import ConflictError, PermissionDenied, ValidationError
from night_market_foundation.followup_service import FollowupService
from night_market_foundation.gateway import DeliveryResult, ScriptedGateway
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database


class FollowupTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.start = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
        self.clock = FixedClock(self.start)
        self.gateway = ScriptedGateway(default=DeliveryResult("accepted", provider_message_ref="p-ok"))
        self.base = DomainService(self.database, self.clock)
        self.service = FollowupService(self.database, self.clock, self.gateway,
                                       retry_delays_seconds=(60, 300))
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="活动机构一")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="operator", actor_id="ad1", new_actor_id="op1",
                                 display_name="操作员", role="operator", organization_id="o1")
        self.base.register_actor(request_id="reviewer", actor_id="ad1", new_actor_id="rv1",
                                 display_name="复核专家", role="reviewer", organization_id="o1")
        self.base.register_actor(request_id="auditor", actor_id="ad1", new_actor_id="au1",
                                 display_name="审计员", role="auditor", organization_id="o1")
        self.base.register_site(request_id="site", actor_id="op1", site_id="s1",
                                organization_id="o1", name="活动站点", timezone_name="Asia/Shanghai")
        self.service.register_category(request_id="cat-r", actor_id="ad1",
                                       category="routine_wellness", tier="routine")
        self.service.register_category(request_id="cat-u", actor_id="ad1",
                                       category="urgent_recheck", tier="urgent",
                                       handoff_due_hours=48)
        self.service.publish_template(request_id="tpl-sms", actor_id="ad1",
                                     template_key="wellness", channel="sms",
                                     body="您好，{site_name}提醒您规律作息")
        self.vars = {"site_name": "活动站点"}

    def tearDown(self):
        self.database.close()

    def _consent(self, request_id="c1", participant="p1", channels=None):
        return self.service.record_consent(
            request_id=request_id, actor_id="op1", site_id="s1", participant_ref=participant,
            channels=channels if channels is not None else [{"channel": "sms", "address": "13800000000"}])

    def _routine(self, request_id="f1", participant="p1", **kwargs):
        params = dict(actor_id="rv1", summary_ref=request_id + "-sum", site_id="s1",
                      participant_ref=participant, category="routine_wellness",
                      template_key="wellness", followup_window_hours=0, send_window_hours=48,
                      message_variables=self.vars)
        params.update(kwargs)
        return self.service.record_followup(request_id=request_id, **params)

    def _urgent(self, request_id="u1", participant="pu1"):
        return self.service.record_followup(
            request_id=request_id, actor_id="rv1", summary_ref=request_id + "-sum",
            site_id="s1", participant_ref=participant, category="urgent_recheck",
            template_key="wellness")


class RoutingTest(FollowupTestBase):
    def test_unregistered_category_rejected_system_does_not_judge(self):
        self._consent()
        with self.assertRaises(ValidationError):
            self._routine(category="self_guessed_risk")

    def test_two_lists_are_separated(self):
        self._consent()
        routine = self._routine()
        urgent = self._urgent()
        explanation_r = self.service.explain(actor_id="ad1", summary_ref=routine["resource_id"])
        explanation_u = self.service.explain(actor_id="ad1", summary_ref=urgent["resource_id"])
        self.assertTrue(explanation_r.messages)
        self.assertIsNone(explanation_r.handoff)
        self.assertEqual("urgent", explanation_u.tier)
        self.assertIsNotNone(explanation_u.handoff)
        self.assertEqual([], explanation_u.messages)
        self.assertEqual("open", explanation_u.handoff["state"])
        self.assertEqual("reviewer", explanation_u.handoff["assigned_role"])

    def test_no_consented_channel_skips_and_is_explained(self):
        result = self._routine(participant="p-noconsent")
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        self.assertEqual([], explanation.messages)
        self.assertEqual("no_consented_channel", explanation.decisions[0]["reason_code"])
        self.assertEqual(self.service.list_pending()["pending_messages"], 0)

    def test_operator_cannot_register_category(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_category(request_id="x", actor_id="op1",
                                           category="c2", tier="routine")

    def test_auditor_cannot_record_followup(self):
        self._consent()
        with self.assertRaises(PermissionDenied):
            self.service.record_followup(
                request_id="fa", actor_id="au1", summary_ref="fa-sum", site_id="s1",
                participant_ref="p1", category="routine_wellness", template_key="wellness")

    def test_operator_cannot_mark_urgent_category(self):
        with self.assertRaises(PermissionDenied):
            self.service.record_followup(
                request_id="fu-op", actor_id="op1", summary_ref="fu-op-sum", site_id="s1",
                participant_ref="pu1", category="urgent_recheck", template_key="wellness")


class ConsentTest(FollowupTestBase):
    def test_revocation_cancels_unsent_immediately(self):
        self._consent(channels=[{"channel": "sms", "address": "138"},
                                {"channel": "wechat", "address": "wx"}])
        self.service.publish_template(request_id="tpl-wx", actor_id="ad1",
                                      template_key="wellness", channel="wechat", body="提醒")
        result = self._routine(followup_window_hours=24, send_window_hours=72)
        self.service.record_consent(request_id="c2", actor_id="op1", site_id="s1",
                                    participant_ref="p1",
                                    channels=[{"channel": "sms", "address": "138"}])
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        states = {m["channel"]: m["state"] for m in explanation.messages}
        self.assertEqual("queued", states["sms"])
        self.assertEqual("cancelled", states["wechat"])
        reasons = [d["reason_code"] for d in explanation.decisions]
        self.assertIn("consent_scope_changed", reasons)

    def test_full_revocation_cancels_all_unsent(self):
        self._consent()
        result = self._routine(followup_window_hours=24, send_window_hours=72)
        self.service.record_consent(request_id="c2", actor_id="op1", site_id="s1",
                                    participant_ref="p1", channels=[])
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        self.assertEqual("cancelled", explanation.messages[0]["state"])
        self.assertEqual("consent_revoked", explanation.messages[0]["reason_code"])

    def test_consent_replay_is_idempotent(self):
        first = self._consent(request_id="c1")
        second = self._consent(request_id="c1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])


class TemplateTest(FollowupTestBase):
    def test_new_version_does_not_change_queued_text(self):
        self._consent()
        result = self._routine(followup_window_hours=24, send_window_hours=72)
        v1_body = "您好，活动站点提醒您规律作息"
        self.service.publish_template(request_id="tpl-v2", actor_id="ad1",
                                      template_key="wellness", channel="sms",
                                      body="全新正文，不应出现在已排队消息中")
        self.clock._value = self.start + timedelta(hours=25)
        self.service.dispatch_due()
        bodies = [c["body"] for c in self.gateway.calls]
        self.assertIn(v1_body, bodies)
        self.assertTrue(all("全新正文" not in b for b in bodies))
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        self.assertEqual(1, explanation.messages[0]["template_version"])

    def test_recalled_version_blocks_and_goes_manual(self):
        self._consent()
        result = self._routine(followup_window_hours=24, send_window_hours=72)
        self.service.recall_template(request_id="recall", actor_id="ad1",
                                     template_key="wellness", channel="sms", version=1)
        self.clock._value = self.start + timedelta(hours=25)
        outcome = self.service.dispatch_due()
        self.assertEqual(1, outcome["blocked"])
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        self.assertEqual("blocked", explanation.messages[0]["state"])
        self.assertEqual("template_version_retired", explanation.messages[0]["reason_code"])
        self.assertEqual([], self.gateway.calls)

    def test_template_with_health_variable_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.publish_template(request_id="bad", actor_id="ad1",
                                          template_key="bad-tpl", channel="sms",
                                          body="您的血压{blood_pressure}偏高")

    def test_only_site_name_variable_allowed_in_followup(self):
        self._consent()
        with self.assertRaises(ValidationError):
            self._routine(message_variables={"site_name": "站点", "diagnosis": "秘密"})


class DispatchTest(FollowupTestBase):
    def test_send_rechecks_consent_and_template(self):
        self._consent()
        result = self._routine(followup_window_hours=24, send_window_hours=72)
        self.clock._value = self.start + timedelta(hours=25)
        # 模拟绕过登记接口的授权数据更正（如外部系统直接清空渠道）：
        # 发送前复核必须拦截，不能把消息发出去
        self.database.connection.execute(
            "UPDATE participant_consents SET channels_json='[]' WHERE participant_ref='p1'")
        outcome = self.service.dispatch_due()
        self.assertEqual(1, outcome["cancelled"])
        self.assertEqual([], self.gateway.calls)
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        self.assertEqual("cancelled", explanation.messages[0]["state"])

    def test_revocation_before_window_cancels_at_record_time(self):
        self._consent()
        result = self._routine(followup_window_hours=24, send_window_hours=72)
        self.service.record_consent(request_id="c2", actor_id="op1", site_id="s1",
                                    participant_ref="p1", channels=[])
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        self.assertEqual("cancelled", explanation.messages[0]["state"])
        self.clock._value = self.start + timedelta(hours=25)
        outcome = self.service.dispatch_due()
        self.assertEqual(0, outcome["due"])
        self.assertEqual([], self.gateway.calls)

    def test_transient_failure_retries_then_succeeds_with_stable_key(self):
        self._consent()
        result = self._routine(send_window_hours=48)
        message_id = self.service.explain(
            actor_id="ad1", summary_ref=result["resource_id"]).messages[0]["message_id"]
        self.gateway._outcomes[message_id] = DeliveryResult(
            "transient", error_code="busy", retry_after_seconds=60)
        first = self.service.dispatch_due()
        self.assertEqual(1, first["retried"])
        self.clock._value = self.start + timedelta(seconds=61)
        second = self.service.dispatch_due()
        self.assertEqual(1, second["sent"])
        keys = {c["idempotency_key"] for c in self.gateway.calls}
        self.assertEqual({message_id}, keys)

    def test_rejected_goes_manual(self):
        self._consent()
        self._routine(send_window_hours=48)
        self.gateway._default = DeliveryResult("rejected", error_code="bad_address")
        outcome = self.service.dispatch_due()
        self.assertEqual(1, outcome["blocked"])

    def test_send_window_expiry_goes_manual(self):
        self._consent()
        result = self._routine(followup_window_hours=0, send_window_hours=2)
        self.clock._value = self.start + timedelta(hours=3)
        outcome = self.service.dispatch_due()
        self.assertEqual(1, outcome["blocked"])
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        self.assertEqual("send_window_expired", explanation.messages[0]["reason_code"])

    def test_expire_overdue_catches_never_picked_messages(self):
        self._consent()
        self._routine(send_window_hours=2)
        self.clock._value = self.start + timedelta(hours=3)
        self.assertEqual(1, self.service.expire_overdue())
        self.assertEqual(0, self.service.list_pending()["pending_messages"])

    def test_duplicate_delivery_receipt_is_idempotent(self):
        self._consent()
        result = self._routine()
        self.service.dispatch_due()
        message_id = self.service.explain(
            actor_id="ad1", summary_ref=result["resource_id"]).messages[0]["message_id"]
        r1 = self.service.confirm_delivery(message_id=message_id, provider_message_ref="ext-1")
        r2 = self.service.confirm_delivery(message_id=message_id, provider_message_ref="ext-1")
        self.assertEqual(r1.receipt_id, r2.receipt_id)
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM delivery_receipts WHERE message_id=?", (message_id,)
        ).fetchone()["c"]
        self.assertEqual(1, count)

    def test_receipt_contains_no_health_details(self):
        self._consent()
        result = self._routine()
        self.service.dispatch_due()
        message_id = self.service.explain(
            actor_id="ad1", summary_ref=result["resource_id"]).messages[0]["message_id"]
        receipt = self.service.get_receipt(message_id)
        text = str(receipt.__dict__)
        self.assertNotIn("p1", text)
        self.assertNotIn("routine_wellness", text)
        self.assertNotIn("规律作息", text)


class HandoffTest(FollowupTestBase):
    def test_escalation_uses_original_deadline(self):
        urgent = self._urgent()
        handoff_id = self.service.explain(
            actor_id="ad1", summary_ref=urgent["resource_id"]).handoff["handoff_id"]
        # 47 小时：未到期，不升级
        self.clock._value = self.start + timedelta(hours=47)
        self.assertEqual(0, self.service.escalate_due()["escalated"])
        # 48 小时原期限到：升级到 admin，期限不顺延
        self.clock._value = self.start + timedelta(hours=48)
        outcome = self.service.escalate_due()
        self.assertEqual(1, outcome["escalated"])
        handoff = self.service.get_handoff(handoff_id)
        self.assertEqual(2, handoff.level)
        self.assertEqual("admin", handoff.assigned_role)
        self.assertEqual("escalated", handoff.state)

    def test_final_level_breaches_after_grace(self):
        urgent = self._urgent()
        self.clock._value = self.start + timedelta(hours=48)
        self.service.escalate_due()
        self.clock._value = self.start + timedelta(hours=71, minutes=59)
        self.assertEqual(0, self.service.escalate_due()["breached"])
        self.clock._value = self.start + timedelta(hours=72)
        self.assertEqual(1, self.service.escalate_due()["breached"])

    def test_acknowledgement_stops_escalation_and_is_idempotent(self):
        urgent = self._urgent()
        handoff_id = self.service.explain(
            actor_id="ad1", summary_ref=urgent["resource_id"]).handoff["handoff_id"]
        first = self.service.acknowledge_handoff(request_id="ack", actor_id="rv1",
                                                 handoff_id=handoff_id)
        second = self.service.acknowledge_handoff(request_id="ack", actor_id="rv1",
                                                  handoff_id=handoff_id)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.clock._value = self.start + timedelta(hours=100)
        self.assertEqual(0, self.service.escalate_due()["escalated"])
        self.assertEqual("acknowledged", self.service.get_handoff(handoff_id).state)

    def test_operator_cannot_acknowledge_first_level(self):
        urgent = self._urgent()
        handoff_id = self.service.explain(
            actor_id="ad1", summary_ref=urgent["resource_id"]).handoff["handoff_id"]
        with self.assertRaises(PermissionDenied):
            self.service.acknowledge_handoff(request_id="ack-op", actor_id="op1",
                                             handoff_id=handoff_id)

    def test_manual_takeover_cancels_and_is_idempotent(self):
        self._consent()
        routine = self._routine(followup_window_hours=24, send_window_hours=72)
        urgent = self._urgent(participant="pu1")
        first = self.service.takeover(request_id="take", actor_id="rv1",
                                      summary_ref=routine["resource_id"])
        second = self.service.takeover(request_id="take", actor_id="rv1",
                                       summary_ref=routine["resource_id"])
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        explanation = self.service.explain(actor_id="ad1", summary_ref=routine["resource_id"])
        self.assertEqual("cancelled", explanation.messages[0]["state"])
        handoff_id = self.service.explain(
            actor_id="ad1", summary_ref=urgent["resource_id"]).handoff["handoff_id"]
        self.service.takeover(request_id="take-u", actor_id="rv1",
                              summary_ref=urgent["resource_id"])
        self.assertEqual("acknowledged", self.service.get_handoff(handoff_id).state)
        self.assertEqual(0, self.service.list_pending()["pending_handoffs"])


class PersistenceTest(FollowupTestBase):
    def test_pending_tasks_recover_from_persisted_state(self):
        self._consent()
        self._routine(followup_window_hours=24, send_window_hours=72)
        self._urgent()
        recovered = FollowupService(self.database, self.clock, self.gateway)
        pending = recovered.list_pending()
        self.assertEqual(1, pending["pending_messages"])
        self.assertEqual(1, pending["pending_handoffs"])
        # 恢复后的服务可以继续完成发送
        self.clock._value = self.start + timedelta(hours=25)
        recovered.dispatch_due()
        self.assertEqual(0, recovered.list_pending()["pending_messages"])

    def test_changed_payload_on_same_request_id_conflicts(self):
        self._consent()
        self._routine(request_id="dup")
        with self.assertRaises(ConflictError):
            self._routine(request_id="dup", participant="p-other")


class ConcurrencyTest(FollowupTestBase):
    def test_fresh_claim_is_not_taken_by_another_worker(self):
        self._consent()
        result = self._routine()
        message_id = self.service.explain(
            actor_id="ad1", summary_ref=result["resource_id"]).messages[0]["message_id"]
        # 模拟另一个 worker 刚刚声明成功
        self.database.connection.execute(
            "UPDATE scheduled_messages SET state='sending',claim_token='other',claimed_at=? "
            "WHERE message_id=?",
            (self.clock.now().isoformat().replace("+00:00", "Z"), message_id),
        )
        self.database.connection.commit()
        outcome = self.service.dispatch_due()
        self.assertEqual(0, outcome["sent"])
        self.assertEqual([], self.gateway.calls)

    def test_stale_claim_after_worker_crash_is_reclaimed(self):
        self._consent()
        result = self._routine()
        message_id = self.service.explain(
            actor_id="ad1", summary_ref=result["resource_id"]).messages[0]["message_id"]
        old = (self.clock.now() - timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
        self.database.connection.execute(
            "UPDATE scheduled_messages SET state='sending',claim_token='dead',claimed_at=? "
            "WHERE message_id=?",
            (old, message_id),
        )
        self.database.connection.commit()
        outcome = self.service.dispatch_due()
        self.assertEqual(1, outcome["sent"])
        self.assertEqual({message_id}, {c["idempotency_key"] for c in self.gateway.calls})

    def test_acceptance_after_cancellation_reconciles_to_manual(self):
        self._consent()
        result = self._routine()
        message_id = self.service.explain(
            actor_id="ad1", summary_ref=result["resource_id"]).messages[0]["message_id"]
        # 供应商在撤回之后才送达成功回执：不复活消息，转人工核对，但保留最小回执
        self.service.takeover(request_id="take", actor_id="rv1",
                              summary_ref=result["resource_id"])
        receipt = self.service.confirm_delivery(message_id=message_id,
                                                provider_message_ref="late-prov-1")
        explanation = self.service.explain(actor_id="ad1", summary_ref=result["resource_id"])
        self.assertEqual("late-prov-1", receipt.provider_message_ref)
        self.assertEqual("blocked", explanation.messages[0]["state"])
        self.assertEqual("delivery_race_after_cancellation", explanation.messages[0]["reason_code"])


if __name__ == "__main__":
    unittest.main()
