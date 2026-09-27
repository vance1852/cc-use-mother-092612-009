"""实现诊后联系分流：接收摘要、按专家类别分流、定时发送与期限升级。

边界原则：
- 系统只按专家给出的、事先登记的类别路由，任何环节都不生成诊疗判断；
- 常规养生提醒与授权人员交接使用两套独立的持久化名单；
- 消息正文与模板版本在排队时固化，模板更新不影响已排队文本；
- 真正发送前再次核对授权状态与模板版本；
- 成功投递只保留无法反推健康细节的最小回执。
"""

from __future__ import annotations

import json
import re
import string
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from . import followup as policy
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .gateway import DeliveryResult, MessageGateway
from .models_followup import DeliveryReceipt, Explanation, FollowupSummary, Handoff
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
WRITE_ROLES = frozenset({"admin", "operator", "reviewer"})
EXPERT_ROLES = frozenset({"admin", "reviewer"})


class FollowupService:
    """协调授权、幂等、发送窗口、升级期限与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None,
                 gateway: MessageGateway | None = None,
                 retry_delays_seconds: tuple[int, ...] = (300, 1800, 7200, 14400),
                 escalation_grace_hours: int = 24,
                 claim_timeout_seconds: int = 900) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.gateway = gateway
        self.retry_delays_seconds = retry_delays_seconds
        self.escalation_grace_hours = escalation_grace_hours
        # 超过该时长仍停留在 sending 的声明视为 worker 崩溃，可被其他 worker 接管
        self.claim_timeout_seconds = claim_timeout_seconds

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now()

    def _stamp(self) -> str:
        return self._now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _hours(self, value: Any, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{field} 必须是正整数小时")
        return value

    def _actor(self, connection, actor_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return dict(row)

    def _require(self, actor: dict[str, Any], *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return dict(row)

    def _same_org(self, actor: dict[str, Any], site: dict[str, Any]) -> None:
        if actor["organization_id"] != site["organization_id"] and actor["role"] != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    def _decision(self, connection, *, summary_ref: str, scope: str, reason_code: str,
                  channel: str | None = None, detail: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO followup_decisions(decision_id,summary_ref,scope,channel,reason_code,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, summary_ref, scope, channel, reason_code,
             canonical_json(detail or {}), self._stamp()),
        )

    def _audit(self, connection, *, actor_id: str, action: str, resource_id: str,
               detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type="followup", resource_id=resource_id,
                     detail=detail, occurred_at=self._stamp())

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"replayed": True, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"]}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._stamp()),
        )
        return {"replayed": False, "resource_type": resource_type, "resource_id": resource_id}

    def _normalize_channels(self, raw: Any) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            raise ValidationError("channels 必须是渠道对象列表")
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValidationError("渠道项必须是对象")
            channel = str(item.get("channel", "")).strip()
            address = str(item.get("address", "")).strip()
            if channel not in policy.ALLOWED_CHANNELS:
                raise ValidationError("渠道不在允许范围内")
            if not address or len(address) > 200:
                raise ValidationError("渠道地址不能为空且不能超过 200 个字符")
            if channel in seen:
                raise ValidationError("同一渠道不能重复授权")
            seen.add(channel)
            normalized.append({"channel": channel, "address": address})
        return normalized

    # ------------------------------------------------------------------
    # 登记：授权范围、专家类别、模板版本
    # ------------------------------------------------------------------

    def register_category(self, *, request_id: str, actor_id: str, category: str, tier: str,
                          handoff_due_hours: int | None = None) -> dict[str, Any]:
        """登记专家后续类别词汇；系统不接受词汇表之外的类别。"""

        payload = {"actor_id": actor_id, "category": category, "tier": tier,
                   "handoff_due_hours": handoff_due_hours}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            category = self._identifier(category, "category")
            if tier not in policy.ALLOWED_TIERS:
                raise ValidationError("tier 不在允许范围内")
            if tier == policy.TIER_URGENT:
                handoff_due_hours = self._hours(handoff_due_hours, "handoff_due_hours")
            elif handoff_due_hours is not None:
                raise ValidationError("常规类别不能设置交接期限")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM followup_categories WHERE category=?", (category,)).fetchone():
                    raise ConflictError("类别已经登记")
                connection.execute(
                    "INSERT INTO followup_categories(category,tier,handoff_due_hours,active,created_by,created_at) "
                    "VALUES(?,?,?,1,?,?)",
                    (category, tier, handoff_due_hours, actor_id, self._stamp()),
                )
                self._audit(connection, actor_id=actor_id, action="followup.category_registered",
                            resource_id=category, detail={"tier": tier,
                                                          "handoff_due_hours": handoff_due_hours})
                return "followup_category", category, {"category": category, "tier": tier}

            return self._idempotent(connection, request_id=request_id, action="register_followup_category",
                                    payload=payload, create=create)

    def publish_template(self, *, request_id: str, actor_id: str, template_key: str,
                         channel: str, body: str) -> dict[str, Any]:
        """发布一个不可变的新模板版本；旧版本停用但已排队消息继续引用旧版本。"""

        payload = {"actor_id": actor_id, "template_key": template_key, "channel": channel, "body": body}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            template_key = self._identifier(template_key, "template_key")
            if channel not in policy.ALLOWED_CHANNELS:
                raise ValidationError("渠道不在允许范围内")
            body = str(body or "").strip()
            if not body or len(body) > 1000:
                raise ValidationError("模板正文不能为空且不能超过 1000 个字符")
            fields = {name for _, name, _, _ in string.Formatter().parse(body) if name}
            if not fields <= policy.ALLOWED_TEMPLATE_VARIABLES:
                raise ValidationError("模板只能使用与健康细节无关的占位符")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM message_templates WHERE template_key=? AND channel=?",
                    (template_key, channel),
                ).fetchone()
                version = (row["version"] or 0) + 1
                now = self._stamp()
                connection.execute(
                    "UPDATE message_templates SET active=0, retired_at=? WHERE template_key=? AND channel=? AND active=1",
                    (now, template_key, channel),
                )
                connection.execute(
                    "INSERT INTO message_templates(template_key,channel,version,body,active,published_at,retired_at) "
                    "VALUES(?,?,?,?,1,?,NULL)",
                    (template_key, channel, version, body, now),
                )
                self._audit(connection, actor_id=actor_id, action="followup.template_published",
                            resource_id=f"{template_key}:{channel}:{version}",
                            detail={"template_key": template_key, "channel": channel, "version": version})
                return ("message_template", f"{template_key}:{channel}:{version}",
                        {"template_key": template_key, "channel": channel, "version": version})

            return self._idempotent(connection, request_id=request_id, action="publish_template",
                                    payload=payload, create=create)

    def recall_template(self, *, request_id: str, actor_id: str, template_key: str,
                        channel: str, version: int) -> dict[str, Any]:
        """主动召回一个已发布版本：不删除文本，但已排队消息发送前复核时转人工。"""

        payload = {"actor_id": actor_id, "template_key": template_key,
                   "channel": channel, "version": version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            template_key = self._identifier(template_key, "template_key")
            if channel not in policy.ALLOWED_CHANNELS:
                raise ValidationError("渠道不在允许范围内")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValidationError("版本号必须是正整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM message_templates WHERE template_key=? AND channel=? AND version=?",
                    (template_key, channel, version),
                ).fetchone()
                if row is None:
                    raise NotFoundError("模板版本不存在")
                now = self._stamp()
                if row["recalled_at"]:
                    return ("message_template", f"{template_key}:{channel}:{version}",
                            {"template_key": template_key, "channel": channel,
                             "version": version, "already_recalled": True})
                connection.execute(
                    "UPDATE message_templates SET active=0,retired_at=COALESCE(retired_at,?),recalled_at=? "
                    "WHERE template_key=? AND channel=? AND version=?",
                    (now, now, template_key, channel, version),
                )
                self._audit(connection, actor_id=actor_id, action="followup.template_recalled",
                            resource_id=f"{template_key}:{channel}:{version}",
                            detail={"template_key": template_key, "channel": channel,
                                    "version": version})
                return ("message_template", f"{template_key}:{channel}:{version}",
                        {"template_key": template_key, "channel": channel,
                         "version": version, "already_recalled": False})

            return self._idempotent(connection, request_id=request_id, action="recall_template",
                                    payload=payload, create=create)

    def record_consent(self, *, request_id: str, actor_id: str, site_id: str,
                       participant_ref: str, channels: list[dict[str, str]]) -> dict[str, Any]:
        """记录或替换参与者选择的联系渠道范围，范围缩小时立即取消未发送内容。"""

        payload = {"actor_id": actor_id, "site_id": site_id,
                   "participant_ref": participant_ref, "channels": channels}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            site = self._site(connection, site_id)
            self._same_org(actor, site)
            participant_ref = self._identifier(participant_ref, "participant_ref")
            normalized = self._normalize_channels(channels)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM participant_consents WHERE site_id=? AND participant_ref=?",
                    (site_id, participant_ref),
                ).fetchone()
                previous = [c["channel"] for c in json.loads(row["channels_json"])] if row else []
                version = (row["version"] + 1) if row else 1
                now = self._stamp()
                connection.execute(
                    "INSERT INTO participant_consents(site_id,participant_ref,channels_json,version,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(site_id,participant_ref) DO UPDATE SET "
                    "channels_json=excluded.channels_json,version=excluded.version,updated_at=excluded.updated_at",
                    (site_id, participant_ref, canonical_json(normalized), version, now),
                )
                # 范围缩小：立即取消尚未发送的内容
                current_channels = {c["channel"] for c in normalized}
                dropped = [c for c in previous if c not in current_channels]
                cancelled: list[str] = []
                if dropped:
                    placeholders = ",".join("?" for _ in dropped)
                    pending = connection.execute(
                        f"SELECT * FROM scheduled_messages WHERE site_id=? AND participant_ref=? "
                        f"AND channel IN ({placeholders}) AND state IN ('queued','sending')",
                        [site_id, participant_ref, *dropped],
                    ).fetchall()
                    for message in pending:
                        reason = (policy.REASON_CONSENT_REVOKED if not current_channels
                                  else policy.REASON_CONSENT_SCOPE_CHANGED)
                        connection.execute(
                            "UPDATE scheduled_messages SET state='cancelled',reason_code=?,"
                            "claim_token=NULL,claimed_at=NULL,updated_at=? "
                            "WHERE message_id=? AND state IN ('queued','sending')",
                            (reason, now, message["message_id"]),
                        )
                        self._decision(connection, summary_ref=message["summary_ref"], scope="cancel",
                                       channel=message["channel"], reason_code=reason,
                                       detail={"consent_version": version})
                        self._audit(connection, actor_id=actor_id, action="followup.message_cancelled",
                                    resource_id=message["message_id"],
                                    detail={"channel": message["channel"], "reason_code": reason})
                        cancelled.append(message["message_id"])
                self._audit(connection, actor_id=actor_id, action="followup.consent_recorded",
                            resource_id=f"{site_id}:{participant_ref}",
                            detail={"version": version, "channels": sorted(current_channels),
                                    "cancelled": cancelled})
                return ("participant_consent", f"{site_id}:{participant_ref}",
                        {"participant_ref": participant_ref, "version": version,
                         "channels": sorted(current_channels), "cancelled_messages": cancelled})

            return self._idempotent(connection, request_id=request_id, action="record_consent",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 现场登记：接收最少必要摘要并分流
    # ------------------------------------------------------------------

    def record_followup(self, *, request_id: str, actor_id: str, summary_ref: str, site_id: str,
                        participant_ref: str, category: str, template_key: str,
                        followup_window_hours: int = 0, send_window_hours: int = 48,
                        message_variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """接收现场摘要与专家后续类别，分别进入两条名单。"""

        payload = {"actor_id": actor_id, "summary_ref": summary_ref, "site_id": site_id,
                   "participant_ref": participant_ref, "category": category,
                   "template_key": template_key, "followup_window_hours": followup_window_hours,
                   "send_window_hours": send_window_hours,
                   "message_variables": message_variables or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            site = self._site(connection, site_id)
            self._same_org(actor, site)
            summary_ref = self._identifier(summary_ref, "summary_ref")
            participant_ref = self._identifier(participant_ref, "participant_ref")
            template_key = self._identifier(template_key, "template_key")
            followup_window_hours = self._hours(followup_window_hours, "followup_window_hours") \
                if followup_window_hours else 0
            send_window_hours = self._hours(send_window_hours, "send_window_hours")
            if send_window_hours < followup_window_hours:
                raise ValidationError("发送窗口不能早于最早发送时间")
            message_variables = message_variables or {}
            if not set(message_variables) <= policy.ALLOWED_TEMPLATE_VARIABLES:
                raise ValidationError("只允许传入与健康细节无关的模板变量")
            category_row = connection.execute(
                "SELECT * FROM followup_categories WHERE category=? AND active=1", (category,)
            ).fetchone()
            if category_row is None:
                raise ValidationError("专家后续类别未登记，系统不能自行判断")
            tier = category_row["tier"]
            # 紧急复查标记属于专家判断的落库，操作员只能录入常规类别
            if tier == policy.TIER_URGENT:
                self._require(actor, *EXPERT_ROLES)
            handoff_due_hours = category_row["handoff_due_hours"]

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM followup_summaries WHERE summary_ref=?",
                                      (summary_ref,)).fetchone():
                    raise ConflictError("摘要编号已经存在")
                now = self._stamp()
                connection.execute(
                    "INSERT INTO followup_summaries(summary_ref,site_id,participant_ref,recorded_by,recorded_at,"
                    "tier,category,followup_window_hours,send_window_hours,handoff_due_hours,template_key,variables_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (summary_ref, site_id, participant_ref, actor_id, now, tier, category,
                     followup_window_hours, send_window_hours, handoff_due_hours,
                     template_key, canonical_json(message_variables)),
                )
                self._audit(connection, actor_id=actor_id, action="followup.case_recorded",
                            resource_id=summary_ref,
                            detail={"site_id": site_id, "tier": tier, "category": category})
                scheduled: list[str] = []
                handoff_id: str | None = None
                if tier == policy.TIER_URGENT:
                    handoff_id = self._open_handoff(connection, actor_id=actor_id, summary_ref=summary_ref,
                                                    site_id=site_id, participant_ref=participant_ref,
                                                    category=category, handoff_due_hours=handoff_due_hours,
                                                    now_dt=self._now())
                else:
                    scheduled = self._schedule_routine(connection, actor_id=actor_id, summary_ref=summary_ref,
                                                       site_id=site_id, participant_ref=participant_ref,
                                                       template_key=template_key,
                                                       message_variables=message_variables,
                                                       followup_window_hours=followup_window_hours,
                                                       send_window_hours=send_window_hours)
                return ("followup_summary", summary_ref,
                        {"summary_ref": summary_ref, "tier": tier, "category": category,
                         "scheduled_messages": scheduled, "handoff_id": handoff_id})

            return self._idempotent(connection, request_id=request_id, action="record_followup",
                                    payload=payload, create=create)

    def _open_handoff(self, connection, *, actor_id: str, summary_ref: str, site_id: str,
                      participant_ref: str, category: str, handoff_due_hours: int,
                      now_dt: datetime) -> str:
        # 同一摘要复用既有交接，重复回执/重复登记不产生第二条名单记录
        existing = connection.execute(
            "SELECT handoff_id FROM followup_handoffs WHERE summary_ref=?", (summary_ref,)
        ).fetchone()
        if existing:
            self._decision(connection, summary_ref=summary_ref, scope="intake",
                           reason_code=policy.REASON_URGENT_HANDOFF,
                           detail={"reused": True, "handoff_id": existing["handoff_id"]})
            return existing["handoff_id"]
        handoff_id = uuid.uuid4().hex
        due_at = (now_dt + timedelta(hours=handoff_due_hours)).isoformat().replace("+00:00", "Z")
        now = self._stamp()
        connection.execute(
            "INSERT INTO followup_handoffs(handoff_id,summary_ref,site_id,participant_ref,category,tier,state,"
            "level,assigned_role,assigned_actor_id,due_at,reason_code,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'open',1,'reviewer',NULL,?,?,?,?)",
            (handoff_id, summary_ref, site_id, participant_ref, category, policy.TIER_URGENT,
             due_at, policy.REASON_URGENT_HANDOFF, now, now),
        )
        self._decision(connection, summary_ref=summary_ref, scope="intake",
                       reason_code=policy.REASON_URGENT_HANDOFF,
                       detail={"handoff_id": handoff_id, "due_at": due_at,
                               "handoff_due_hours": handoff_due_hours})
        self._audit(connection, actor_id=actor_id, action="followup.handoff_opened",
                    resource_id=handoff_id, detail={"summary_ref": summary_ref, "category": category,
                                                    "due_at": due_at})
        return handoff_id

    def _schedule_routine(self, connection, *, actor_id: str, summary_ref: str, site_id: str,
                          participant_ref: str, template_key: str,
                          message_variables: dict[str, Any],
                          followup_window_hours: int, send_window_hours: int) -> list[str]:
        consent_row = connection.execute(
            "SELECT * FROM participant_consents WHERE site_id=? AND participant_ref=?",
            (site_id, participant_ref),
        ).fetchone()
        channels = json.loads(consent_row["channels_json"]) if consent_row else []
        now_dt = self._now()
        scheduled_for = (now_dt + timedelta(hours=followup_window_hours)).isoformat().replace("+00:00", "Z")
        send_before = (now_dt + timedelta(hours=send_window_hours)).isoformat().replace("+00:00", "Z")
        if not channels:
            self._decision(connection, summary_ref=summary_ref, scope="intake",
                           reason_code=policy.REASON_NO_CONSENTED_CHANNEL,
                           detail={"template_key": template_key})
            self._audit(connection, actor_id=actor_id, action="followup.message_skipped",
                        resource_id=summary_ref,
                        detail={"reason_code": policy.REASON_NO_CONSENTED_CHANNEL})
            return []
        scheduled: list[str] = []
        for consented in channels:
            channel = consented["channel"]
            template = connection.execute(
                "SELECT * FROM message_templates WHERE template_key=? AND channel=? AND active=1",
                (template_key, channel),
            ).fetchone()
            if template is None:
                self._decision(connection, summary_ref=summary_ref, scope="intake", channel=channel,
                               reason_code=policy.REASON_NO_PUBLISHED_TEMPLATE,
                               detail={"template_key": template_key})
                self._audit(connection, actor_id=actor_id, action="followup.message_skipped",
                            resource_id=summary_ref, detail={"channel": channel,
                                                              "reason_code": policy.REASON_NO_PUBLISHED_TEMPLATE})
                continue
            body_text = template["body"]
            try:
                body = body_text.format(**message_variables)
            except (KeyError, IndexError) as exc:
                raise ValidationError(f"模板变量缺失：{exc.args[0]}") from exc
            message_id = uuid.uuid4().hex
            # 排队时固化模板版本、模板正文哈希与渲染快照；之后模板发布新版本不会改变这条文本
            connection.execute(
                "INSERT INTO scheduled_messages(message_id,summary_ref,site_id,participant_ref,channel,"
                "template_key,template_version,consent_version,template_body_hash,body_snapshot,state,"
                "scheduled_for,send_before,attempts,reason_code,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,0,?,?,?)",
                (message_id, summary_ref, site_id, participant_ref, channel, template_key,
                 template["version"], consent_row["version"], digest(body_text), body,
                 scheduled_for, send_before,
                 policy.REASON_ROUTINE_SCHEDULED, self._stamp(), self._stamp()),
            )
            self._decision(connection, summary_ref=summary_ref, scope="intake", channel=channel,
                           reason_code=policy.REASON_ROUTINE_SCHEDULED,
                           detail={"message_id": message_id, "template_version": template["version"],
                                   "scheduled_for": scheduled_for, "send_before": send_before})
            self._audit(connection, actor_id=actor_id, action="followup.message_scheduled",
                        resource_id=message_id, detail={"summary_ref": summary_ref, "channel": channel,
                                                        "template_version": template["version"]})
            scheduled.append(message_id)
        return scheduled

    # ------------------------------------------------------------------
    # 定时发送：发送前复核、失败重试、最小回执
    # ------------------------------------------------------------------

    def dispatch_due(self, *, actor_id: str = "system-dispatcher", limit: int = 100) -> dict[str, int]:
        """发送所有到期消息。无状态设计：每次从持久化状态恢复未完成任务。"""

        if self.gateway is None:
            raise ValidationError("未配置消息网关")
        now_stamp = self._stamp()
        stale_before = (self._now() - timedelta(seconds=self.claim_timeout_seconds)) \
            .isoformat().replace("+00:00", "Z")
        # 候选包含到期消息，以及 worker 崩溃后残留在 sending、声明已超时的消息
        rows = self.database.connection.execute(
            "SELECT * FROM scheduled_messages WHERE "
            "(state='queued' AND scheduled_for<=?) "
            "OR (state='sending' AND claimed_at<=?) "
            "ORDER BY scheduled_for, message_id LIMIT ?",
            (now_stamp, stale_before, limit),
        ).fetchall()
        sent = retried = blocked = cancelled = skipped = 0
        for row in rows:
            outcome = self._dispatch_one(dict(row), actor_id=actor_id)
            sent += outcome == "sent"
            retried += outcome == "retried"
            blocked += outcome == "blocked"
            cancelled += outcome == "cancelled"
            skipped += outcome == "skipped"
        return {"due": len(rows), "sent": sent, "retried": retried,
                "blocked": blocked, "cancelled": cancelled, "skipped": skipped}

    def _claim(self, connection, message_id: str, token: str, now_stamp: str,
               stale_before: str) -> dict[str, Any] | None:
        """原子占用：只有把状态成功比较交换为自己令牌的 worker 才能发送。"""

        cursor = connection.execute(
            "UPDATE scheduled_messages SET state='sending',claim_token=?,claimed_at=?,"
            "attempts=attempts+1,updated_at=? "
            "WHERE message_id=? AND ("
            "state='queued' OR (state='sending' AND claimed_at<=?))",
            (token, now_stamp, now_stamp, message_id, stale_before),
        )
        if cursor.rowcount != 1:
            return None
        return dict(connection.execute(
            "SELECT * FROM scheduled_messages WHERE message_id=?", (message_id,)
        ).fetchone())

    def _dispatch_one(self, message: dict[str, Any], *, actor_id: str) -> str:
        token = uuid.uuid4().hex
        now_dt = self._now()
        now_stamp = self._stamp()
        stale_before = (now_dt - timedelta(seconds=self.claim_timeout_seconds)) \
            .isoformat().replace("+00:00", "Z")

        # 第一步：短事务内原子占用，占用成功后才做发送前复核
        with self.database.transaction(immediate=True) as connection:
            claimed = self._claim(connection, message["message_id"], token, now_stamp, stale_before)
            if claimed is None:
                return "skipped"
            send_before = datetime.fromisoformat(claimed["send_before"].replace("Z", "+00:00"))
            if now_dt > send_before:
                return self._block_message(connection, message=claimed, actor_id=actor_id,
                                           reason_code=policy.REASON_SEND_WINDOW_EXPIRED,
                                           detail={"at": now_stamp})
            consent_row = connection.execute(
                "SELECT * FROM participant_consents WHERE site_id=? AND participant_ref=?",
                (claimed["site_id"], claimed["participant_ref"]),
            ).fetchone()
            consented = json.loads(consent_row["channels_json"]) if consent_row else []
            match = next((c for c in consented if c["channel"] == claimed["channel"]), None)
            if match is None:
                reason = (policy.REASON_CONSENT_REVOKED if not consented
                          else policy.REASON_CONSENT_SCOPE_CHANGED)
                connection.execute(
                    "UPDATE scheduled_messages SET state='cancelled',reason_code=?,"
                    "claim_token=NULL,claimed_at=NULL,updated_at=? WHERE message_id=? AND claim_token=?",
                    (reason, now_stamp, claimed["message_id"], token),
                )
                self._decision(connection, summary_ref=claimed["summary_ref"], scope="dispatch",
                               channel=claimed["channel"], reason_code=reason,
                               detail={"consent_version": consent_row["version"] if consent_row else None})
                self._audit(connection, actor_id=actor_id, action="followup.message_cancelled",
                            resource_id=claimed["message_id"],
                            detail={"channel": claimed["channel"], "reason_code": reason})
                return "cancelled"
            template = connection.execute(
                "SELECT * FROM message_templates WHERE template_key=? AND channel=? AND version=?",
                (claimed["template_key"], claimed["channel"], claimed["template_version"]),
            ).fetchone()
            if template is None:
                return self._block_message(connection, message=claimed, actor_id=actor_id,
                                           reason_code=policy.REASON_TEMPLATE_MISSING, detail={})
            if template["recalled_at"]:
                # 管理员主动召回的版本停止发送并转人工；
                # 仅仅被新版本取代（active=0）不在此列，排队时固化的文本照常发出
                return self._block_message(connection, message=claimed, actor_id=actor_id,
                                           reason_code=policy.REASON_TEMPLATE_RETIRED,
                                           detail={"template_version": template["version"],
                                                   "recalled_at": template["recalled_at"]})
            if digest(template["body"]) != claimed["template_body_hash"]:
                # 不可变版本的正文出现差异属于异常，绝不能悄悄换成别的文本
                return self._block_message(connection, message=claimed, actor_id=actor_id,
                                           reason_code=policy.REASON_TEMPLATE_MISSING,
                                           detail={"reason": "body_snapshot_mismatch"})
            address = match["address"]
            body = claimed["body_snapshot"]

        # 第二步：事务外调用渠道，幂等键在所有重试间固定为消息编号；
        # 崩溃重发时渠道侧据此去重，不会产生两条内容
        result = self.gateway.send(channel=message["channel"], address=address, body=body,
                                   idempotency_key=message["message_id"])

        # 第三步：只有仍持有声明的 worker 才能落盘结果，避免与撤回/接管相互覆盖
        with self.database.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM scheduled_messages WHERE message_id=? AND claim_token=?",
                (message["message_id"], token),
            ).fetchone()
            if current is None:
                # 声明已被撤回/接管清掉。若网关刚好受理成功，内容可能已实际发出：
                # 保留最小回执，并把该事项标记为需要人工核对，而不是悄悄丢弃
                if result.accepted:
                    self._reconcile_late_acceptance(connection, message=message,
                                                    actor_id=actor_id, result=result)
                    return "blocked"
                return "skipped"
            if result.accepted:
                return self._mark_sent(connection, message=dict(current), actor_id=actor_id, result=result)
            if result.rejected:
                return self._block_message(connection, message=dict(current), actor_id=actor_id,
                                           reason_code=policy.REASON_DELIVERY_REJECTED,
                                           detail={"error_code": result.error_code})
            return self._handle_transient(connection, message=dict(current), actor_id=actor_id, result=result)

    def _reconcile_late_acceptance(self, connection, *, message: dict[str, Any],
                                   actor_id: str, result: DeliveryResult) -> None:
        """调和"已受理但声明已失效"的对撞：最小回执 + 人工核对标记。"""

        provider_ref = result.provider_message_ref or "provider-accepted"
        connection.execute(
            "INSERT OR IGNORE INTO delivery_receipts(receipt_id,message_id,channel,provider_message_ref,delivered_at) "
            "VALUES(?,?,?,?,?)",
            (message["message_id"], message["message_id"], message["channel"],
             provider_ref, self._stamp()),
        )
        current = connection.execute(
            "SELECT * FROM scheduled_messages WHERE message_id=?", (message["message_id"],)
        ).fetchone()
        if current is not None and current["state"] in ("queued", "sending", "cancelled"):
            connection.execute(
                "UPDATE scheduled_messages SET state='blocked',reason_code=?,"
                "claim_token=NULL,claimed_at=NULL,updated_at=? WHERE message_id=?",
                (policy.REASON_DELIVERY_RACE, self._stamp(), message["message_id"]),
            )
            self._decision(connection, summary_ref=message["summary_ref"], scope="manual_handoff",
                           channel=message["channel"], reason_code=policy.REASON_DELIVERY_RACE,
                           detail={"provider_message_ref": provider_ref})
            self._audit(connection, actor_id=actor_id, action="followup.message_blocked",
                        resource_id=message["message_id"],
                        detail={"reason_code": policy.REASON_DELIVERY_RACE,
                                "provider_message_ref": provider_ref})

    def _mark_sent(self, connection, *, message: dict[str, Any], actor_id: str,
                   result: DeliveryResult) -> str:
        provider_ref = result.provider_message_ref or "provider-accepted"
        # 唯一约束 + 固定编号保证重复成功回调只产生一张最小回执
        connection.execute(
            "INSERT OR IGNORE INTO delivery_receipts(receipt_id,message_id,channel,provider_message_ref,delivered_at) "
            "VALUES(?,?,?,?,?)",
            (message["message_id"], message["message_id"], message["channel"], provider_ref, self._stamp()),
        )
        connection.execute(
            "UPDATE scheduled_messages SET state='sent',last_error=NULL,"
            "claim_token=NULL,claimed_at=NULL,updated_at=? WHERE message_id=?",
            (self._stamp(), message["message_id"]),
        )
        self._decision(connection, summary_ref=message["summary_ref"], scope="dispatch",
                       channel=message["channel"], reason_code=policy.REASON_DELIVERED, detail={})
        self._audit(connection, actor_id=actor_id, action="followup.message_sent",
                    resource_id=message["message_id"],
                    detail={"channel": message["channel"], "provider_message_ref": provider_ref})
        return "sent"

    def _handle_transient(self, connection, *, message: dict[str, Any], actor_id: str,
                          result: DeliveryResult) -> str:
        now_dt = self._now()
        send_before = datetime.fromisoformat(message["send_before"].replace("Z", "+00:00"))
        delay_index = min(message["attempts"], len(self.retry_delays_seconds)) - 1
        delay = result.retry_after_seconds or self.retry_delays_seconds[max(delay_index, 0)]
        next_attempt = now_dt + timedelta(seconds=delay)
        if next_attempt > send_before:
            return self._block_message(connection, message=message, actor_id=actor_id,
                                       reason_code=policy.REASON_DELIVERY_EXHAUSTED,
                                       detail={"attempts": message["attempts"],
                                               "error_code": result.error_code})
        next_stamp = next_attempt.isoformat().replace("+00:00", "Z")
        connection.execute(
            "UPDATE scheduled_messages SET state='queued',scheduled_for=?,last_error=?,"
            "claim_token=NULL,claimed_at=NULL,updated_at=? WHERE message_id=?",
            (next_stamp, result.error_code, self._stamp(), message["message_id"]),
        )
        self._decision(connection, summary_ref=message["summary_ref"], scope="retry",
                       channel=message["channel"], reason_code=policy.REASON_TRANSIENT_RETRY,
                       detail={"attempts": message["attempts"], "next_at": next_stamp,
                               "error_code": result.error_code})
        self._audit(connection, actor_id=actor_id, action="followup.message_retried",
                    resource_id=message["message_id"],
                    detail={"attempts": message["attempts"], "next_at": next_stamp})
        return "retried"

    def _block_message(self, connection, *, message: dict[str, Any], actor_id: str,
                       reason_code: str, detail: dict[str, Any]) -> str:
        current = connection.execute(
            "SELECT * FROM scheduled_messages WHERE message_id=?", (message["message_id"],)
        ).fetchone()
        if current is None:
            return "cancelled"
        if current["state"] not in ("queued", "sending"):
            return current["state"]
        message = dict(current)
        connection.execute(
            "UPDATE scheduled_messages SET state='blocked',reason_code=?,"
            "claim_token=NULL,claimed_at=NULL,updated_at=? WHERE message_id=?",
            (reason_code, self._stamp(), message["message_id"]),
        )
        self._decision(connection, summary_ref=message["summary_ref"], scope="manual_handoff",
                       channel=message["channel"], reason_code=reason_code, detail=detail)
        self._audit(connection, actor_id=actor_id, action="followup.message_blocked",
                    resource_id=message["message_id"], detail={"reason_code": reason_code})
        return "blocked"

    def expire_overdue(self, *, actor_id: str = "system-dispatcher", limit: int = 100) -> int:
        """把超过发送窗口仍排队的消息转人工，即使一直没有 worker 取到它。"""

        now = self._stamp()
        stale_before = (self._now() - timedelta(seconds=self.claim_timeout_seconds)) \
            .isoformat().replace("+00:00", "Z")
        rows = self.database.connection.execute(
            "SELECT * FROM scheduled_messages WHERE send_before<? AND ("
            "state='queued' OR (state='sending' AND claimed_at<=?)) LIMIT ?",
            (now, stale_before, limit),
        ).fetchall()
        count = 0
        for row in rows:
            with self.database.transaction(immediate=True) as connection:
                count += self._block_message(connection, message=dict(row), actor_id=actor_id,
                                             reason_code=policy.REASON_SEND_WINDOW_EXPIRED,
                                             detail={"at": now}) == "blocked"
        return count

    def confirm_delivery(self, *, message_id: str, provider_message_ref: str) -> DeliveryReceipt:
        """接收供应商成功回执；重复回执幂等，且仍只落最小字段。"""

        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("消息不存在")
            existing = connection.execute(
                "SELECT * FROM delivery_receipts WHERE message_id=?", (message_id,)
            ).fetchone()
            if existing:
                return DeliveryReceipt(existing["receipt_id"], existing["message_id"],
                                       existing["channel"], existing["provider_message_ref"],
                                       existing["delivered_at"])
            connection.execute(
                "INSERT OR IGNORE INTO delivery_receipts(receipt_id,message_id,channel,provider_message_ref,delivered_at) "
                "VALUES(?,?,?,?,?)",
                (message_id, message_id, row["channel"], provider_message_ref, self._stamp()),
            )
            if row["state"] in ("queued", "sending"):
                connection.execute(
                    "UPDATE scheduled_messages SET state='sent',claim_token=NULL,claimed_at=NULL,"
                    "updated_at=? WHERE message_id=?",
                    (self._stamp(), message_id),
                )
            elif row["state"] == "cancelled":
                # 撤回/接管之后才收到供应商受理：内容可能已发出，转人工核对
                connection.execute(
                    "UPDATE scheduled_messages SET state='blocked',reason_code=?,"
                    "claim_token=NULL,claimed_at=NULL,updated_at=? WHERE message_id=?",
                    (policy.REASON_DELIVERY_RACE, self._stamp(), message_id),
                )
                self._decision(connection, summary_ref=row["summary_ref"], scope="manual_handoff",
                               channel=row["channel"], reason_code=policy.REASON_DELIVERY_RACE,
                               detail={"provider_message_ref": provider_message_ref, "via": "receipt"})
            receipt_row = connection.execute(
                "SELECT * FROM delivery_receipts WHERE message_id=?", (message_id,)
            ).fetchone()
            return DeliveryReceipt(receipt_row["receipt_id"], receipt_row["message_id"],
                                   receipt_row["channel"], receipt_row["provider_message_ref"],
                                   receipt_row["delivered_at"])

    # ------------------------------------------------------------------
    # 高风险事项：确认、升级与人工接管
    # ------------------------------------------------------------------

    def acknowledge_handoff(self, *, request_id: str, actor_id: str, handoff_id: str) -> dict[str, Any]:
        """授权人员确认接收，幂等；确认后不再升级。"""

        payload = {"actor_id": actor_id, "handoff_id": handoff_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EXPERT_ROLES)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM followup_handoffs WHERE handoff_id=?", (handoff_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("交接事项不存在")
                site = self._site(connection, row["site_id"])
                if actor["organization_id"] != site["organization_id"] and actor["role"] != "admin":
                    raise PermissionDenied("不能确认其他组织的交接")
                if row["state"] == policy.HANDOFF_ACKNOWLEDGED:
                    return "handoff", handoff_id, {"handoff_id": handoff_id, "already": True}
                if row["state"] == policy.HANDOFF_BREACHED:
                    raise ConflictError("事项已逾期，请通过人工接管接口处理")
                required_role = policy.HANDOFF_ROLE_BY_LEVEL[row["level"]]
                if actor["role"] != required_role and actor["role"] != "admin":
                    raise PermissionDenied(f"第 {row['level']} 层必须由 {required_role} 确认")
                now = self._stamp()
                connection.execute(
                    "UPDATE followup_handoffs SET state='acknowledged',assigned_actor_id=?,"
                    "acknowledged_at=COALESCE(acknowledged_at,?),acknowledged_by=?,updated_at=? WHERE handoff_id=?",
                    (actor_id, now, actor_id, now, handoff_id),
                )
                self._decision(connection, summary_ref=row["summary_ref"], scope="handoff",
                               reason_code=policy.REASON_ACKNOWLEDGED, detail={"by": actor_id, "level": row["level"]})
                self._audit(connection, actor_id=actor_id, action="followup.handoff_acknowledged",
                            resource_id=handoff_id, detail={"level": row["level"]})
                return "handoff", handoff_id, {"handoff_id": handoff_id, "already": False}

            return self._idempotent(connection, request_id=request_id, action="acknowledge_handoff",
                                    payload=payload, create=create)

    def escalate_due(self, *, actor_id: str = "system-scheduler", limit: int = 100) -> dict[str, int]:
        """按原期限升级未确认的高风险事项；最终层级仍未确认则记为逾期。"""

        now = self._stamp()
        now_dt = self._now()
        rows = self.database.connection.execute(
            "SELECT * FROM followup_handoffs WHERE state IN ('open','escalated') AND due_at<=? LIMIT ?",
            (now, limit),
        ).fetchall()
        escalated = breached = 0
        for row in rows:
            with self.database.transaction(immediate=True) as connection:
                current = connection.execute(
                    "SELECT * FROM followup_handoffs WHERE handoff_id=?", (row["handoff_id"],)
                ).fetchone()
                if current is None or current["state"] not in ("open", "escalated"):
                    continue
                if datetime.fromisoformat(current["due_at"].replace("Z", "+00:00")) > now_dt:
                    continue
                if current["state"] == "open":
                    # 原期限一到立即升级，不延长原期限；最终层级给固定的宽限窗口
                    grace_due = (now_dt + timedelta(hours=self.escalation_grace_hours)).isoformat().replace("+00:00", "Z")
                    connection.execute(
                        "UPDATE followup_handoffs SET state='escalated',level=2,assigned_role='admin',"
                        "escalated_at=?,escalation_due_at=?,updated_at=? WHERE handoff_id=?",
                        (now, grace_due, now, current["handoff_id"]),
                    )
                    self._decision(connection, summary_ref=current["summary_ref"], scope="escalation",
                                   reason_code=policy.REASON_ESCALATED_ON_DUE,
                                   detail={"original_due_at": current["due_at"], "new_level": 2,
                                           "escalation_due_at": grace_due})
                    self._audit(connection, actor_id=actor_id, action="followup.handoff_escalated",
                                resource_id=current["handoff_id"],
                                detail={"original_due_at": current["due_at"], "escalation_due_at": grace_due})
                    escalated += 1
                elif current["escalation_due_at"] and current["escalation_due_at"] <= now:
                    connection.execute(
                        "UPDATE followup_handoffs SET state='breached',updated_at=? WHERE handoff_id=?",
                        (now, current["handoff_id"]),
                    )
                    self._decision(connection, summary_ref=current["summary_ref"], scope="escalation",
                                   reason_code=policy.REASON_FINAL_BREACHED,
                                   detail={"escalation_due_at": current["escalation_due_at"]})
                    self._audit(connection, actor_id=actor_id, action="followup.handoff_breached",
                                resource_id=current["handoff_id"],
                                detail={"escalation_due_at": current["escalation_due_at"]})
                    breached += 1
        return {"examined": len(rows), "escalated": escalated, "breached": breached}

    def takeover(self, *, request_id: str, actor_id: str, summary_ref: str) -> dict[str, Any]:
        """工作人员人工接管：取消尚未发送的自动消息，交接记为已接收。"""

        payload = {"actor_id": actor_id, "summary_ref": summary_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EXPERT_ROLES, "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                summary = connection.execute(
                    "SELECT * FROM followup_summaries WHERE summary_ref=?", (summary_ref,)
                ).fetchone()
                if summary is None:
                    raise NotFoundError("随访摘要不存在")
                site = self._site(connection, summary["site_id"])
                self._same_org(actor, site)
                now = self._stamp()
                cancelled: list[str] = []
                pending = connection.execute(
                    "SELECT * FROM scheduled_messages WHERE summary_ref=? AND state IN ('queued','sending')",
                    (summary_ref,),
                ).fetchall()
                for message in pending:
                    connection.execute(
                        "UPDATE scheduled_messages SET state='cancelled',reason_code=?,"
                        "claim_token=NULL,claimed_at=NULL,updated_at=? WHERE message_id=?",
                        (policy.REASON_MANUAL_TAKEOVER, now, message["message_id"]),
                    )
                    self._decision(connection, summary_ref=summary_ref, scope="manual_handoff",
                                   channel=message["channel"], reason_code=policy.REASON_MANUAL_TAKEOVER,
                                   detail={"by": actor_id})
                    self._audit(connection, actor_id=actor_id, action="followup.message_cancelled",
                                resource_id=message["message_id"],
                                detail={"reason_code": policy.REASON_MANUAL_TAKEOVER})
                    cancelled.append(message["message_id"])
                handoff = connection.execute(
                    "SELECT * FROM followup_handoffs WHERE summary_ref=?", (summary_ref,)
                ).fetchone()
                handoff_id = None
                if handoff and handoff["state"] in ("open", "escalated"):
                    connection.execute(
                        "UPDATE followup_handoffs SET state='acknowledged',assigned_actor_id=?,"
                        "acknowledged_at=?,acknowledged_by=?,updated_at=? WHERE handoff_id=?",
                        (actor_id, now, actor_id, now, handoff["handoff_id"]),
                    )
                    self._decision(connection, summary_ref=summary_ref, scope="handoff",
                                   reason_code=policy.REASON_MANUAL_TAKEOVER, detail={"by": actor_id})
                    self._audit(connection, actor_id=actor_id, action="followup.handoff_acknowledged",
                                resource_id=handoff["handoff_id"], detail={"via": "manual_takeover"})
                    handoff_id = handoff["handoff_id"]
                return ("followup_takeover", summary_ref,
                        {"summary_ref": summary_ref, "cancelled_messages": cancelled,
                         "handoff_id": handoff_id})

            return self._idempotent(connection, request_id=request_id, action="followup_takeover",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询与恢复
    # ------------------------------------------------------------------

    def explain(self, *, actor_id: str, summary_ref: str) -> Explanation:
        """解释一条联系为何安排、跳过或转人工。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        summary = connection.execute(
            "SELECT * FROM followup_summaries WHERE summary_ref=?", (summary_ref,)
        ).fetchone()
        if summary is None:
            raise NotFoundError("随访摘要不存在")
        site = self._site(connection, summary["site_id"])
        self._same_org(actor, site)
        decisions: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM followup_decisions WHERE summary_ref=? ORDER BY created_at, decision_id",
            (summary_ref,),
        ):
            decisions.append({"scope": row["scope"], "channel": row["channel"],
                              "reason_code": row["reason_code"],
                              "reason_text": policy.REASON_TEXT.get(row["reason_code"], row["reason_code"]),
                              "detail": json.loads(row["detail_json"]),
                              "created_at": row["created_at"]})
        messages: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM scheduled_messages WHERE summary_ref=? ORDER BY created_at, message_id",
            (summary_ref,),
        ):
            messages.append({"message_id": row["message_id"], "channel": row["channel"],
                             "state": row["state"], "template_version": row["template_version"],
                             "scheduled_for": row["scheduled_for"], "send_before": row["send_before"],
                             "attempts": row["attempts"], "reason_code": row["reason_code"],
                             "reason_text": policy.REASON_TEXT.get(row["reason_code"], row["reason_code"])})
        handoff = None
        handoff_row = connection.execute(
            "SELECT * FROM followup_handoffs WHERE summary_ref=?", (summary_ref,)
        ).fetchone()
        if handoff_row:
            handoff = {"handoff_id": handoff_row["handoff_id"], "state": handoff_row["state"],
                       "level": handoff_row["level"], "assigned_role": handoff_row["assigned_role"],
                       "assigned_actor_id": handoff_row["assigned_actor_id"],
                       "due_at": handoff_row["due_at"],
                       "escalation_due_at": handoff_row["escalation_due_at"],
                       "acknowledged_at": handoff_row["acknowledged_at"],
                       "acknowledged_by": handoff_row["acknowledged_by"]}
        return Explanation(summary_ref=summary_ref, participant_ref=summary["participant_ref"],
                           tier=summary["tier"], category=summary["category"],
                           decisions=decisions, messages=messages, handoff=handoff)

    def get_summary(self, summary_ref: str) -> FollowupSummary:
        row = self.database.connection.execute(
            "SELECT * FROM followup_summaries WHERE summary_ref=?", (summary_ref,)
        ).fetchone()
        if row is None:
            raise NotFoundError("随访摘要不存在")
        return FollowupSummary(row["summary_ref"], row["site_id"], row["participant_ref"],
                               row["recorded_by"], row["recorded_at"], row["tier"], row["category"],
                               row["followup_window_hours"], row["send_window_hours"],
                               row["template_key"], json.loads(row["variables_json"]),
                               row["handoff_due_hours"])

    def get_handoff(self, handoff_id: str) -> Handoff:
        row = self.database.connection.execute(
            "SELECT * FROM followup_handoffs WHERE handoff_id=?", (handoff_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("交接事项不存在")
        return Handoff(row["handoff_id"], row["summary_ref"], row["participant_ref"], row["category"],
                       row["tier"], row["state"], row["level"], row["assigned_role"],
                       row["assigned_actor_id"], row["due_at"], row["escalated_at"],
                       row["acknowledged_at"], row["acknowledged_by"], row["reason_code"],
                       row["created_at"], row["updated_at"])

    def list_pending(self) -> dict[str, Any]:
        """恢复视角：列出所有尚未完成的定时任务，供重启后的 worker 继续处理。"""

        connection = self.database.connection
        messages = connection.execute(
            "SELECT COUNT(*) AS count FROM scheduled_messages WHERE state IN ('queued','sending')"
        ).fetchone()["count"]
        handoffs = connection.execute(
            "SELECT COUNT(*) AS count FROM followup_handoffs WHERE state IN ('open','escalated')"
        ).fetchone()["count"]
        return {"pending_messages": messages, "pending_handoffs": handoffs}

    def get_receipt(self, message_id: str) -> DeliveryReceipt:
        row = self.database.connection.execute(
            "SELECT * FROM delivery_receipts WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("回执不存在")
        return DeliveryReceipt(row["receipt_id"], row["message_id"], row["channel"],
                               row["provider_message_ref"], row["delivered_at"])
