"""演示数据生成（确定性随机，不依赖第三方库）。

场景覆盖：
- 40 天展示/点击/购买/取消/退货全链路
- 一条"成熟之后才发生退款"的曝光，用于演示标签回补把旧正例翻成负例
- 一个待撤回用户（u_withdraw），用于演示撤回处置链与恢复
- 部分曝光尚未成熟，用于演示 frozen/rolling 两种窗口策略的差异
"""
from __future__ import annotations

from . import MATURITY_DAYS_DEFAULT
from .ingestion import add_event
from .models import add_days, day_floor, now_ts


class _Rng:
    def __init__(self, seed: int = 20261116):
        self.state = seed

    def random(self) -> float:
        self.state = (6364136223846793005 * self.state + 1442695) & 0xFFFFFFFFFFFFFFFF
        return (self.state >> 33) / 0x100000000

    def randint(self, lo: int, hi: int) -> int:
        return lo + int(self.random() * (hi - lo + 1))

    def choice(self, seq):
        return seq[int(self.random() * len(seq))]


def seed_demo(conn, horizon_days: int = MATURITY_DAYS_DEFAULT,
              n_users: int = 20, n_items: int = 30, days: int = 40,
              now: int | None = None) -> dict:
    rng = _Rng()
    now = now or now_ts()
    start = day_floor(add_days(now, -days))
    seq = 0

    def eid(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}_{seq:05d}"

    users = [f"u_{i:03d}" for i in range(n_users - 1)] + ["u_withdraw"]
    items = [f"item_{i:03d}" for i in range(n_items)]

    # 用户活跃度差异：高活跃用户转化率高
    activity = {u: 0.3 + 1.4 * rng.random() for u in users}
    quality = {it: 0.4 + 1.2 * rng.random() for it in items}

    counts = {"impression": 0, "click": 0, "purchase": 0,
              "cancel": 0, "refund": 0}
    late_refund_impression = None

    for day in range(days):
        day_ts = start + day * 86400
        for u in users:
            n_imp = rng.randint(0, int(3 * activity[u]))
            for _ in range(n_imp):
                it = rng.choice(items)
                pos = rng.randint(1, 10)
                imp_id = eid("imp")
                imp_ts = day_ts + rng.randint(0, 70000)
                add_event(conn, event_id=eid("evt"), impression_id=imp_id,
                          user_id=u, item_id=it, event_type="impression",
                          event_ts=imp_ts, position=pos)
                counts["impression"] += 1

                p_click = min(0.8, 0.25 * activity[u] * quality[it])
                if rng.random() > p_click:
                    continue
                click_ts = imp_ts + rng.randint(60, 7200)
                add_event(conn, event_id=eid("evt"), impression_id=imp_id,
                          user_id=u, item_id=it, event_type="click",
                          event_ts=click_ts)
                counts["click"] += 1

                p_buy = min(0.7, 0.35 * quality[it])
                if rng.random() > p_buy:
                    continue
                buy_ts = click_ts + rng.randint(120, 10800)
                add_event(conn, event_id=eid("evt"), impression_id=imp_id,
                          user_id=u, item_id=it, event_type="purchase",
                          event_ts=buy_ts)
                counts["purchase"] += 1

                r = rng.random()
                if r < 0.08:  # 取消
                    add_event(conn, event_id=eid("evt"),
                              impression_id=imp_id, user_id=u, item_id=it,
                              event_type="cancel",
                              event_ts=buy_ts + rng.randint(3600, 86400))
                    counts["cancel"] += 1
                elif r < 0.16:  # 正常成熟窗内退货
                    add_event(conn, event_id=eid("evt"),
                              impression_id=imp_id, user_id=u, item_id=it,
                              event_type="refund",
                              event_ts=buy_ts + rng.randint(3600, 5 * 86400))
                    counts["refund"] += 1

    # 特殊演示样本 1：曝光于 20 天前（早已成熟为正例），退款昨天才到 —— 回补翻负例
    demo_anchor = add_days(now, -20)
    demo_imp = "imp_late_refund"
    add_event(conn, event_id="evt_late_refund_imp", impression_id=demo_imp,
              user_id="u_001", item_id="item_001", event_type="impression",
              event_ts=demo_anchor, position=2)
    add_event(conn, event_id="evt_late_refund_click", impression_id=demo_imp,
              user_id="u_001", item_id="item_001", event_type="click",
              event_ts=demo_anchor + 300)
    add_event(conn, event_id="evt_late_refund_buy", impression_id=demo_imp,
              user_id="u_001", item_id="item_001", event_type="purchase",
              event_ts=demo_anchor + 900)
    add_event(conn, event_id="evt_late_refund_refund", impression_id=demo_imp,
              user_id="u_001", item_id="item_001", event_type="refund",
              event_ts=add_days(now, -1))
    late_refund_impression = demo_imp

    # 特殊演示样本 2：u_withdraw 的一条完整转化链，撤回后应整体消失/打墓碑
    w_imp = "imp_withdraw_demo"
    w_ts = add_days(now, -5)
    add_event(conn, event_id="evt_withdraw_imp", impression_id=w_imp,
              user_id="u_withdraw", item_id="item_007",
              event_type="impression", event_ts=w_ts, position=1)
    add_event(conn, event_id="evt_withdraw_click", impression_id=w_imp,
              user_id="u_withdraw", item_id="item_007",
              event_type="click", event_ts=w_ts + 200)
    add_event(conn, event_id="evt_withdraw_buy", impression_id=w_imp,
              user_id="u_withdraw", item_id="item_007",
              event_type="purchase", event_ts=w_ts + 800)

    conn.commit()
    return {"counts": counts, "users": len(users), "items": len(items),
            "horizon_days": horizon_days,
            "late_refund_impression": late_refund_impression,
            "withdraw_demo_user": "u_withdraw", "now": now}
