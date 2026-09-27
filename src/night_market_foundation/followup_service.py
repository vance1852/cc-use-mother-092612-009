"""实现义诊后续联系的路由、调度与人工接管规则。

边界原则：
- 系统只按专家给出的固定类别做路由，绝不从摘要内容生成或改写诊疗结论；
- 普通养生/复查提醒走参与者授权的渠道，高风险事项只走授权人员交接；
- 文本在排队时固化模板版本，模板更新不影响已排队内容；
- 真正发送前在同一事务内再次复核授权状态与固化模板版本；
- 所有定时状态持久化，调度器本身无状态，重启后凭表内状态恢复。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from . import followup as policy
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .followup_gateway import MessageGateway, SendResult, StaffNotifier
from .followup_models import (
    ClockResult,
    DeliveryAttempt,
    EncounterSummary,
    ExpertDecision,
    FollowupExplanation,
    FollowupRecord,
    ParticipantProfile,
    StaffTask,
)
from .followup_storage import ensure_followup_schema
from .models import WriteReceipt
from .storage import Database

TEMPLATE_CODES = frozenset(p.template_code for p in policy.CATEGORY_POLICY.values() if p.template_code)
SYSTEM_ACTOR = "scheduler"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 尚未真正发送、撤回时需要立即取消的状态。
PENDING_STATES = ("queued", "sending", "failed_retry", "paused_template")


class FollowupService:
    """协调授权、模板固化、发送窗口、失败重试、升级与人工接管。"""

    def __init__(self, database: Database, clock: Clock | None = None,
                 gateway: MessageGateway | None = None,
                 notifier: StaffNotifier | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.gateway = gateway
        self.notifier = notifier
        ensure_followup_schema(database)

    # ------------------------------------------------------------------ 基础工具

    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone()

    def _now(self) -> str:
        return self._ts(self._now_dt())

    @staticmethod
    def _ts(value: datetime) -> str:
        return value.astimezone().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    @staticmethod
    def _identifier(value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    @staticmethod
    def _text(value: str, field: str, limit: int = 500) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute(
            "SELECT * FROM actors WHERE actor_id=?", (actor_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    @staticmethod
    def _require(actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    @staticmethod
    def _check_site_scope(actor, site) -> None:
        if actor["organization_id"] != site["organization_id"] and actor["role"] != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    def _profile(self, connection, participant_id: str, site_id: str):
        row = connection.execute(
            "SELECT * FROM participant_contacts WHERE participant_id=? AND site_id=?",
            (participant_id, site_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("参与者联系资料不存在")
        return row

    def _event(self, connection, followup_id: str, event: str, detail: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO followup_events(followup_id,event,detail_json,created_at) VALUES(?,?,?,?)",
            (followup_id, event, canonical_json(detail), self._now()),
        )

    def _has_event(self, connection, followup_id: str, event: str,
                   task_id: str | None = None) -> bool:
        if task_id is None:
            return connection.execute(
                "SELECT 1 FROM followup_events WHERE followup_id=? AND event=? LIMIT 1",
                (followup_id, event),
            ).fetchone() is not None
        return connection.execute(
            "SELECT 1 FROM followup_events WHERE followup_id=? AND event=? "
            "AND json_extract(detail_json,'$.task_id')=? LIMIT 1",
            (followup_id, event, task_id),
        ).fetchone() is not None

    @staticmethod
    def _validate_string_set(values: Any, allowed: frozenset[str], field: str) -> set[str]:
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValidationError(f"{field} 必须是字符串数组")
        selected = {item.strip() for item in values}
        invalid = selected - allowed
        if invalid:
            raise ValidationError(f"{field} 含不允许的取值: {sorted(invalid)}")
        return selected

    # ------------------------------------------------------------------ 参与者资料与授权

    def register_participant(self, *, request_id: str, actor_id: str, participant_id: str,
                             site_id: str, channels: list[str], purposes: list[str]) -> WriteReceipt:
        """登记参与者选择的联系渠道范围与授权用途；重复提交为幂等更新。"""

        payload = {"actor_id": actor_id, "participant_id": participant_id, "site_id": site_id,
                   "channels": sorted(channels) if isinstance(channels, list) else channels,
                   "purposes": sorted(purposes) if isinstance(purposes, list) else purposes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            participant_id = self._identifier(participant_id, "participant_id")
            channels = self._validate_string_set(channels, policy.CONTACT_CHANNELS, "channels")
            purposes = self._validate_string_set(purposes, policy.CONSENT_PURPOSES, "purposes")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM participant_contacts WHERE participant_id=? AND site_id=?",
                    (participant_id, site_id),
                ).fetchone()
                if existing:
                    version = existing["version"] + 1
                    action_name = "participant.authorization_changed"
                else:
                    version = 1
                    action_name = "participant.registered"
                connection.execute(
                    "INSERT INTO participant_contacts(participant_id,site_id,channels_json,purposes_json,"
                    "version,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(participant_id,site_id) DO UPDATE SET "
                    "channels_json=excluded.channels_json,purposes_json=excluded.purposes_json,"
                    "version=excluded.version,updated_at=excluded.updated_at",
                    (participant_id, site_id, canonical_json(sorted(channels)),
                     canonical_json(sorted(purposes)), version, self._now(), self._now()),
                )
                append_event(connection, actor_id=actor_id, action=action_name,
                             resource_type="participant_contact", resource_id=participant_id,
                             detail={"site_id": site_id, "channels": sorted(channels),
                                     "purposes": sorted(purposes), "version": version},
                             occurred_at=self._now())
                return "participant_contact", participant_id, {
                    "participant_id": participant_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="followup.register_participant", payload=payload,
                                    create=create)

    def get_participant(self, participant_id: str, site_id: str) -> ParticipantProfile:
        row = self.database.connection.execute(
            "SELECT * FROM participant_contacts WHERE participant_id=? AND site_id=?",
            (participant_id, site_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("参与者联系资料不存在")
        return ParticipantProfile(row["participant_id"], row["site_id"],
                                  frozenset(json.loads(row["channels_json"])),
                                  frozenset(json.loads(row["purposes_json"])), row["version"])

    def revoke_consent(self, *, request_id: str, actor_id: str, participant_id: str,
                       site_id: str) -> WriteReceipt:
        """撤回授权：立即取消该参与者所有尚未发送的提醒（已投递内容不受影响）。"""

        payload = {"actor_id": actor_id, "participant_id": participant_id, "site_id": site_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            profile = self._profile(connection, participant_id, site_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE participant_contacts SET channels_json='[]',purposes_json='[]',"
                    "version=?,updated_at=? WHERE participant_id=? AND site_id=?",
                    (profile["version"] + 1, self._now(), participant_id, site_id),
                )
                pending = connection.execute(
                    f"SELECT * FROM followups WHERE participant_id=? AND site_id=? "
                    f"AND template_code IS NOT NULL AND status IN ({','.join('?' * len(PENDING_STATES))})",
                    (participant_id, site_id, *PENDING_STATES),
                ).fetchall()
                for row in pending:
                    connection.execute(
                        "UPDATE followups SET status='revoked',sending_at=NULL,retry_due_at=NULL,"
                        "completed_at=? WHERE followup_id=?",
                        (self._now(), row["followup_id"]),
                    )
                    self._event(connection, row["followup_id"], "consent.revoked",
                                {"previous_status": row["status"]})
                    append_event(connection, actor_id=actor_id, action="followup.revoked",
                                 resource_type="followup", resource_id=row["followup_id"],
                                 detail={"previous_status": row["status"]},
                                 occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="participant.consent_revoked",
                             resource_type="participant_contact", resource_id=participant_id,
                             detail={"site_id": site_id, "version": profile["version"] + 1,
                                     "cancelled": len(pending)}, occurred_at=self._now())
                return "participant_contact", participant_id, {
                    "participant_id": participant_id, "cancelled": len(pending)}

            return self._idempotent(connection, request_id=request_id,
                                    action="followup.revoke_consent", payload=payload, create=create)

    # ------------------------------------------------------------------ 现场摘要与专家类别

    def record_encounter(self, *, request_id: str, actor_id: str, encounter_id: str,
                         participant_id: str, site_id: str, summary: dict[str, str]) -> WriteReceipt:
        """接收现场服务生成的最少必要摘要；字段白名单外的内容一律拒绝。"""

        payload = {"actor_id": actor_id, "encounter_id": encounter_id,
                   "participant_id": participant_id, "site_id": site_id, "summary": summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            self._profile(connection, participant_id, site_id)
            encounter_id = self._identifier(encounter_id, "encounter_id")
            if not isinstance(summary, dict) or not summary:
                raise ValidationError("summary 必须是非空对象")
            bad_fields = set(summary) - policy.ALLOWED_SUMMARY_FIELDS
            if bad_fields:
                raise ValidationError(f"summary 含不允许的字段: {sorted(bad_fields)}")
            clean: dict[str, str] = {}
            for key, value in summary.items():
                if not isinstance(value, str):
                    raise ValidationError("summary 字段值必须是字符串")
                clean[key] = self._text(value, f"summary.{key}", policy.SUMMARY_FIELD_LIMIT)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO encounters(encounter_id,participant_id,site_id,summary_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (encounter_id, participant_id, site_id, canonical_json(clean),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("义诊编号已经存在") from exc
                # 审计只记录字段名，不记录摘要具体内容。
                append_event(connection, actor_id=actor_id, action="encounter.recorded",
                             resource_type="encounter", resource_id=encounter_id,
                             detail={"site_id": site_id, "participant_id": participant_id,
                                     "fields": sorted(clean)}, occurred_at=self._now())
                return "encounter", encounter_id, {"encounter_id": encounter_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="followup.record_encounter", payload=payload,
                                    create=create)

    def get_encounter(self, encounter_id: str) -> EncounterSummary:
        row = self.database.connection.execute(
            "SELECT * FROM encounters WHERE encounter_id=?", (encounter_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("义诊记录不存在")
        return EncounterSummary(row["encounter_id"], row["participant_id"], row["site_id"],
                                json.loads(row["summary_json"]))

    def record_expert_decision(self, *, request_id: str, actor_id: str, encounter_id: str,
                               category: str) -> WriteReceipt:
        """接收专家给出的后续类别并据此创建路由。医学判断只来自专家，系统不做推断。"""

        payload = {"actor_id": actor_id, "encounter_id": encounter_id, "category": category}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            encounter = connection.execute(
                "SELECT * FROM encounters WHERE encounter_id=?", (encounter_id,)
            ).fetchone()
            if encounter is None:
                raise NotFoundError("义诊记录不存在")
            if category not in policy.FOLLOWUP_CATEGORIES:
                raise ValidationError("后续类别不在允许范围内")
            existing_decision = connection.execute(
                "SELECT * FROM expert_decisions WHERE encounter_id=?", (encounter_id,)
            ).fetchone()
            if existing_decision:
                # 同一专家决定重复提交：类别一致才允许，且只回放既有路由。
                if existing_decision["category"] != category:
                    raise ConflictError("专家已对该义诊给出不同类别，系统不得改判")
                row = connection.execute(
                    "SELECT followup_id,status FROM followups WHERE encounter_id=?", (encounter_id,)
                ).fetchone()

                def replay() -> tuple[str, str, dict[str, Any]]:
                    return "followup", row["followup_id"], {
                        "followup_id": row["followup_id"], "status": row["status"]}

                return self._idempotent(connection, request_id=request_id,
                                        action="followup.record_expert_decision", payload=payload,
                                        create=replay)

            site = self._site(connection, encounter["site_id"])
            profile = self._profile(connection, encounter["participant_id"], encounter["site_id"])
            rule = policy.CATEGORY_POLICY[category]
            now = self._now_dt()
            followup_id = uuid.uuid4().hex
            channels = tuple(sorted(json.loads(profile["channels_json"])))
            base: dict[str, Any] = {
                "followup_id": followup_id, "encounter_id": encounter_id,
                "participant_id": encounter["participant_id"], "site_id": encounter["site_id"],
                "category": category, "channels": canonical_json(channels),
                "created_at": self._now(), "template_code": None, "template_version": None,
                "text_snapshot": None, "not_before": None, "expires_at": None,
                "handoff_due_at": None, "handoff_role": None, "handoff_actor_id": None,
                "completed_at": None, "retry_due_at": None, "sending_at": None,
            }
            status = ""
            purpose_ok = False
            authorized_channels: tuple[str, ...] = ()

            if rule.participant_message:
                assert rule.template_code and rule.purpose and rule.window_ttl is not None
                template = connection.execute(
                    "SELECT * FROM message_templates WHERE code=? AND active=1 "
                    "ORDER BY version DESC LIMIT 1",
                    (rule.template_code,),
                ).fetchone()
                if template is None:
                    raise ValidationError(f"模板 {rule.template_code} 尚未启用，无法排队")
                authorized_purposes = frozenset(json.loads(profile["purposes_json"]))
                authorized_channels = tuple(c for c in channels
                                            if c in frozenset(json.loads(profile["channels_json"])))
                purpose_ok = rule.purpose in authorized_purposes
                base["template_code"] = template["code"]
                base["template_version"] = template["version"]
                if purpose_ok and authorized_channels:
                    # 文本在此固化；之后模板更新不会改动这一行。
                    base["text_snapshot"] = template["text"]
                    base["not_before"] = self._ts(policy.next_window_start(now, site["timezone_name"]))
                    base["expires_at"] = self._ts(now + rule.window_ttl)
                    status = "queued"
                else:
                    status = "skipped"
            else:
                base["handoff_due_at"] = self._ts(now + policy.URGENT_DEADLINE)
                base["handoff_role"] = policy.ESCALATION_ROLE_CHAIN[0]
                status = "in_human_handoff"
            base["status"] = status

            def create() -> tuple[str, str, dict[str, Any]]:
                self._insert_followup(connection, base)
                connection.execute(
                    "INSERT INTO expert_decisions(encounter_id,expert_actor_id,category,decided_at) "
                    "VALUES(?,?,?,?)",
                    (encounter_id, actor_id, category, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="expert.decision_recorded",
                             resource_type="expert_decision", resource_id=encounter_id,
                             detail={"category": category, "followup_id": followup_id},
                             occurred_at=self._now())
                if rule.participant_message:
                    if status == "queued":
                        self._event(connection, followup_id, "routing.scheduled", {
                            "category": category, "channels": list(authorized_channels),
                            "template_code": base["template_code"],
                            "template_version": base["template_version"],
                            "not_before": base["not_before"], "expires_at": base["expires_at"],
                            "reason": "purpose_and_channel_authorized",
                        })
                    else:
                        self._event(connection, followup_id, "routing.skipped", {
                            "category": category,
                            "reason": "purpose_or_channel_not_authorized",
                            "purpose_expected": rule.purpose,
                            "purpose_authorized": purpose_ok,
                            "channels_available": list(authorized_channels),
                        })
                        append_event(connection, actor_id=actor_id, action="followup.skipped",
                                     resource_type="followup", resource_id=followup_id,
                                     detail={"category": category,
                                             "reason": "purpose_or_channel_not_authorized"},
                                     occurred_at=self._now())
                if rule.staff_handoff:
                    task_id = self._open_staff_task(
                        connection, followup_id=followup_id, level=0,
                        role=policy.ESCALATION_ROLE_CHAIN[0], due_at=base["handoff_due_at"])
                    self._event(connection, followup_id, "routing.human_handoff", {
                        "category": category, "task_id": task_id,
                        "role": policy.ESCALATION_ROLE_CHAIN[0],
                        "handoff_due_at": base["handoff_due_at"],
                        "reason": "expert_marked_urgent_followup",
                    })
                    append_event(connection, actor_id=actor_id, action="followup.handoff_opened",
                                 resource_type="followup", resource_id=followup_id,
                                 detail={"category": category,
                                         "role": policy.ESCALATION_ROLE_CHAIN[0],
                                         "handoff_due_at": base["handoff_due_at"]},
                                 occurred_at=self._now())
                return "followup", followup_id, {"followup_id": followup_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="followup.record_expert_decision", payload=payload,
                                    create=create)

    @staticmethod
    def _insert_followup(connection, v: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO followups(followup_id,encounter_id,participant_id,site_id,category,status,"
            "channels_json,created_at,template_code,template_version,text_snapshot,not_before,"
            "expires_at,handoff_due_at,handoff_role,handoff_actor_id,completed_at,retry_due_at,"
            "sending_at) VALUES(:followup_id,:encounter_id,:participant_id,:site_id,:category,"
            ":status,:channels,:created_at,:template_code,:template_version,:text_snapshot,"
            ":not_before,:expires_at,:handoff_due_at,:handoff_role,:handoff_actor_id,"
            ":completed_at,:retry_due_at,:sending_at)",
            v,
        )

    def get_decision(self, encounter_id: str) -> ExpertDecision:
        row = self.database.connection.execute(
            "SELECT * FROM expert_decisions WHERE encounter_id=?", (encounter_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("专家决定不存在")
        return ExpertDecision(row["encounter_id"], row["expert_actor_id"], row["category"])

    # ------------------------------------------------------------------ 模板版本

    def register_template(self, *, request_id: str, actor_id: str, code: str,
                          version: int, text: str) -> WriteReceipt:
        """登记模板新版本；新版本成为当前版本，旧版本保留且不可变。"""

        payload = {"actor_id": actor_id, "code": code, "version": version, "text": text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            code = self._identifier(code, "code")
            if code not in TEMPLATE_CODES:
                raise ValidationError("模板代码不在允许范围内")
            if not isinstance(version, int) or version < 1:
                raise ValidationError("version 必须是不小于 1 的整数")
            text = self._text(text, "text", 1000)
            existing = connection.execute(
                "SELECT * FROM message_templates WHERE code=? AND version=?", (code, version)
            ).fetchone()
            if existing and existing["text"] != text:
                raise ConflictError("同一模板版本的文本不可修改")

            def create() -> tuple[str, str, dict[str, Any]]:
                if not existing:
                    connection.execute(
                        "INSERT INTO message_templates(code,version,text,active,created_at) "
                        "VALUES(?,?,?,1,?)",
                        (code, version, text, self._now()),
                    )
                    connection.execute(
                        "UPDATE message_templates SET active=0 WHERE code=? AND version<>?",
                        (code, version),
                    )
                append_event(connection, actor_id=actor_id, action="template.registered",
                             resource_type="message_template", resource_id=f"{code}:{version}",
                             detail={"code": code, "version": version}, occurred_at=self._now())
                return "message_template", f"{code}:{version}", {"code": code, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="followup.register_template", payload=payload,
                                    create=create)

    # ------------------------------------------------------------------ 人工接管

    def _open_staff_task(self, connection, *, followup_id: str, level: int,
                         role: str, due_at: str) -> str:
        task_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO staff_tasks(task_id,followup_id,role,level,status,claimed_by,due_at,"
            "created_at,claimed_at,confirmed_at,disposition_code) "
            "VALUES(?,?,?,?, 'open',NULL,?,?,NULL,NULL,NULL)",
            (task_id, followup_id, role, level, due_at, self._now()),
        )
        return task_id

    def claim_task(self, *, actor_id: str, task_id: str) -> StaffTask:
        """授权人员认领交接任务；同一人重复认领幂等，他人已认领则冲突。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT * FROM staff_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("交接任务不存在")
            followup = connection.execute(
                "SELECT * FROM followups WHERE followup_id=?", (row["followup_id"],)
            ).fetchone()
            site = self._site(connection, followup["site_id"])
            self._check_site_scope(actor, site)
            if actor["role"] != row["role"]:
                raise PermissionDenied("该任务只交给指定角色")
            if row["status"] == "claimed":
                if row["claimed_by"] == actor_id:
                    return self._task_object(row)
                raise ConflictError("任务已被其他授权人员认领")
            if row["status"] != "open":
                raise ConflictError("任务已不在可认领状态")
            connection.execute(
                "UPDATE staff_tasks SET status='claimed',claimed_by=?,claimed_at=? WHERE task_id=?",
                (actor_id, self._now(), task_id),
            )
            self._event(connection, row["followup_id"], "staff.claimed",
                        {"task_id": task_id, "role": row["role"], "actor_id": actor_id})
            append_event(connection, actor_id=actor_id, action="staff.task_claimed",
                         resource_type="staff_task", resource_id=task_id,
                         detail={"followup_id": row["followup_id"], "role": row["role"]},
                         occurred_at=self._now())
            row = connection.execute(
                "SELECT * FROM staff_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._task_object(row)

    def confirm_received(self, *, request_id: str, actor_id: str, task_id: str,
                         disposition_code: str) -> WriteReceipt:
        """授权人员确认已接手高风险事项；重复确认幂等。"""

        payload = {"actor_id": actor_id, "task_id": task_id, "disposition_code": disposition_code}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT * FROM staff_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("交接任务不存在")
            followup = connection.execute(
                "SELECT * FROM followups WHERE followup_id=?", (row["followup_id"],)
            ).fetchone()
            site = self._site(connection, followup["site_id"])
            self._check_site_scope(actor, site)
            if actor["role"] != row["role"]:
                raise PermissionDenied("只有当前任务的授权角色可以确认")
            if disposition_code not in policy.DISPOSITION_CODES:
                raise ValidationError("disposition_code 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "confirmed":
                    return "staff_task", task_id, {"task_id": task_id, "replayed": True}
                if row["status"] == "escalated":
                    raise ConflictError("任务已升级，不能在原任务上确认")
                if row["status"] not in ("open", "claimed"):
                    raise ConflictError("任务已不在可确认状态")
                claimed_by = row["claimed_by"] or actor_id
                connection.execute(
                    "UPDATE staff_tasks SET status='confirmed',claimed_by=?,"
                    "claimed_at=COALESCE(claimed_at,?),confirmed_at=?,disposition_code=? "
                    "WHERE task_id=?",
                    (claimed_by, self._now(), self._now(), disposition_code, task_id),
                )
                connection.execute(
                    "UPDATE followups SET status='completed',handoff_actor_id=?,completed_at=? "
                    "WHERE followup_id=?",
                    (claimed_by, self._now(), row["followup_id"]),
                )
                self._event(connection, row["followup_id"], "staff.confirmed",
                            {"task_id": task_id, "level": row["level"], "role": row["role"],
                             "actor_id": claimed_by, "disposition_code": disposition_code})
                append_event(connection, actor_id=actor_id, action="staff.task_confirmed",
                             resource_type="staff_task", resource_id=task_id,
                             detail={"followup_id": row["followup_id"], "level": row["level"],
                                     "disposition_code": disposition_code}, occurred_at=self._now())
                return "staff_task", task_id, {"task_id": task_id, "replayed": False}

            return self._idempotent(connection, request_id=request_id,
                                    action="followup.confirm_received", payload=payload,
                                    create=create)

    # ------------------------------------------------------------------ 时钟驱动的调度

    def run_due(self) -> ClockResult:
        """按注入时钟处理到期内容：窗口发送、失败重试、到期升级、过期关闭。

        调度器无状态，任何时候调用都只依据持久化行推进状态，因此进程重启后
        重新调用本方法即可恢复全部尚未完成的定时任务。每条事项使用独立事务，
        个别处理失败不影响其他事项；外部发送使用确定性幂等键，重放不产生重复消息。
        """

        with self.database.transaction(immediate=True) as connection:
            self._recover_stale_claims(connection)
        # 先关过期窗口，再发送，避免到期与过期在同一时刻发生时误发。
        expired = self._expire_windows()
        due_sent, retried = self._send_due_messages()
        escalated = self._escalate_due_tasks()
        self._recover_staff_notifications()
        return ClockResult(due_sent=due_sent, retried=retried,
                           escalated=escalated, expired=expired)

    def recover(self) -> ClockResult:
        """从持久化状态恢复并立即推进一次所有到期任务（与 run_due 等价）。"""

        return self.run_due()

    def _recover_stale_claims(self, connection) -> None:
        cutoff = self._ts(self._now_dt() - policy.CLAIM_TIMEOUT)
        rows = connection.execute(
            "SELECT followup_id,sending_at FROM followups WHERE status='sending' "
            "AND sending_at IS NOT NULL AND sending_at<=?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            attempts = connection.execute(
                "SELECT COUNT(*) AS c FROM delivery_attempts WHERE followup_id=?",
                (row["followup_id"],),
            ).fetchone()["c"]
            if attempts == 0:
                connection.execute(
                    "UPDATE followups SET status='queued',sending_at=NULL WHERE followup_id=?",
                    (row["followup_id"],),
                )
                self._event(connection, row["followup_id"], "send.claim_recovered",
                            {"to_status": "queued"})
            else:
                backoff = policy.BACKOFF_MINUTES[min(attempts - 1, len(policy.BACKOFF_MINUTES) - 1)]
                retry_at = self._parse(row["sending_at"]) + timedelta(minutes=backoff)
                connection.execute(
                    "UPDATE followups SET status='failed_retry',sending_at=NULL,retry_due_at=? "
                    "WHERE followup_id=?",
                    (self._ts(retry_at), row["followup_id"]),
                )
                self._event(connection, row["followup_id"], "send.claim_recovered",
                            {"to_status": "failed_retry", "retry_due_at": self._ts(retry_at)})

    def _send_due_messages(self) -> tuple[int, int]:
        now_text = self._now()
        rows = self.database.connection.execute(
            "SELECT followup_id,status FROM followups WHERE template_code IS NOT NULL AND "
            "((status='queued' AND not_before IS NOT NULL AND not_before<=?) "
            "OR (status='failed_retry' AND retry_due_at IS NOT NULL AND retry_due_at<=?)) "
            "ORDER BY not_before,followup_id",
            (now_text, now_text),
        ).fetchall()
        sent = 0
        retried = 0
        for item in rows:
            outcome = self._process_one_delivery(item["followup_id"], item["status"])
            if outcome == "delivered":
                sent += 1
            elif outcome == "retry":
                retried += 1
        return sent, retried

    def _process_one_delivery(self, followup_id: str, expected_status: str) -> str:
        """在独立事务内处理一条到期提醒。"""

        with self.database.transaction(immediate=True) as connection:
            claimed = connection.execute(
                "UPDATE followups SET status='sending',sending_at=? "
                "WHERE followup_id=? AND status=?",
                (self._now(), followup_id, expected_status),
            )
            if claimed.rowcount != 1:
                return "skipped"  # 已被其他调度进程认领或状态已变化
            row = connection.execute(
                "SELECT * FROM followups WHERE followup_id=?", (followup_id,)
            ).fetchone()
            now = self._now_dt()
            now_text = self._now()
            if row["expires_at"] and now_text > row["expires_at"]:
                self._finish_without_delivery(connection, row, "expired", "window.expired")
                return "other"
            site = self._site(connection, row["site_id"])
            if not policy.within_daytime_window(now, site["timezone_name"]):
                next_open = policy.next_window_start(now, site["timezone_name"])
                connection.execute(
                    "UPDATE followups SET status='queued',sending_at=NULL,not_before=? "
                    "WHERE followup_id=?",
                    (self._ts(next_open), followup_id),
                )
                self._event(connection, followup_id, "send.deferred_night",
                            {"next_not_before": self._ts(next_open)})
                return "other"
            # 发送前最后复核：授权仍有效，固化模板版本存在且文本与排队时一致。
            profile = connection.execute(
                "SELECT * FROM participant_contacts WHERE participant_id=? AND site_id=?",
                (row["participant_id"], row["site_id"]),
            ).fetchone()
            template = connection.execute(
                "SELECT * FROM message_templates WHERE code=? AND version=?",
                (row["template_code"], row["template_version"]),
            ).fetchone()
            if profile is None:
                self._pause(connection, row, now_text, "profile_missing")
                return "other"
            if template is None or template["text"] != row["text_snapshot"]:
                self._pause(connection, row, now_text,
                            "template_version_unavailable_or_mutated")
                return "other"
            purposes = frozenset(json.loads(profile["purposes_json"]))
            live_channels = [c for c in json.loads(row["channels_json"])
                             if c in frozenset(json.loads(profile["channels_json"]))]
            rule = policy.CATEGORY_POLICY[row["category"]]
            if rule.purpose not in purposes or not live_channels:
                connection.execute(
                    "UPDATE followups SET status='revoked',sending_at=NULL,completed_at=? "
                    "WHERE followup_id=?",
                    (now_text, followup_id),
                )
                self._event(connection, followup_id, "send.authorization_revoked", {
                    "reason": "purpose_or_channel_no_longer_authorized"})
                append_event(connection, actor_id=SYSTEM_ACTOR, action="followup.revoked",
                             resource_type="followup", resource_id=followup_id,
                             detail={"reason": "pre_send_authorization_check"},
                             occurred_at=now_text)
                return "other"
            channel = live_channels[0]
            attempt_number = connection.execute(
                "SELECT COUNT(*) AS c FROM delivery_attempts WHERE followup_id=?",
                (followup_id,),
            ).fetchone()["c"] + 1
            if attempt_number > policy.MAX_ATTEMPTS:
                self._finish_without_delivery(connection, row, "failed",
                                              "send.attempts_exhausted")
                return "other"
            # 幂等键由事项与尝试序号确定性生成：崩溃重放时键不变，渠道侧去重。
            attempt_key = f"{followup_id}:{attempt_number}"
            result = self._gateway_send(
                channel=channel, participant_id=row["participant_id"],
                text=row["text_snapshot"], attempt_key=attempt_key)
            attempt_id = uuid.uuid4().hex
            if result.ok:
                connection.execute(
                    "INSERT INTO delivery_attempts(attempt_id,followup_id,channel,attempt_number,"
                    "status,provider_message_id,detail_code,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'delivered',?,?,?,?)",
                    (attempt_id, followup_id, channel, attempt_number,
                     result.provider_message_id, result.detail_code, now_text, now_text),
                )
                connection.execute(
                    "UPDATE followups SET status='delivered',sending_at=NULL,completed_at=? "
                    "WHERE followup_id=?",
                    (now_text, followup_id),
                )
                # 事件只留渠道、尝试序号与渠道侧编号，不含文本与任何健康字段。
                self._event(connection, followup_id, "send.delivered", {
                    "channel": channel, "attempt_number": attempt_number,
                    "provider_message_id": result.provider_message_id})
                append_event(connection, actor_id=SYSTEM_ACTOR, action="followup.delivered",
                             resource_type="followup", resource_id=followup_id,
                             detail={"channel": channel, "attempt_number": attempt_number},
                             occurred_at=now_text)
                return "delivered"
            connection.execute(
                "INSERT INTO delivery_attempts(attempt_id,followup_id,channel,attempt_number,"
                "status,provider_message_id,detail_code,created_at,updated_at) "
                "VALUES(?,?,?,?, 'failed',NULL,?,?,?)",
                (attempt_id, followup_id, channel, attempt_number, result.detail_code,
                 now_text, now_text),
            )
            if result.retryable and attempt_number < policy.MAX_ATTEMPTS:
                retry_at = now + timedelta(minutes=policy.BACKOFF_MINUTES[attempt_number - 1])
                connection.execute(
                    "UPDATE followups SET status='failed_retry',sending_at=NULL,retry_due_at=? "
                    "WHERE followup_id=?",
                    (self._ts(retry_at), followup_id),
                )
                self._event(connection, followup_id, "send.retry_scheduled", {
                    "channel": channel, "attempt_number": attempt_number,
                    "detail_code": result.detail_code, "retry_due_at": self._ts(retry_at)})
                return "retry"
            self._finish_without_delivery(
                connection, row, "failed",
                "send.attempts_exhausted" if result.retryable else "send.permanently_failed",
                extra={"detail_code": result.detail_code})
            return "other"

    def _pause(self, connection, row, now_text: str, reason: str) -> None:
        connection.execute(
            "UPDATE followups SET status='paused_template',sending_at=NULL WHERE followup_id=?",
            (row["followup_id"],),
        )
        self._event(connection, row["followup_id"], "send.paused", {"reason": reason})
        append_event(connection, actor_id=SYSTEM_ACTOR, action="followup.paused",
                     resource_type="followup", resource_id=row["followup_id"],
                     detail={"reason": reason}, occurred_at=now_text)

    def _gateway_send(self, *, channel: str, participant_id: str, text: str,
                      attempt_key: str) -> SendResult:
        if self.gateway is None:
            return SendResult.retry_later("gateway_not_configured")
        try:
            return self.gateway.send(channel=channel, target_token=participant_id,
                                     text=text, idempotency_key=attempt_key)
        except Exception as exc:  # 网关异常按可重试失败落库，调度不中断
            return SendResult.retry_later(f"gateway_error:{type(exc).__name__}")

    def _finish_without_delivery(self, connection, row, status: str, event: str,
                                 extra: dict[str, Any] | None = None) -> None:
        now_text = self._now()
        connection.execute(
            "UPDATE followups SET status=?,sending_at=NULL,retry_due_at=NULL,completed_at=? "
            "WHERE followup_id=?",
            (status, now_text, row["followup_id"]),
        )
        detail = dict(extra or {})
        detail["status"] = status
        self._event(connection, row["followup_id"], event, detail)
        append_event(connection, actor_id=SYSTEM_ACTOR, action=f"followup.{status}",
                     resource_type="followup", resource_id=row["followup_id"],
                     detail={"reason": event}, occurred_at=now_text)

    def _expire_windows(self) -> int:
        now_text = self._now()
        rows = self.database.connection.execute(
            "SELECT followup_id FROM followups WHERE template_code IS NOT NULL "
            "AND status IN ('queued','failed_retry') AND expires_at IS NOT NULL AND expires_at<?",
            (now_text,),
        ).fetchall()
        count = 0
        for item in rows:
            with self.database.transaction(immediate=True) as connection:
                changed = connection.execute(
                    "UPDATE followups SET status='expired' WHERE followup_id=? "
                    "AND status IN ('queued','failed_retry')",
                    (item["followup_id"],),
                )
                if changed.rowcount != 1:
                    continue
                row = connection.execute(
                    "SELECT expires_at FROM followups WHERE followup_id=?", (item["followup_id"],)
                ).fetchone()
                self._event(connection, item["followup_id"], "window.expired",
                            {"expires_at": row["expires_at"]})
                append_event(connection, actor_id=SYSTEM_ACTOR, action="followup.expired",
                             resource_type="followup", resource_id=item["followup_id"],
                             detail={"expires_at": row["expires_at"]}, occurred_at=now_text)
                count += 1
        return count

    def _escalate_due_tasks(self) -> int:
        now_text = self._now()
        rows = self.database.connection.execute(
            "SELECT task_id FROM staff_tasks WHERE status IN ('open','claimed') AND due_at<=? "
            "ORDER BY followup_id,level",
            (now_text,),
        ).fetchall()
        escalated = 0
        for item in rows:
            with self.database.transaction(immediate=True) as connection:
                task = connection.execute(
                    "SELECT * FROM staff_tasks WHERE task_id=? AND status IN ('open','claimed')",
                    (item["task_id"],),
                ).fetchone()
                if task is None:
                    continue  # 已被其他进程处理
                followup = connection.execute(
                    "SELECT * FROM followups WHERE followup_id=?", (task["followup_id"],)
                ).fetchone()
                next_level = task["level"] + 1
                if next_level < len(policy.ESCALATION_ROLE_CHAIN):
                    # 未在原期限内确认：原任务升级，并建立下一授权角色的任务。
                    connection.execute(
                        "UPDATE staff_tasks SET status='escalated' WHERE task_id=?",
                        (task["task_id"],),
                    )
                    self._event(connection, task["followup_id"], "staff.escalated", {
                        "task_id": task["task_id"], "from_role": task["role"],
                        "from_level": task["level"], "deadline_was": task["due_at"]})
                    next_role = policy.ESCALATION_ROLE_CHAIN[next_level]
                    # 按原期限升级：新任务沿用原始截止时间；已逾期则下一次扫描立即处理。
                    new_task_id = self._open_staff_task(
                        connection, followup_id=task["followup_id"], level=next_level,
                        role=next_role, due_at=followup["handoff_due_at"])
                    self._event(connection, task["followup_id"], "staff.task_opened", {
                        "task_id": new_task_id, "role": next_role, "level": next_level,
                        "due_at": followup["handoff_due_at"]})
                    self._safe_notify(connection, role=next_role, task_id=new_task_id,
                                      due_at=followup["handoff_due_at"],
                                      site_id=followup["site_id"],
                                      followup_id=task["followup_id"])
                    append_event(connection, actor_id=SYSTEM_ACTOR, action="staff.task_escalated",
                                 resource_type="staff_task", resource_id=new_task_id,
                                 detail={"followup_id": task["followup_id"],
                                         "from_level": task["level"], "to_role": next_role,
                                         "original_due_at": followup["handoff_due_at"]},
                                 occurred_at=now_text)
                    escalated += 1
                else:
                    # 授权链末端仍未确认：终态告警只记录一次，重复扫描天然幂等。
                    if self._has_event(connection, task["followup_id"],
                                       "staff.escalation_exhausted", task["task_id"]):
                        continue
                    self._event(connection, task["followup_id"], "staff.escalation_exhausted", {
                        "task_id": task["task_id"], "role": task["role"],
                        "original_due_at": followup["handoff_due_at"]})
                    self._safe_notify(connection, role=task["role"], task_id=task["task_id"],
                                      due_at=task["due_at"], site_id=followup["site_id"],
                                      followup_id=task["followup_id"])
                    append_event(connection, actor_id=SYSTEM_ACTOR,
                                 action="staff.escalation_exhausted",
                                 resource_type="staff_task", resource_id=task["task_id"],
                                 detail={"followup_id": task["followup_id"],
                                         "role": task["role"]},
                                 occurred_at=now_text)
        return escalated

    def _safe_notify(self, connection, *, role: str, task_id: str, due_at: str,
                     site_id: str, followup_id: str) -> None:
        if self.notifier is None:
            self._event(connection, followup_id, "staff.notified",
                        {"task_id": task_id, "role": role, "deferred": True})
            return
        try:
            self.notifier.notify(actor_id=None, task_id=task_id, role=role,
                                 due_at=due_at, site_id=site_id)
        except Exception as exc:  # 通知失败不回滚调度，恢复扫描依据事件记录补发
            self._event(connection, followup_id, "staff.notify_failed",
                        {"task_id": task_id, "role": role, "error": type(exc).__name__})
            return
        self._event(connection, followup_id, "staff.notified",
                    {"task_id": task_id, "role": role})

    def _recover_staff_notifications(self) -> None:
        """恢复未发出的在岗任务通知（通知按 task_id 幂等，重复扫描不重复通知）。"""

        tasks = self.database.connection.execute(
            "SELECT task_id FROM staff_tasks WHERE status IN ('open','claimed')"
        ).fetchall()
        for task in tasks:
            with self.database.transaction(immediate=True) as connection:
                locked = connection.execute(
                    "SELECT * FROM staff_tasks WHERE task_id=? AND status IN ('open','claimed')",
                    (task["task_id"],),
                ).fetchone()
                if locked is None:
                    continue
                if self._has_event(connection, locked["followup_id"], "staff.notified",
                                   locked["task_id"]):
                    continue
                followup = connection.execute(
                    "SELECT site_id FROM followups WHERE followup_id=?",
                    (locked["followup_id"],),
                ).fetchone()
                self._safe_notify(connection, role=locked["role"], task_id=locked["task_id"],
                                  due_at=locked["due_at"], site_id=followup["site_id"],
                                  followup_id=locked["followup_id"])

    # ------------------------------------------------------------------ 后台查询

    @staticmethod
    def _task_object(row) -> StaffTask:
        return StaffTask(row["task_id"], row["followup_id"], row["role"], row["level"],
                         row["status"], row["claimed_by"], row["due_at"], row["created_at"],
                         row["claimed_at"], row["confirmed_at"], row["disposition_code"])

    @staticmethod
    def _followup_object(row) -> FollowupRecord:
        return FollowupRecord(
            followup_id=row["followup_id"], encounter_id=row["encounter_id"],
            participant_id=row["participant_id"], site_id=row["site_id"], category=row["category"],
            status=row["status"], channels=tuple(json.loads(row["channels_json"])),
            created_at=row["created_at"], template_code=row["template_code"],
            template_version=row["template_version"], text_snapshot=row["text_snapshot"],
            not_before=row["not_before"], expires_at=row["expires_at"],
            handoff_due_at=row["handoff_due_at"], handoff_role=row["handoff_role"],
            handoff_actor_id=row["handoff_actor_id"], completed_at=row["completed_at"])

    def get_followup(self, followup_id: str) -> FollowupRecord:
        row = self.database.connection.execute(
            "SELECT * FROM followups WHERE followup_id=?", (followup_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("后续事项不存在")
        return self._followup_object(row)

    def explain(self, *, actor_id: str, followup_id: str) -> FollowupExplanation:
        """解释一条联系为何安排、跳过或转人工：状态、事件时间线、尝试与交接记录。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer", "auditor", "operator")
            row = connection.execute(
                "SELECT * FROM followups WHERE followup_id=?", (followup_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("后续事项不存在")
            site = self._site(connection, row["site_id"])
            self._check_site_scope(actor, site)
            timeline = tuple(
                {"sequence": event["id"], "event": event["event"],
                 "detail": json.loads(event["detail_json"]), "created_at": event["created_at"]}
                for event in connection.execute(
                    "SELECT * FROM followup_events WHERE followup_id=? ORDER BY id",
                    (followup_id,))
            )
            attempts = tuple(
                DeliveryAttempt(a["attempt_id"], a["followup_id"], a["channel"],
                                a["attempt_number"], a["status"], a["detail_code"],
                                a["created_at"], a["updated_at"])
                for a in connection.execute(
                    "SELECT * FROM delivery_attempts WHERE followup_id=? ORDER BY attempt_number",
                    (followup_id,))
            )
            tasks = tuple(
                self._task_object(t)
                for t in connection.execute(
                    "SELECT * FROM staff_tasks WHERE followup_id=? ORDER BY level",
                    (followup_id,))
            )
            return FollowupExplanation(self._followup_object(row), timeline, attempts, tasks)
