"""领域模型：时间、标签状态机、防泄漏特征、授权与哈希工具。

标签规则（以一次曝光 impression 为锚点 anchor，成熟窗 horizon_days 天）：
- anchor 后出现 refund（退货）           -> 负例 0，status=mature（退款后绝不当正例）
- anchor 后出现 cancel（取消，无退款）     -> -2，status=excluded（不进训练/评估）
- anchor 后有 purchase 且无取消/退款      -> 正例 1
- anchor 后只有点击/无转化                -> 负例 0
- anchor + H 天之前且尚未有终态           -> -1，status=pending（未成熟）

防泄漏铁律：特征构建只读取 event_ts <= anchor 的事件；
features 快照随样本落库，之后事件再变化也不会污染已冻结的样本。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

DAY_SECONDS = 86400

# 反馈事件的生命周期顺序（仅用于解释，不强制发生顺序校验之外的语义）
EVENT_TYPES = ("impression", "click", "purchase", "cancel", "refund")


# ---------- 时间工具（统一 epoch 秒，展示用 ISO 字符串） ----------
def now_ts() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_iso(s: str) -> int:
    s = s.replace("Z", "+00:00")
    return int(datetime.fromisoformat(s).timestamp())


def add_days(ts: int, days: int) -> int:
    return ts + days * DAY_SECONDS


def day_floor(ts: int) -> int:
    return (ts // DAY_SECONDS) * DAY_SECONDS


# ---------- 稳定哈希 ----------
def stable_hash(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


# ---------- 授权 ----------
def current_consent_version(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM consent_versions"
    ).fetchone()
    return int(row["v"])


def is_withdrawn(conn, user_id: str, at_version: int | None = None) -> bool:
    """用户是否已撤回。at_version 给定时，只看该版本之前（含）的授权记录。"""
    if at_version is None:
        row = conn.execute(
            "SELECT status FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return bool(row and row["status"] == "withdrawn")
    row = conn.execute(
        """SELECT granted FROM consent_versions
           WHERE user_id=? AND version<=? ORDER BY version DESC LIMIT 1""",
        (user_id, at_version),
    ).fetchone()
    return bool(row and row["granted"] == 0)


# ---------- 标签状态机 ----------
def compute_label(events: list[dict], anchor_ts: int, horizon_days: int,
                  now: int) -> tuple[int, str, str | None, list[str]]:
    """根据某条曝光的完整事件链计算标签。

    返回 (label, status, reason, trigger_event_ids)。
    事件按 event_ts 排序，只看 anchor 之后的转化类事件。
    """
    after = sorted(
        (e for e in events if e["event_ts"] > anchor_ts
         and e["event_type"] in ("click", "purchase", "cancel", "refund")),
        key=lambda e: e["event_ts"],
    )
    types = {e["event_type"] for e in after}
    triggers = [e["event_id"] for e in after]
    mature_at = add_days(anchor_ts, horizon_days)

    # 退货优先级最高：哪怕发生在成熟窗之外（迟来的退款），也必须翻负例
    if "refund" in types:
        refund_ids = [e["event_id"] for e in after if e["event_type"] == "refund"]
        return 0, "mature", "refund_after_purchase", refund_ids
    if "cancel" in types:
        cancel_ids = [e["event_id"] for e in after if e["event_type"] == "cancel"]
        return -2, "excluded", "cancelled_order", cancel_ids
    if now < mature_at:
        return -1, "pending", "not_matured", []
    if "purchase" in types:
        return 1, "mature", "purchased", triggers
    if "click" in types:
        return 0, "mature", "clicked_no_purchase", triggers
    return 0, "mature", "no_engagement", triggers


# ---------- 防泄漏特征构建 ----------
def build_features(conn, user_id: str, item_id: str, anchor_ts: int,
                   position: int | None) -> tuple[dict, int, list[str]]:
    """只用 event_ts <= anchor_ts 的数据构建特征。

    第二返回值 feature_cutoff_ts 恒等于 anchor_ts，落库后可审计；
    第三返回值为特征实际引用的来源事件 id。
    """
    rows = conn.execute(
        """SELECT event_id, event_type, event_ts FROM events
           WHERE event_ts <= ? AND (user_id=? OR item_id=?)
           ORDER BY event_ts""",
        (anchor_ts, user_id, item_id),
    ).fetchall()

    # 断言式防护：任何越界数据都视为编程错误，直接抛出而不是静默污染样本
    assert all(r["event_ts"] <= anchor_ts for r in rows), "feature leakage detected"

    user_evs = conn.execute(
        """SELECT event_id, event_type FROM events
           WHERE event_ts <= ? AND user_id=?""",
        (anchor_ts, user_id),
    ).fetchall()
    item_evs = conn.execute(
        """SELECT event_id, event_type FROM events
           WHERE event_ts <= ? AND item_id=?""",
        (anchor_ts, item_id),
    ).fetchall()

    def _count(evs, t):
        return sum(1 for e in evs if e["event_type"] == t)

    u_imp = max(1, _count(user_evs, "impression"))
    u_click = _count(user_evs, "click")
    u_purch = _count(user_evs, "purchase")
    i_imp = max(1, _count(item_evs, "impression"))
    i_click = _count(item_evs, "click")
    i_purch = _count(item_evs, "purchase")

    features = {
        "position": float(position) if position is not None else 0.0,
        "user_ctr": u_click / u_imp,
        "user_cvr": u_purch / u_imp,
        "item_ctr": i_click / i_imp,
        "item_cvr": i_purch / i_imp,
        "user_history_len": float(len(user_evs)),
        "item_history_len": float(len(item_evs)),
    }
    source_ids = [e["event_id"] for e in user_evs] + [e["event_id"] for e in item_evs]
    return features, anchor_ts, sorted(set(source_ids))


FEATURE_KEYS = (
    "position", "user_ctr", "user_cvr", "item_ctr",
    "item_cvr", "user_history_len", "item_history_len",
)
