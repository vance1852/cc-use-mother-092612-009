"""运行诊后联系分流的离线端到端验收。

在临时 SQLite 数据库中演示：
- 两条名单分流（常规提醒 / 授权人员交接）；
- 可注入时钟决定的发送窗口、升级期限与到期动作；
- 发送前复核、失败重试幂等、模板版本固化；
- 成功投递只留最小回执；
- 以持久化状态恢复尚未完成的定时任务。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .followup_service import FollowupService
from .gateway import DeliveryResult
from .service import DomainService
from .storage import Database

TEMPLATE_V1_SMS = "您好，来自{site_name}的养生提醒：请注意规律作息。"
TEMPLATE_V2_SMS = "您好，来自{site_name}的新版养生提醒：请注意规律作息与清淡饮食。"
TEMPLATE_V1_WECHAT = "【夜市义诊】{site_name}温馨提醒您注意休息。"


class SequenceGateway:
    """对指定地址的首次调用返回瞬时失败，其余调用均成功的测试网关。"""

    def __init__(self, transient_addresses: set[str] | None = None,
                 retry_after_seconds: int = 60) -> None:
        self._transient_addresses = transient_addresses or set()
        self._retry_after_seconds = retry_after_seconds
        self.calls: list[dict[str, object]] = []

    def send(self, *, channel: str, address: str, body: str, idempotency_key: str) -> DeliveryResult:
        self.calls.append({"channel": channel, "address": address, "body": body,
                           "idempotency_key": idempotency_key})
        if address in self._transient_addresses:
            self._transient_addresses.discard(address)
            return DeliveryResult("transient", error_code="gateway_busy",
                                  retry_after_seconds=self._retry_after_seconds)
        return DeliveryResult("accepted", provider_message_ref=f"prov-{len(self.calls)}")


def run() -> dict[str, object]:
    """执行完整分流链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "followup_acceptance.sqlite3")
        start = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
        clock = FixedClock(start)
        gateway = SequenceGateway(transient_addresses={"13800000002"})
        base = DomainService(database, clock)
        followup = FollowupService(database, clock, gateway, retry_delays_seconds=(60,))

        # 基础建档
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="org-001", name="示范活动机构")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="系统管理员", role="admin", organization_id="org-001")
        base.register_actor(request_id="reviewer", actor_id="admin-001", new_actor_id="rev-001",
                            display_name="复核专家", role="reviewer", organization_id="org-001")
        base.register_actor(request_id="operator", actor_id="admin-001", new_actor_id="op-001",
                            display_name="现场操作员", role="operator", organization_id="org-001")
        base.register_site(request_id="site", actor_id="op-001", site_id="site-001",
                           organization_id="org-001", name="一号活动站点", timezone_name="Asia/Shanghai")

        # 专家类别词汇：系统只按登记类别路由
        followup.register_category(request_id="cat-routine", actor_id="admin-001",
                                   category="routine_wellness", tier="routine")
        followup.register_category(request_id="cat-urgent", actor_id="admin-001",
                                   category="urgent_recheck", tier="urgent", handoff_due_hours=48)

        # 模板版本发布
        followup.publish_template(request_id="tpl-sms-v1", actor_id="admin-001",
                                  template_key="wellness", channel="sms", body=TEMPLATE_V1_SMS)
        followup.publish_template(request_id="tpl-wechat-v1", actor_id="admin-001",
                                  template_key="wellness", channel="wechat", body=TEMPLATE_V1_WECHAT)

        # 参与者授权范围
        followup.record_consent(request_id="consent-routine", actor_id="op-001", site_id="site-001",
                                participant_ref="p-routine",
                                channels=[{"channel": "sms", "address": "13800000001"},
                                          {"channel": "wechat", "address": "wx-routine"}])
        followup.record_consent(request_id="consent-retry", actor_id="op-001", site_id="site-001",
                                participant_ref="p-retry",
                                channels=[{"channel": "sms", "address": "13800000002"}])
        followup.record_consent(request_id="consent-tpl", actor_id="op-001", site_id="site-001",
                                participant_ref="p-template",
                                channels=[{"channel": "sms", "address": "13800000003"}])
        followup.record_consent(request_id="consent-late", actor_id="op-001", site_id="site-001",
                                participant_ref="p-late",
                                channels=[{"channel": "sms", "address": "13800000004"}])
        followup.record_consent(request_id="consent-u2", actor_id="op-001", site_id="site-001",
                                participant_ref="p-urgent2",
                                channels=[{"channel": "sms", "address": "13800000005"}])

        variables = {"site_name": "一号活动站点"}

        # 现场登记：常规（立即到期，72 小时发送窗口）
        routine = followup.record_followup(request_id="case-routine", actor_id="rev-001",
                                           summary_ref="sum-routine", site_id="site-001",
                                           participant_ref="p-routine", category="routine_wellness",
                                           template_key="wellness", followup_window_hours=0,
                                           send_window_hours=72, message_variables=variables)
        # 重试用例：第一次网关瞬时失败
        followup.record_followup(request_id="case-retry", actor_id="rev-001",
                                 summary_ref="sum-retry", site_id="site-001",
                                 participant_ref="p-retry", category="routine_wellness",
                                 template_key="wellness", followup_window_hours=0,
                                 send_window_hours=72, message_variables=variables)
        # 模板版本用例：24 小时后才发送
        followup.record_followup(request_id="case-tpl", actor_id="rev-001",
                                 summary_ref="sum-template", site_id="site-001",
                                 participant_ref="p-template", category="routine_wellness",
                                 template_key="wellness", followup_window_hours=24,
                                 send_window_hours=72, message_variables=variables)
        # 恢复用例：200 小时后才到期，验收结束时仍未完成
        followup.record_followup(request_id="case-late", actor_id="rev-001",
                                 summary_ref="sum-late", site_id="site-001",
                                 participant_ref="p-late", category="routine_wellness",
                                 template_key="wellness", followup_window_hours=200,
                                 send_window_hours=240, message_variables=variables)

        # 高风险：48 小时期限，一条无人确认，一条当天确认
        urgent1 = followup.record_followup(request_id="case-urgent1", actor_id="rev-001",
                                           summary_ref="sum-urgent1", site_id="site-001",
                                           participant_ref="p-urgent1", category="urgent_recheck",
                                           template_key="wellness")
        urgent2 = followup.record_followup(request_id="case-urgent2", actor_id="rev-001",
                                           summary_ref="sum-urgent2", site_id="site-001",
                                           participant_ref="p-urgent2", category="urgent_recheck",
                                           template_key="wellness")
        handoff1_id = followup.explain(actor_id="admin-001",
                                       summary_ref="sum-urgent1").handoff["handoff_id"]
        handoff2_id = followup.explain(actor_id="admin-001",
                                       summary_ref="sum-urgent2").handoff["handoff_id"]

        # t0：发送到期常规消息（含一次瞬时失败后的重试）
        outcome_t0 = followup.dispatch_due(actor_id="system-dispatcher")
        retry_message_id = None
        for item in followup.explain(actor_id="admin-001", summary_ref="sum-retry").messages:
            retry_message_id = item["message_id"]
        # 推进 61 秒后重试成功，且幂等键不变
        clock._value = start + timedelta(seconds=61)
        outcome_retry = followup.dispatch_due(actor_id="system-dispatcher")
        retry_calls = [c for c in gateway.calls if c["idempotency_key"] == retry_message_id]

        # 重复成功回执幂等：再投递一次供应商回执不产生第二张回执
        receipt_first = followup.confirm_delivery(message_id=retry_message_id,
                                                  provider_message_ref="prov-manual-1")
        receipt_second = followup.confirm_delivery(message_id=retry_message_id,
                                                   provider_message_ref="prov-manual-1")

        # t0+1h：复核专家确认第二条高风险事项
        clock._value = start + timedelta(hours=1)
        ack = followup.acknowledge_handoff(request_id="ack-u2", actor_id="rev-001",
                                           handoff_id=handoff2_id)
        ack_replay = followup.acknowledge_handoff(request_id="ack-u2", actor_id="rev-001",
                                                  handoff_id=handoff2_id)

        # t0+10h：模板发布 v2
        clock._value = start + timedelta(hours=10)
        followup.publish_template(request_id="tpl-sms-v2", actor_id="admin-001",
                                  template_key="wellness", channel="sms", body=TEMPLATE_V2_SMS)

        # t0+25h：模板用例发送，正文必须仍是排队时固化的 v1
        clock._value = start + timedelta(hours=25)
        followup.dispatch_due(actor_id="system-dispatcher")
        tpl_message = followup.explain(actor_id="admin-001", summary_ref="sum-template").messages[0]
        tpl_call = next(c for c in gateway.calls
                        if c["idempotency_key"] == tpl_message["message_id"])

        # t0+49h：第一条高风险事项超过 48h 原期限仍未确认 → 升级
        clock._value = start + timedelta(hours=49)
        escalation = followup.escalate_due(actor_id="system-scheduler")
        handoff1_after_escalation = followup.get_handoff(handoff1_id)

        # t0+74h：升级后 24h 宽限仍未确认 → 逾期；已确认的第二条不受影响
        clock._value = start + timedelta(hours=74)
        breach = followup.escalate_due(actor_id="system-scheduler")
        handoff1_final = followup.get_handoff(handoff1_id)
        handoff2_final = followup.get_handoff(handoff2_id)

        # 最小回执核验：字段中没有参与者编号、类别与正文
        routine_items = followup.explain(actor_id="admin-001", summary_ref="sum-routine").messages
        receipt = followup.get_receipt(routine_items[0]["message_id"])
        receipt_keys = set(receipt.__dict__.keys())

        # 可解释性：常规案例包含安排决策；高风险案例包含升级轨迹
        explanation_routine = followup.explain(actor_id="admin-001", summary_ref="sum-routine")
        explanation_urgent = followup.explain(actor_id="admin-001", summary_ref="sum-urgent1")

        # 模拟服务重启：新服务对象从同一 SQLite 恢复未完成任务
        recovered = FollowupService(database, clock)
        pending = recovered.list_pending()

        valid, event_count = base.verify_audit()
        database.close()

        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "dispatch_t0": outcome_t0,
            "dispatch_retry": outcome_retry,
            "retry_attempts": len(retry_calls),
            "retry_idempotency_key_stable": len({c["idempotency_key"] for c in retry_calls}) == 1,
            "duplicate_receipt_idempotent": receipt_first.receipt_id == receipt_second.receipt_id,
            "receipt_keys": sorted(receipt_keys),
            "receipt_is_minimal": receipt_keys == {"receipt_id", "message_id", "channel",
                                                   "provider_message_ref", "delivered_at"},
            "urgent2_acknowledged": handoff2_final.state == "acknowledged",
            "ack_replayed": ack_replay["replayed"] is True and ack["replayed"] is False,
            "escalation": escalation,
            "breach": breach,
            "handoff1_final_state": handoff1_final.state,
            "handoff1_final_level": handoff1_final.level,
            "template_message_version": tpl_message["template_version"],
            "template_body_kept_v1": tpl_call["body"] == TEMPLATE_V1_SMS.format(**variables),
            "routine_decisions": [d["reason_code"] for d in explanation_routine.decisions],
            "urgent_decision_scopes": [d["scope"] for d in explanation_urgent.decisions],
            "pending_after_restart": pending,
        }
        expected = {
            "audit_valid": True,
            "retry_attempts": 2,
            "retry_idempotency_key_stable": True,
            "duplicate_receipt_idempotent": True,
            "receipt_is_minimal": True,
            "urgent2_acknowledged": True,
            "ack_replayed": True,
            "handoff1_final_state": "breached",
            "handoff1_final_level": 2,
            "template_message_version": 1,
            "template_body_kept_v1": True,
        }
        result["expectations_met"] = all(result[k] == v for k, v in expected.items()) \
            and result["dispatch_t0"]["sent"] == 2 \
            and result["dispatch_t0"]["retried"] == 1 \
            and result["dispatch_retry"]["sent"] == 1 \
            and escalation["escalated"] == 1 and breach["breached"] == 1 \
            and pending["pending_messages"] == 1 and pending["pending_handoffs"] == 0
        result["status"] = "ok" if result["expectations_met"] and valid else "failed"
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
