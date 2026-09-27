"""义诊后续联系路由的 HTTP 接口测试。"""

import json
import unittest
from datetime import datetime, timezone

from night_market_foundation.api import route
from night_market_foundation.clock import FixedClock
from night_market_foundation.followup_gateway import RecordingGateway, RecordingNotifier
from night_market_foundation.followup_service import FollowupService
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database


class FollowupApiTest(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc))
        self.database = Database()
        self.base = DomainService(self.database, self.clock)
        self.gateway = RecordingGateway()
        self.followup = FollowupService(self.database, self.clock, self.gateway,
                                        RecordingNotifier())
        self.base.register_organization(request_id="r1", actor_id="bootstrap",
                                        organization_id="o1", name="机构")
        self.base.register_actor(request_id="r2", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="r3", actor_id="a1", new_actor_id="op1",
                                 display_name="操作员", role="operator", organization_id="o1")
        self.base.register_actor(request_id="r4", actor_id="a1", new_actor_id="rv1",
                                 display_name="专家", role="reviewer", organization_id="o1")
        self.base.register_site(request_id="r5", actor_id="op1", site_id="s1",
                                organization_id="o1", name="站点",
                                timezone_name="Asia/Shanghai")
        self.followup.register_template(request_id="t1", actor_id="a1",
                                        code="wellness_reminder", version=1, text="养生提醒")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.base, method, path, body, {"X-Actor-Id": actor},
                     followup=self.followup)

    def test_full_followup_chain_over_http(self):
        status, payload = self.call("POST", "/followup/participants", {
            "request_id": "p1", "participant_id": "pt1", "site_id": "s1",
            "channels": ["sms"], "purposes": ["wellness"]})
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/followup/encounters", {
            "request_id": "e1", "encounter_id": "en1", "participant_id": "pt1",
            "site_id": "s1", "summary": {"service_topic": "推拿"}})
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/followup/expert-decisions", {
            "request_id": "d1", "encounter_id": "en1",
            "category": "wellness_reminder"}, actor="rv1")
        self.assertEqual(201, status)
        followup_id = payload["resource_id"]

        status, payload = self.call("POST", "/followup/clock-ticks", {}, actor="a1")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["due_sent"])

        status, payload = self.call("GET",
                                    f"/followup/followups/{followup_id}/explanation",
                                    None, actor="rv1")
        self.assertEqual(200, status)
        self.assertEqual("delivered", payload["followup"]["status"])
        events = [item["event"] for item in payload["timeline"]]
        self.assertIn("routing.scheduled", events)
        self.assertIn("send.delivered", events)
        rendered = json.dumps(payload, ensure_ascii=False)
        # 现场摘要内容不出现在任何解释输出中，投递事件也不带消息文本
        self.assertNotIn("推拿", rendered)
        delivered = next(item for item in payload["timeline"]
                         if item["event"] == "send.delivered")
        self.assertNotIn("text", delivered["detail"])

    def test_urgent_handoff_claim_and_confirm_over_http(self):
        self.call("POST", "/followup/participants", {
            "request_id": "p1", "participant_id": "pt1", "site_id": "s1",
            "channels": ["sms"], "purposes": ["wellness"]})
        self.call("POST", "/followup/encounters", {
            "request_id": "e1", "encounter_id": "en1", "participant_id": "pt1",
            "site_id": "s1", "summary": {"service_topic": "舌诊"}})
        status, payload = self.call("POST", "/followup/expert-decisions", {
            "request_id": "d1", "encounter_id": "en1",
            "category": "urgent_followup"}, actor="rv1")
        followup_id = payload["resource_id"]
        status, payload = self.call("GET",
                                    f"/followup/followups/{followup_id}/explanation",
                                    None, actor="rv1")
        task_id = payload["tasks"][0]["task_id"]

        status, payload = self.call("POST", "/followup/staff-tasks/claim",
                                    {"task_id": task_id}, actor="rv1")
        self.assertEqual(200, status)
        self.assertEqual("claimed", payload["status"])

        status, payload = self.call("POST", "/followup/staff-tasks/confirm", {
            "request_id": "c1", "task_id": task_id,
            "disposition_code": "accepted"}, actor="rv1")
        self.assertEqual(201, status)

    def test_invalid_channel_is_rejected(self):
        status, payload = self.call("POST", "/followup/participants", {
            "request_id": "p-bad", "participant_id": "pt9", "site_id": "s1",
            "channels": ["carrier_pigeon"], "purposes": ["wellness"]})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_unknown_followup_route_is_404(self):
        status, payload = self.call("GET", "/followup/nope", None)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
