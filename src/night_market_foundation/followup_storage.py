"""义诊后续联系路由的持久化结构。

所有定时状态都落库，进程重启后只靠表内状态即可恢复尚未完成的任务，
不依赖内存队列。建表语句全部幂等，可在既有基础库上重复执行。
"""

from __future__ import annotations

FOLLOWUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS participant_contacts (
    participant_id TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    channels_json TEXT NOT NULL,
    purposes_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (participant_id, site_id)
);
CREATE TABLE IF NOT EXISTS encounters (
    encounter_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    summary_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expert_decisions (
    encounter_id TEXT PRIMARY KEY REFERENCES encounters(encounter_id),
    expert_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    category TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_templates (
    code TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    text TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (code, version)
);
CREATE TABLE IF NOT EXISTS followups (
    followup_id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL UNIQUE REFERENCES encounters(encounter_id),
    participant_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    template_code TEXT,
    template_version INTEGER,
    text_snapshot TEXT,
    not_before TEXT,
    expires_at TEXT,
    handoff_due_at TEXT,
    handoff_role TEXT,
    handoff_actor_id TEXT,
    completed_at TEXT,
    retry_due_at TEXT,
    sending_at TEXT
);
CREATE TABLE IF NOT EXISTS followup_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    followup_id TEXT NOT NULL REFERENCES followups(followup_id),
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    followup_id TEXT NOT NULL REFERENCES followups(followup_id),
    channel TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    provider_message_id TEXT,
    detail_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(followup_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS staff_tasks (
    task_id TEXT PRIMARY KEY,
    followup_id TEXT NOT NULL REFERENCES followups(followup_id),
    role TEXT NOT NULL,
    level INTEGER NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    confirmed_at TEXT,
    disposition_code TEXT,
    UNIQUE(followup_id, level)
);
"""


def ensure_followup_schema(database) -> None:
    """在既有数据库上幂等地创建后续联系相关表。"""

    database.connection.executescript(FOLLOWUP_SCHEMA)
