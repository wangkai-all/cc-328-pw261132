"""事件接入、个性化许可与撤回处置。

撤回处置链（全部留痕，disposal_log 只追加）：
1. consent_versions 追加 granted=0，users 置 withdrawn
2. 原始 events 逐行归档到 disposal_archive 后硬删除
3. 该用户 samples 所有版本归档并打墓碑（tombstoned=1），数据集快照同步标记
4. 引用该用户的缓存全部失效
5. 运行中任务标记 aborted；排队/运行任务在真正写库前还会做授权复检（见 jobs.py）

恢复（演示能力，生产环境通常按保留期清除归档）同样写新授权版本并留痕。
"""
from __future__ import annotations

import json

from .models import is_withdrawn, iso, now_ts


class ConsentError(RuntimeError):
    pass


class ValidationError(RuntimeError):
    pass


# ---------------- 事件接入 ----------------
def add_event(conn, *, event_id: str, impression_id: str, user_id: str,
              item_id: str, event_type: str, event_ts: int,
              position: int | None = None, payload: dict | None = None) -> dict:
    if is_withdrawn(conn, user_id):
        raise ConsentError(f"user {user_id} 已撤回个性化许可，拒绝接入其事件")
    exists = conn.execute("SELECT 1 FROM events WHERE event_id=?",
                          (event_id,)).fetchone()
    if exists:
        raise ValidationError(f"event_id 重复: {event_id}")
    conn.execute(
        """INSERT INTO events
           (event_id, impression_id, user_id, item_id, event_type,
            event_ts, event_time, position, payload_json)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (event_id, impression_id, user_id, item_id, event_type,
         event_ts, iso(event_ts), position, json.dumps(payload or {})),
    )
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, status, created_ts) VALUES (?, 'active', ?)",
        (user_id, event_ts),
    )
    return {"event_id": event_id, "status": "accepted"}


# ---------------- 缓存登记与失效 ----------------
def register_cache(conn, cache_key: str, user_ids: list[str], payload: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO cache_entries
           (cache_key, user_ids_json, payload_json, created_ts, invalidated)
           VALUES (?,?,?,?,0)""",
        (cache_key, json.dumps(sorted(set(user_ids))),
         json.dumps(payload, sort_keys=True), now_ts()),
    )


