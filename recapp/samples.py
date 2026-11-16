"""样本构建、标签版本化、回补与窗口策略。

两种窗口策略（新鲜度 vs 完整性）：
- frozen 冻结观察窗：只纳入 anchor + H <= now 的成熟样本，标签已完整，
  没有 pending 噪声；代价是最近 H 天的曝光不能入样（牺牲新鲜度）。
- rolling 滚动成熟标签：窗内全部曝光立即入样，未成熟者给临时标签
  （status=pending），靠 backfill 在成熟/退款到达后回补出新版本；
  新鲜度高，但在回补完成前标签不完整。

样本不可变：标签或特征的任何变化都追加 (sample_id, version+1)，
旧版本保留并置 superseded=1，label_history 记录每次翻转的触发事件。
"""
from __future__ import annotations

import json
import uuid

from . import FEATURE_VERSION
from .models import (add_days, build_features, compute_label,
                     current_consent_version, is_withdrawn, now_ts)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _impressions(conn, start_ts=None, end_ts=None):
    sql = "SELECT * FROM events WHERE event_type='impression'"
    args = []
    if start_ts is not None:
        sql += " AND event_ts >= ?"
        args.append(start_ts)
    if end_ts is not None:
        sql += " AND event_ts < ?"
        args.append(end_ts)
    sql += " ORDER BY event_ts"
    return conn.execute(sql, args).fetchall()


def _latest_version_map(conn):
    rows = conn.execute(
        "SELECT sample_id, MAX(version) AS v FROM samples GROUP BY sample_id"
    ).fetchall()
    return {r["sample_id"]: r["v"] for r in rows}


def create_sample_version(conn, imp: dict, horizon_days: int,
                          now: int, cv: int,
                          previous: dict | None = None,
                          recompute_features: bool = False) -> dict | None:
    """为一条曝光创建样本新版本。返回新行字典；无变化且非首次则返回 None。"""
    user_id, item_id = imp["user_id"], imp["item_id"]
    anchor_ts = imp["event_ts"]
    if is_withdrawn(conn, user_id, at_version=cv):
        return None  # 撤回用户：不产生任何新样本

    chain = conn.execute(
        "SELECT * FROM events WHERE impression_id=? ORDER BY event_ts",
        (imp["impression_id"],),
    ).fetchall()
    label, status, reason, trigger_ids = compute_label(
        [dict(e) for e in chain], anchor_ts, horizon_days, now)

    # rolling 策略下 pending 样本需要一个临时二分类标签：当前已购买则暂记 1，否则 0
    effective_label = label
    if status == "pending":
        effective_label = 1 if any(
            e["event_type"] == "purchase" for e in chain
            if e["event_ts"] > anchor_ts) else 0

    if previous is not None:
        unchanged = (previous["label"] == effective_label
                     and previous["label_status"] == status
                     and previous["label_reason"] == reason)
        if unchanged and not recompute_features:
            return None
        new_version = previous["version"] + 1
        if recompute_features:
            features, cutoff, source_ids = build_features(
                conn, user_id, item_id, anchor_ts, imp["position"])
        else:
            # 回补只翻标签：特征快照保持锚点时刻的冻结版本，杜绝事后数据泄漏
            features = json.loads(previous["features_json"])
            cutoff = previous["feature_cutoff_ts"]
            source_ids = json.loads(previous["source_event_ids_json"])
    else:
        new_version = 1
        features, cutoff, source_ids = build_features(
            conn, user_id, item_id, anchor_ts, imp["position"])

    assert cutoff == anchor_ts, "特征截止时间必须等于锚点时间"
    sample_id = previous["sample_id"] if previous else _new_id("smp")

    conn.execute(
        """INSERT INTO samples
           (sample_id, version, user_id, item_id, anchor_ts, label, label_status,
            label_reason, features_json, feature_cutoff_ts, source_event_ids_json,
            created_consent_version, tombstoned, superseded, created_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
        (sample_id, new_version, user_id, item_id, anchor_ts, effective_label,
         status, reason, json.dumps(features, sort_keys=True), cutoff,
         json.dumps(source_ids), cv, 0, now),
    )
    if previous is not None:
        conn.execute(
            "UPDATE samples SET superseded=1 WHERE sample_id=? AND version=?",
            (sample_id, previous["version"]),
        )
    conn.execute(
        """INSERT INTO label_history
           (sample_id, version, old_label, new_label, old_status, new_status,
            reason, trigger_event_ids_json, changed_ts, consent_version)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sample_id, new_version,
         previous["label"] if previous else None, effective_label,
         previous["label_status"] if previous else None, status,
         reason or "", json.dumps(trigger_ids), now, cv),
    )
    return {"sample_id": sample_id, "version": new_version,
            "label": effective_label, "label_status": status,
            "label_reason": reason}


