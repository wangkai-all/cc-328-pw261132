"""SQLite 连接管理与建表脚本。

设计约定：
- 原始事件表 events 是追加写的业务事实表；许可撤回时对其做硬删除，
  删除前行内容转入 disposal_archive（演示用可恢复），并在 disposal_log 留痕。
- samples 每个 (sample_id, version) 不可变；标签回补产生新版本，旧版本保留。
- 所有任务/评估都固定 input_hash 与 consent_version，保证可复现与可审计。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 用户与个性化许可
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active', 'withdrawn')),
    created_ts INTEGER NOT NULL,
    profile_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS consent_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    granted INTEGER NOT NULL,           -- 1=授予 0=撤回
    reason TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- 原始反馈事件（展示/点击/购买/取消/退货）
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    impression_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('impression', 'click', 'purchase', 'cancel', 'refund')
    ),
    event_ts INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    position INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_imp ON events (impression_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_events_user ON events (user_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_events_item ON events (item_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (event_ts);

-- 样本（每次标签/特征变化产生一个新版本，行不可变）
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    anchor_ts INTEGER NOT NULL,
    label INTEGER NOT NULL,              -- 1 正例 / 0 负例 / -1 待定 / -2 剔除
    label_status TEXT NOT NULL,          -- pending / mature / excluded / tombstoned
    label_reason TEXT,
    features_json TEXT NOT NULL,
    feature_cutoff_ts INTEGER NOT NULL,  -- 特征截止时间，必须 == anchor_ts
    source_event_ids_json TEXT NOT NULL,
    created_consent_version INTEGER NOT NULL,
    tombstoned INTEGER NOT NULL DEFAULT 0,
    superseded INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL,
    PRIMARY KEY (sample_id, version)
);
CREATE INDEX IF NOT EXISTS idx_samples_user ON samples (user_id);
CREATE INDEX IF NOT EXISTS idx_samples_anchor ON samples (anchor_ts);

CREATE TABLE IF NOT EXISTS label_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    old_label INTEGER,
    new_label INTEGER NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    trigger_event_ids_json TEXT NOT NULL,
    changed_ts INTEGER NOT NULL,
    consent_version INTEGER NOT NULL
);

-- 数据集（窗口）与版本
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('frozen', 'rolling')),
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    horizon_days INTEGER NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    built_consent_version INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    job_id TEXT,
    n_items INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dataset_id, version)
);

CREATE TABLE IF NOT EXISTS dataset_items (
    dataset_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    sample_id TEXT NOT NULL,
    sample_version INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    anchor_ts INTEGER NOT NULL,
    label INTEGER NOT NULL,
    label_status TEXT NOT NULL,
    tombstoned INTEGER NOT NULL DEFAULT 0,
    features_json TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version, sample_id)
);

-- 许可撤回处置：日志只追加；归档用于演示级恢复
CREATE TABLE IF NOT EXISTS disposal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,               -- withdraw/restore/purge_cache/abort_job ...
    user_id TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    consent_version INTEGER NOT NULL,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS disposal_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,          -- event / sample
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    archived_ts INTEGER NOT NULL,
    restored INTEGER NOT NULL DEFAULT 0,
    restored_ts INTEGER
);

-- 缓存登记：key -> 涉及用户，撤回时按用户失效
CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    user_ids_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    invalidated INTEGER NOT NULL DEFAULT 0
);

-- 任务台账（构建/回补/重建/评估）
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'aborted')),
    pinned_consent_version INTEGER NOT NULL,
    input_hash TEXT,
    result_json TEXT,
    error TEXT,
    created_ts INTEGER NOT NULL,
    started_ts INTEGER,
    finished_ts INTEGER
);

-- 评估产物
CREATE TABLE IF NOT EXISTS evaluations (
    eval_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_version INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    consent_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    n_train INTEGER NOT NULL,
    n_test INTEGER NOT NULL,
    skipped_tombstones INTEGER NOT NULL DEFAULT 0,
    job_id TEXT,
    created_ts INTEGER NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
