"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 诊后联系分流：参与者选择的联系渠道范围（最小化保存，不存健康信息）
CREATE TABLE IF NOT EXISTS participant_consents (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    participant_ref TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (site_id, participant_ref)
);
-- 专家后续类别词汇表：类别必须事先登记，系统只按登记结果路由
CREATE TABLE IF NOT EXISTS followup_categories (
    category TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    handoff_due_hours INTEGER,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(tier IN ('routine', 'urgent')),
    CHECK((tier = 'urgent' AND handoff_due_hours IS NOT NULL AND handoff_due_hours > 0)
          OR tier = 'routine')
);
-- 不可变模板版本：新版本发布不改变已经排队消息引用的版本；
-- active 表示当前用于新排队的版本，recalled_at 表示管理员主动召回（旧版本仍可用于已排队消息）
CREATE TABLE IF NOT EXISTS message_templates (
    template_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    body TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    published_at TEXT NOT NULL,
    retired_at TEXT,
    recalled_at TEXT,
    PRIMARY KEY (template_key, channel, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS message_templates_one_active
    ON message_templates(template_key, channel) WHERE active = 1;
-- 现场最少必要摘要：不保存诊断文本，只保存专家类别与路由所需参数
CREATE TABLE IF NOT EXISTS followup_summaries (
    summary_ref TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    participant_ref TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES actors(actor_id),
    recorded_at TEXT NOT NULL,
    tier TEXT NOT NULL,
    category TEXT NOT NULL,
    followup_window_hours INTEGER NOT NULL,
    send_window_hours INTEGER NOT NULL,
    handoff_due_hours INTEGER,
    template_key TEXT NOT NULL,
    variables_json TEXT NOT NULL
);
-- 常规养生提醒名单（与授权人员交接名单分开保存）
CREATE TABLE IF NOT EXISTS scheduled_messages (
    message_id TEXT PRIMARY KEY,
    summary_ref TEXT NOT NULL REFERENCES followup_summaries(summary_ref),
    site_id TEXT NOT NULL,
    participant_ref TEXT NOT NULL,
    channel TEXT NOT NULL,
    template_key TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    consent_version INTEGER NOT NULL,
    template_body_hash TEXT NOT NULL,
    body_snapshot TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued', 'sending', 'sent', 'cancelled', 'blocked')),
    scheduled_for TEXT NOT NULL,
    send_before TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    claimed_at TEXT,
    last_error TEXT,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(summary_ref, channel)
);
CREATE INDEX IF NOT EXISTS scheduled_messages_due
    ON scheduled_messages(state, scheduled_for);
-- 授权人员跟进名单：高风险事项、期限、升级层级与确认轨迹
CREATE TABLE IF NOT EXISTS followup_handoffs (
    handoff_id TEXT PRIMARY KEY,
    summary_ref TEXT NOT NULL UNIQUE REFERENCES followup_summaries(summary_ref),
    site_id TEXT NOT NULL,
    participant_ref TEXT NOT NULL,
    category TEXT NOT NULL,
    tier TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'acknowledged', 'escalated', 'breached')),
    level INTEGER NOT NULL DEFAULT 1 CHECK(level IN (1, 2)),
    assigned_role TEXT NOT NULL,
    assigned_actor_id TEXT REFERENCES actors(actor_id),
    due_at TEXT NOT NULL,
    escalation_due_at TEXT,
    escalated_at TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT REFERENCES actors(actor_id),
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS followup_handoffs_due
    ON followup_handoffs(state, due_at);
-- 成功投递的最小回执：不含参与者编号、类别与正文，无法反推健康细节
CREATE TABLE IF NOT EXISTS delivery_receipts (
    receipt_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE REFERENCES scheduled_messages(message_id),
    channel TEXT NOT NULL,
    provider_message_ref TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);
-- 每条联系的安排/跳过/转人工决策轨迹，支撑后台可解释查询
CREATE TABLE IF NOT EXISTS followup_decisions (
    decision_id TEXT PRIMARY KEY,
    summary_ref TEXT NOT NULL REFERENCES followup_summaries(summary_ref),
    scope TEXT NOT NULL,
    channel TEXT,
    reason_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS followup_decisions_summary
    ON followup_decisions(summary_ref, created_at);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