def build_all_samples(conn, horizon_days: int, now: int | None = None) -> dict:
    """全量（重）建样本：首次曝光建 v1，其余按最新版本判断是否需要新版本。"""
    now = now or now_ts()
    cv = current_consent_version(conn)
    latest = _latest_version_map(conn)
    created, updated, skipped = 0, 0, 0
    for imp in _impressions(conn):
        prev_row = None
        prev_sid = None
        # sample 与 impression 的对应通过来源事件链维系，这里用锚点查最新版
        row = conn.execute(
            """SELECT * FROM samples WHERE user_id=? AND item_id=? AND anchor_ts=?
               ORDER BY version DESC LIMIT 1""",
            (imp["user_id"], imp["item_id"], imp["event_ts"]),
        ).fetchone()
        if row:
            prev_row, prev_sid = dict(row), row["sample_id"]
        result = create_sample_version(
            conn, dict(imp), horizon_days, now, cv, previous=prev_row)
        if result is None:
            skipped += 1
        elif prev_row is None:
            created += 1
        else:
            updated += 1
    conn.commit()
    return {"created": created, "updated": updated, "skipped": skipped,
            "consent_version": cv}


def backfill_labels(conn, horizon_days: int, now: int | None = None) -> dict:
    """标签回补：重算最新版本样本，捕捉成熟翻转与迟来退款。

    退款优先规则保证：只要事件链中出现 refund，旧的正例一定被新版本负例取代。
    """
    now = now or now_ts()
    cv = current_consent_version(conn)
    latest = _latest_version_map(conn)
    changes = []
    for sample_id, ver in latest.items():
        prev = dict(conn.execute(
            "SELECT * FROM samples WHERE sample_id=? AND version=?",
            (sample_id, ver)).fetchone())
        if prev["tombstoned"]:
            continue  # 撤回数据不复活
        imp = conn.execute(
            """SELECT * FROM events WHERE event_type='impression'
               AND user_id=? AND item_id=? AND event_ts=?""",
            (prev["user_id"], prev["item_id"], prev["anchor_ts"]),
        ).fetchone()
        if imp is None:
            continue  # 原始事件已被撤回硬删除
        result = create_sample_version(
            conn, dict(imp), horizon_days, now, cv, previous=prev)
        if result:
            changes.append(result)
    conn.commit()
    return {"changed": len(changes), "changes": changes, "consent_version": cv}


def rebuild_samples(conn, horizon_days: int, now: int | None = None) -> dict:
    """样本重建：特征与标签都按当前授权版本重算（用于特征版本升级/恢复后重建）。"""
    now = now or now_ts()
    cv = current_consent_version(conn)
    latest = _latest_version_map(conn)
    rebuilt = 0
    for sample_id, ver in latest.items():
        prev = dict(conn.execute(
            "SELECT * FROM samples WHERE sample_id=? AND version=?",
            (sample_id, ver)).fetchone())
        if prev["tombstoned"]:
            continue
        imp = conn.execute(
            """SELECT * FROM events WHERE event_type='impression'
               AND user_id=? AND item_id=? AND event_ts=?""",
            (prev["user_id"], prev["item_id"], prev["anchor_ts"]),
        ).fetchone()
        if imp is None:
            continue
        if create_sample_version(conn, dict(imp), horizon_days, now, cv,
                                 previous=prev, recompute_features=True):
            rebuilt += 1
    conn.commit()
    return {"rebuilt": rebuilt, "consent_version": cv,
            "feature_version": FEATURE_VERSION}


# ---------------- 窗口与数据集版本 ----------------
def create_window(conn, name: str, strategy: str, start_ts: int, end_ts: int,
                  horizon_days: int) -> dict:
    if strategy not in ("frozen", "rolling"):
        raise ValueError("strategy 必须是 frozen 或 rolling")
    if end_ts <= start_ts:
        raise ValueError("窗口结束时间必须晚于开始时间")
    dataset_id = _new_id("ds")
    conn.execute(
        """INSERT INTO datasets
           (dataset_id, name, strategy, start_ts, end_ts, horizon_days,
            current_version, created_ts)
           VALUES (?,?,?,?,?,?,0,?)""",
        (dataset_id, name, strategy, start_ts, end_ts, horizon_days, now_ts()),
    )
    conn.commit()
    return {"dataset_id": dataset_id, "name": name, "strategy": strategy}