def get_cache(conn, cache_key: str):
    row = conn.execute(
        "SELECT * FROM cache_entries WHERE cache_key=? AND invalidated=0",
        (cache_key,)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def _invalidate_cache_for_user(conn, user_id: str, cv: int) -> int:
    rows = conn.execute("SELECT cache_key, user_ids_json FROM cache_entries "
                        "WHERE invalidated=0").fetchall()
    n = 0
    for r in rows:
        if user_id in json.loads(r["user_ids_json"]):
            conn.execute("UPDATE cache_entries SET invalidated=1 WHERE cache_key=?",
                         (r["cache_key"],))
            conn.execute(
                """INSERT INTO disposal_log
                   (action, user_id, entity_type, entity_id, detail_json,
                    consent_version, created_ts)
                   VALUES ('purge_cache', ?, 'cache', ?, ?, ?, ?)""",
                (user_id, r["cache_key"], "{}", cv, now_ts()),
            )
            n += 1
    return n


# ---------------- 许可撤回 / 恢复 ----------------
def withdraw(conn, user_id: str, reason: str) -> dict:
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        raise ValidationError(f"用户不存在: {user_id}")
    if user["status"] == "withdrawn":
        raise ConsentError(f"用户 {user_id} 已是撤回状态")

    ts = now_ts()
    cur = conn.execute(
        """INSERT INTO consent_versions (user_id, granted, reason, created_ts, created_at)
           VALUES (?, 0, ?, ?, ?)""",
        (user_id, reason, ts, iso(ts)),
    )
    cv = cur.lastrowid
    conn.execute("UPDATE users SET status='withdrawn' WHERE user_id=?", (user_id,))

    # 1) 原始明细：归档 + 硬删除
    events = conn.execute("SELECT * FROM events WHERE user_id=?", (user_id,)).fetchall()
    for e in events:
        conn.execute(
            """INSERT INTO disposal_archive
               (user_id, entity_type, entity_id, payload_json, archived_ts)
               VALUES (?, 'event', ?, ?, ?)""",
            (user_id, e["event_id"], json.dumps(dict(e)), ts),
        )
    conn.execute("DELETE FROM events WHERE user_id=?", (user_id,))
    _log(conn, "withdraw", user_id, "event", user_id,
         {"archived_event_count": len(events), "hard_deleted": True}, cv, ts)

    # 2) 训练样本：所有版本归档并打墓碑（保留行以维持数据集可复现审计）
    samples = conn.execute(
        "SELECT * FROM samples WHERE user_id=?", (user_id,)).fetchall()
    for s in samples:
        conn.execute(
            """INSERT INTO disposal_archive
               (user_id, entity_type, entity_id, payload_json, archived_ts)
               VALUES (?, 'sample', ?, ?, ?)""",
            (user_id, f"{s['sample_id']}:v{s['version']}", json.dumps(dict(s)), ts),
        )
        conn.execute(
            "UPDATE samples SET tombstoned=1, label_status='tombstoned' "
            "WHERE sample_id=? AND version=?",
            (s["sample_id"], s["version"]),
        )
    # 数据集快照只打墓碑标记，不改业务 label_status ——
    # 业务状态属于固定输入哈希，处置标记（tombstoned）不参与哈希
    conn.execute(
        "UPDATE dataset_items SET tombstoned=1 WHERE user_id=?", (user_id,),
    )
    _log(conn, "tombstone_samples", user_id, "sample", user_id,
         {"tombstoned_sample_versions": len(samples)}, cv, ts)

    # 3) 缓存失效
    n_cache = _invalidate_cache_for_user(conn, user_id, cv)

    # 4) 运行中的任务中止（排队任务由 worker 写前复检兜底）
    jobs = conn.execute(
        "SELECT job_id, job_type FROM jobs WHERE status IN ('queued','running')"
    ).fetchall()
    aborted = []
    for j in jobs:
        params = json.loads(conn.execute(
            "SELECT params_json FROM jobs WHERE job_id=?", (j["job_id"],)).fetchone()
            ["params_json"])
        if params.get("user_id") == user_id or j["job_type"] in (
                "build_window", "backfill", "rebuild", "evaluate"):
            conn.execute(
                "UPDATE jobs SET status='aborted', error=?, finished_ts=? "
                "WHERE job_id=? AND status IN ('queued','running')",
                (f"授权撤回：用户 {user_id} 撤回许可 (consent_version={cv})", ts,
                 j["job_id"]),
            )
            aborted.append(j["job_id"])
            _log(conn, "abort_job", user_id, "job", j["job_id"],
                 {"job_type": j["job_type"]}, cv, ts)

    conn.commit()
    return {
        "user_id": user_id, "consent_version": cv,
        "events_archived_and_deleted": len(events),
        "sample_versions_tombstoned": len(samples),
        "cache_entries_invalidated": n_cache,
        "jobs_aborted": aborted,
    }


def restore(conn, user_id: str, reason: str) -> dict:
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        raise ValidationError(f"用户不存在: {user_id}")
    if user["status"] != "withdrawn":
        raise ConsentError(f"用户 {user_id} 未处于撤回状态，无需恢复")

    ts = now_ts()
    cur = conn.execute(
        """INSERT INTO consent_versions (user_id, granted, reason, created_ts, created_at)
           VALUES (?, 1, ?, ?, ?)""",
        (user_id, reason, ts, iso(ts)),
    )
    cv = cur.lastrowid
    conn.execute("UPDATE users SET status='active' WHERE user_id=?", (user_id,))

    archived = conn.execute(
        "SELECT * FROM disposal_archive WHERE user_id=? AND restored=0", (user_id,)
    ).fetchall()
    restored_events, restored_samples = 0, 0
    for a in archived:
        payload = json.loads(a["payload_json"])
        if a["entity_type"] == "event":
            conn.execute(
                """INSERT OR IGNORE INTO events
                   (event_id, impression_id, user_id, item_id, event_type,
                    event_ts, event_time, position, payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (payload["event_id"], payload["impression_id"], payload["user_id"],
                 payload["item_id"], payload["event_type"], payload["event_ts"],
                 payload["event_time"], payload["position"], payload["payload_json"]),
            )
            restored_events += 1
        elif a["entity_type"] == "sample":
            conn.execute(
                "UPDATE samples SET tombstoned=0, label_status=? "
                "WHERE sample_id=? AND version=?",
                (payload["label_status"], payload["sample_id"], payload["version"]),
            )
            restored_samples += 1
        conn.execute(
            "UPDATE disposal_archive SET restored=1, restored_ts=? WHERE id=?",
            (ts, a["id"]),
        )
    conn.execute(
        "UPDATE dataset_items SET tombstoned=0 WHERE user_id=?", (user_id,),
    )
    _log(conn, "restore", user_id, "user", user_id,
         {"restored_events": restored_events,
          "restored_sample_versions": restored_samples}, cv, ts)
    conn.commit()
    return {"user_id": user_id, "consent_version": cv,
            "restored_events": restored_events,
            "restored_sample_versions": restored_samples}


def _log(conn, action, user_id, entity_type, entity_id, detail, cv, ts):
    conn.execute(
        """INSERT INTO disposal_log
           (action, user_id, entity_type, entity_id, detail_json,
            consent_version, created_ts)
           VALUES (?,?,?,?,?,?,?)""",
        (action, user_id, entity_type, entity_id,
         json.dumps(detail, sort_keys=True), cv, ts),
    )