def build_dataset_version(conn, dataset_id: str, job_id: str | None = None,
                          now: int | None = None) -> dict:
    ds = conn.execute("SELECT * FROM datasets WHERE dataset_id=?",
                      (dataset_id,)).fetchone()
    if not ds:
        raise ValueError("数据集不存在")
    now = now or now_ts()
    cv = current_consent_version(conn)
    horizon = ds["horizon_days"]
    mature_cutoff = add_days(now, -horizon)

    items = []
    for imp in _impressions(conn, ds["start_ts"], ds["end_ts"]):
        row = conn.execute(
            """SELECT * FROM samples WHERE user_id=? AND item_id=? AND anchor_ts=?
               ORDER BY version DESC LIMIT 1""",
            (imp["user_id"], imp["item_id"], imp["event_ts"]),
        ).fetchone()
        if not row or row["tombstoned"]:
            continue
        # frozen：只要成熟样本（完整性）；rolling：成熟 + 未成熟临时标签（新鲜度）
        if ds["strategy"] == "frozen":
            if row["label_status"] != "mature" or row["anchor_ts"] > mature_cutoff:
                continue
        else:
            if row["label_status"] == "excluded":
                continue
        items.append({
            "sample_id": row["sample_id"],
            "sample_version": row["version"],
            "user_id": row["user_id"], "item_id": row["item_id"],
            "anchor_ts": row["anchor_ts"], "label": row["label"],
            "label_status": row["label_status"],
            "tombstoned": row["tombstoned"],
            "features_json": row["features_json"],
        })

    from .evaluation import dataset_input_hash
    input_hash = dataset_input_hash(items, ds, cv)

    new_ver = ds["current_version"] + 1
    conn.executemany(
        """INSERT INTO dataset_items
           (dataset_id, version, sample_id, sample_version, user_id, item_id,
            anchor_ts, label, label_status, tombstoned, features_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(dataset_id, new_ver, i["sample_id"], i["sample_version"], i["user_id"],
          i["item_id"], i["anchor_ts"], i["label"], i["label_status"],
          i["tombstoned"], i["features_json"]) for i in items],
    )
    conn.execute(
        """INSERT INTO dataset_versions
           (dataset_id, version, created_ts, built_consent_version, input_hash,
            status, job_id, n_items)
           VALUES (?,?,?,?,?,'ready',?,?)""",
        (dataset_id, new_ver, now, cv, input_hash, job_id, len(items)),
    )
    conn.execute("UPDATE datasets SET current_version=? WHERE dataset_id=?",
                 (new_ver, dataset_id))

    # 登记特征缓存（撤回时按用户失效，验证缓存处置链）
    from .ingestion import register_cache
    register_cache(conn, f"features:{dataset_id}:v{new_ver}",
                   sorted({i["user_id"] for i in items}),
                   {"n_items": len(items), "input_hash": input_hash})
    conn.commit()
    return {"dataset_id": dataset_id, "version": new_ver,
            "n_items": len(items), "input_hash": input_hash,
            "consent_version": cv}


def sample_lineage(conn, sample_id: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM samples WHERE sample_id=? ORDER BY version",
        (sample_id,)).fetchall()
    if not rows:
        raise ValueError("样本不存在")
    latest = rows[-1]
    imp = conn.execute(
        "SELECT impression_id FROM events WHERE event_type='impression' "
        "AND user_id=? AND item_id=? AND event_ts=?",
        (latest["user_id"], latest["item_id"], latest["anchor_ts"]),
    ).fetchone()
    impression_id = imp["impression_id"] if imp else ""
    events = conn.execute(
        "SELECT * FROM events WHERE impression_id=? ORDER BY event_ts",
        (impression_id,)).fetchall() if impression_id else []
    history = conn.execute(
        "SELECT * FROM label_history WHERE sample_id=? ORDER BY version",
        (sample_id,)).fetchall()
    return {
        "sample_id": sample_id,
        "user_id": latest["user_id"], "item_id": latest["item_id"],
        "anchor_ts": latest["anchor_ts"],
        "versions": [dict(r) for r in rows],
        "source_events": [dict(e) for e in events],
        "label_history": [dict(h) for h in history],
    }
