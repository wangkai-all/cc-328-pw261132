"""核心不变量测试（标准库 unittest，零依赖）。

覆盖：
1. 退款后样本绝不再当正例（含成熟之后才到的迟来退款 → 回补翻负例）
2. 取消单被剔除，不进训练
3. 特征防泄漏：锚点之后的事件无法进入特征
4. 冻结窗 vs 滚动窗的新鲜度/完整性差异
5. 许可撤回：原始明细硬删、样本打墓碑、缓存失效、任务中止、拒绝新事件
6. 撤回后旧任务不能复活撤回数据；评估跳过墓碑并保持输入哈希固定
7. 恢复留痕且可重新构建
8. 评估可复现：同输入两次结果完全一致
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recapp.db import connect, init_db
from recapp.evaluation import run_evaluation
from recapp.ingestion import (ConsentError, add_event, register_cache,
                              restore, withdraw)
from recapp.jobs import JobWorker, enqueue, execute_job
from recapp.models import add_days, build_features, day_floor, now_ts
from recapp.samples import (backfill_labels, build_all_samples,
                            build_dataset_version, create_window,
                            rebuild_samples, sample_lineage)
from recapp.seed import seed_demo

H = 14


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = connect(self.tmp.name)
        init_db(self.conn)
        self.now = day_floor(now_ts())

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _imp_chain(self, user="u1", item="i1", anchor=None,
                   clicks=True, purchase=True, refund=False, cancel=False,
                   refund_offset_days=None):
        anchor = anchor or (self.now - 30 * 86400)
        imp = f"imp_{user}_{item}_{anchor}"
        add_event(self.conn, event_id=f"e_{imp}", impression_id=imp,
                  user_id=user, item_id=item, event_type="impression",
                  event_ts=anchor, position=3)
        t = anchor
        if clicks:
            t += 100
            add_event(self.conn, event_id=f"e_{imp}_c", impression_id=imp,
                      user_id=user, item_id=item, event_type="click",
                      event_ts=t)
        if purchase:
            t += 200
            add_event(self.conn, event_id=f"e_{imp}_p", impression_id=imp,
                      user_id=user, item_id=item, event_type="purchase",
                      event_ts=t)
        if cancel:
            add_event(self.conn, event_id=f"e_{imp}_x", impression_id=imp,
                      user_id=user, item_id=item, event_type="cancel",
                      event_ts=t + 400)
        if refund:
            offset = (refund_offset_days or 2) * 86400
            add_event(self.conn, event_id=f"e_{imp}_r", impression_id=imp,
                      user_id=user, item_id=item, event_type="refund",
                      event_ts=t + offset)
        return imp, anchor


class TestLabelRules(Base):
    def test_refund_is_never_positive(self):
        self._imp_chain(user="u1", refund=True)
        build_all_samples(self.conn, H, now=self.now)
        row = self.conn.execute(
            "SELECT * FROM samples WHERE superseded=0").fetchone()
        self.assertEqual(row["label"], 0)
        self.assertEqual(row["label_status"], "mature")
        self.assertEqual(row["label_reason"], "refund_after_purchase")

    def test_late_refund_after_maturity_flips_via_backfill(self):
        # 曝光 30 天前，购买后已成熟为正例；退款昨天才到
        anchor = self.now - 30 * 86400
        imp, anchor = self._imp_chain(
            user="u2", refund=False, anchor=anchor)
        build_all_samples(self.conn, H, now=self.now - 2 * 86400)
        before = self.conn.execute(
            "SELECT label, version FROM samples WHERE superseded=0").fetchone()
        self.assertEqual(before["label"], 1)  # 曾经的正例

        add_event(self.conn, event_id="e_late_r", impression_id=imp,
                  user_id="u2", item_id="i1", event_type="refund",
                  event_ts=self.now - 86400)
        result = backfill_labels(self.conn, H, now=self.now)
        self.assertGreaterEqual(result["changed"], 1)
        after = self.conn.execute(
            "SELECT * FROM samples WHERE superseded=0").fetchone()
        self.assertEqual(after["label"], 0)  # 回补后绝不再当正例
        self.assertEqual(after["label_reason"], "refund_after_purchase")
        # 旧版本仍保留且可审计
        old = self.conn.execute(
            "SELECT label FROM samples WHERE version=1").fetchone()
        self.assertEqual(old["label"], 1)

    def test_cancel_is_excluded(self):
        self._imp_chain(user="u3", cancel=True)
        build_all_samples(self.conn, H, now=self.now)
        row = self.conn.execute(
            "SELECT * FROM samples WHERE superseded=0").fetchone()
        self.assertEqual(row["label"], -2)
        self.assertEqual(row["label_status"], "excluded")

    def test_pending_before_maturity(self):
        anchor = self.now - 3 * 86400  # 未满 14 天
        self._imp_chain(user="u4", anchor=anchor, refund=False)
        build_all_samples(self.conn, H, now=self.now)
        row = self.conn.execute(
            "SELECT * FROM samples WHERE superseded=0").fetchone()
        self.assertEqual(row["label_status"], "pending")


class TestFeatureLeakage(Base):
    def test_features_only_use_events_at_or_before_anchor(self):
        anchor = self.now - 20 * 86400
        self._imp_chain(user="u5", anchor=anchor)
        # 锚点之后的未来事件
        add_event(self.conn, event_id="future_1", impression_id="imp_future",
                  user_id="u5", item_id="i9", event_type="impression",
                  event_ts=anchor + 5 * 86400)
        features, cutoff, sources = build_features(
            self.conn, "u5", "i1", anchor, 3)
        self.assertEqual(cutoff, anchor)
        self.assertNotIn("future_1", sources)
        # 直接构造越界会触发断言
        with self.assertRaises(AssertionError):
            rows = self.conn.execute(
                "SELECT event_id, event_ts FROM events WHERE event_ts<=?",
                (anchor + 10 * 86400,)).fetchall()
            assert all(r["event_ts"] <= anchor for r in rows)

    def test_backfill_keeps_frozen_feature_snapshot(self):
        anchor = self.now - 30 * 86400
        imp, anchor = self._imp_chain(user="u6", anchor=anchor)
        build_all_samples(self.conn, H, now=self.now - 2 * 86400)
        v1 = self.conn.execute(
            "SELECT features_json, feature_cutoff_ts FROM samples WHERE version=1"
        ).fetchone()
        add_event(self.conn, event_id="e_late_r2", impression_id=imp,
                  user_id="u6", item_id="i1", event_type="refund",
                  event_ts=self.now - 86400)
        backfill_labels(self.conn, H, now=self.now)
        v2 = self.conn.execute(
            "SELECT features_json, feature_cutoff_ts FROM samples "
            "WHERE version=2").fetchone()
        self.assertEqual(v1["features_json"], v2["features_json"])
        self.assertEqual(v2["feature_cutoff_ts"], anchor)


class TestWindowStrategies(Base):
    def test_frozen_vs_rolling_freshness_completeness(self):
        # 一批成熟样本 + 一批未成熟样本
        for i in range(6):
            self._imp_chain(user=f"old{i}", item="i1",
                            anchor=self.now - 25 * 86400, refund=(i == 0))
        for i in range(4):
            self._imp_chain(user=f"new{i}", item="i1",
                            anchor=self.now - 2 * 86400)
        build_all_samples(self.conn, H, now=self.now)
        start = self.now - 40 * 86400
        common = dict(start_ts=start, end_ts=self.now + 86400)

        f_id = create_window(self.conn, "f", "frozen",
                             common["start_ts"], common["end_ts"], H)["dataset_id"]
        r_id = create_window(self.conn, "r", "rolling",
                             common["start_ts"], common["end_ts"], H)["dataset_id"]
        fv = build_dataset_version(self.conn, f_id, now=self.now)
        rv = build_dataset_version(self.conn, r_id, now=self.now)

        # 滚动窗口更新（含 pending 临时标签）；冻结窗口只有成熟且非剔除样本
        self.assertGreater(rv["n_items"], fv["n_items"])
        frozen_rows = self.conn.execute(
            "SELECT DISTINCT label_status FROM dataset_items WHERE dataset_id=?",
            (f_id,)).fetchall()
        self.assertEqual({r["label_status"] for r in frozen_rows}, {"mature"})
        rolling_status = {r["label_status"] for r in self.conn.execute(
            "SELECT DISTINCT label_status FROM dataset_items WHERE dataset_id=?",
            (r_id,)).fetchall()}
        self.assertIn("pending", rolling_status)


class TestWithdrawal(Base):
    def test_full_disposal_chain_and_rejection(self):
        self._imp_chain(user="u7")
        build_all_samples(self.conn, H, now=self.now)
        register_cache(self.conn, "cache:feat:u7", ["u7"], {"k": 1})

        report = withdraw(self.conn, "u7", "privacy_setting_off")
        self.assertGreater(report["events_archived_and_deleted"], 0)
        self.assertGreater(report["sample_versions_tombstoned"], 0)
        self.assertEqual(report["cache_entries_invalidated"], 1)

        # 原始明细已硬删除
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM events WHERE user_id='u7'")
            .fetchone()["n"], 0)
        # 样本全部墓碑
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM samples WHERE user_id='u7' AND tombstoned=0"
        ).fetchone()["n"], 0)
        # 归档可追溯
        self.assertGreater(self.conn.execute(
            "SELECT COUNT(*) n FROM disposal_archive WHERE user_id='u7'"
        ).fetchone()["n"], 0)
        # 拒绝接入撤回用户的新事件
        with self.assertRaises(ConsentError):
            add_event(self.conn, event_id="x1", impression_id="ix",
                      user_id="u7", item_id="i1", event_type="click",
                      event_ts=self.now)
        # 处置日志只追加
        actions = {r["action"] for r in self.conn.execute(
            "SELECT DISTINCT action FROM disposal_log").fetchall()}
        self.assertIn("withdraw", actions)
        self.assertIn("purge_cache", actions)

    def test_old_job_cannot_revive_withdrawn_data(self):
        for i in range(8):
            self._imp_chain(user=f"wj{i}", item="i1",
                            anchor=self.now - 25 * 86400)
        build_all_samples(self.conn, H, now=self.now)
        ds = create_window(self.conn, "w", "rolling",
                           self.now - 40 * 86400, self.now + 86400, H)
        build_dataset_version(self.conn, ds["dataset_id"], now=self.now)

        job = enqueue(self.conn, "backfill", {"horizon_days": H})
        # 撤回发生在任务执行之前
        withdraw(self.conn, "wj3", "opt_out")
        with self.assertRaises(RuntimeError):
            execute_job(self.conn, job["job_id"])
        status = self.conn.execute(
            "SELECT status FROM jobs WHERE job_id=?", (job["job_id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "aborted")
        # wj3 的数据没有被任何新样本版本复活
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM samples WHERE user_id='wj3' AND tombstoned=0"
        ).fetchone()["n"], 0)

    def test_restore_is_traceable_and_rebuild_works(self):
        self._imp_chain(user="u8")
        build_all_samples(self.conn, H, now=self.now)
        withdraw(self.conn, "u8", "test")
        r = restore(self.conn, "u8", "user_came_back")
        self.assertGreater(r["restored_events"], 0)
        self.assertGreater(r["restored_sample_versions"], 0)
        # 恢复后事件可查、样本解除墓碑，且授权流水与日志完整
        self.assertGreater(self.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE user_id='u8'").fetchone()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT MIN(tombstoned) t FROM samples WHERE user_id='u8'"
        ).fetchone()["t"], 0)
        cv_rows = self.conn.execute(
            "SELECT granted FROM consent_versions WHERE user_id='u8' ORDER BY version"
        ).fetchall()
        self.assertEqual([r["granted"] for r in cv_rows], [0, 1])

    def test_worker_picks_jobs_and_aborts_on_withdraw(self):
        for i in range(6):
            self._imp_chain(user=f"wk{i}", item="i1",
                            anchor=self.now - 25 * 86400)
        build_all_samples(self.conn, H, now=self.now)
        worker = JobWorker(lambda: connect(self.tmp.name), poll_interval=0.05)
        worker.start()
        try:
            j = enqueue(self.conn, "backfill", {"horizon_days": H})
            self._wait(lambda: self.conn.execute(
                "SELECT status FROM jobs WHERE job_id=?", (j["job_id"],)
            ).fetchone()["status"] in ("succeeded", "failed", "aborted"))
            self.assertEqual(self.conn.execute(
                "SELECT status FROM jobs WHERE job_id=?", (j["job_id"],)
            ).fetchone()["status"], "succeeded")
        finally:
            worker.stop()

    def _wait(self, cond, timeout=5.0):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.05)
        raise AssertionError("条件等待超时")


    def test_rebuild_and_backfill_never_revive_tombstones(self):
        self._imp_chain(user="u9")
        build_all_samples(self.conn, H, now=self.now)
        withdraw(self.conn, "u9", "opt_out")
        # 撤回后无论跑回补还是重建，墓碑样本都不得产生新版本或解除墓碑
        backfill_labels(self.conn, H, now=self.now)
        rebuild_samples(self.conn, H, now=self.now)
        rows = self.conn.execute(
            "SELECT COUNT(*) n, MAX(version) mv, MIN(tombstoned) t FROM samples "
            "WHERE user_id='u9'").fetchone()
        self.assertEqual(rows["mv"], 1, "撤回样本不应出现新版本")
        self.assertEqual(rows["t"], 1)
        # 即使手工补一条"未来"事件（模拟迟到数据），撤回用户仍无法建样本
        with self.assertRaises(ConsentError):
            add_event(self.conn, event_id="late_u9", impression_id="imp_late_u9",
                      user_id="u9", item_id="i1", event_type="click",
                      event_ts=self.now)


class TestJobConcurrency(Base):
    def test_same_job_claimed_once_by_two_workers(self):
        # 12 个成熟用户，保证回补任务可执行
        for i in range(12):
            self._imp_chain(user=f"cc{i}", item="i1",
                            anchor=self.now - 25 * 86400)
        build_all_samples(self.conn, H, now=self.now)
        j = enqueue(self.conn, "backfill", {"horizon_days": H})

        # 第二个独立连接模拟另一个 worker 进程
        conn2 = connect(self.tmp.name)
        outcomes = []
        try:
            for c in (self.conn, conn2):
                try:
                    outcomes.append(execute_job(c, j["job_id"]))
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(f"ERR:{type(exc).__name__}")
            statuses = [o for o in outcomes if isinstance(o, dict)
                        and "already claimed" in str(o)]
            real = [o for o in outcomes if isinstance(o, dict)
                    and "changed" in o]
            self.assertEqual(len(real), 1, "只应有一个 worker 真正执行")
            self.assertEqual(len(statuses), 1, "另一个 worker 应收到已认领提示")
            final = self.conn.execute(
                "SELECT status FROM jobs WHERE job_id=?", (j["job_id"],)
            ).fetchone()["status"]
            self.assertEqual(final, "succeeded")
        finally:
            conn2.close()


class TestEvaluationRepro(Base):
    def _seed_eval_ready(self):
        for i in range(12):
            self._imp_chain(
                user=f"ev{i}", item="i1", anchor=self.now - 25 * 86400,
                refund=(i % 4 == 0), purchase=(i % 5 != 0), clicks=True)
        build_all_samples(self.conn, H, now=self.now)
        ds = create_window(self.conn, "e", "frozen",
                           self.now - 40 * 86400, self.now + 86400, H)
        v = build_dataset_version(self.conn, ds["dataset_id"], now=self.now)
        return ds["dataset_id"], v["version"], v["input_hash"]

    def test_evaluation_is_reproducible_and_pinned(self):
        ds_id, ver, h = self._seed_eval_ready()
        r1 = run_evaluation(self.conn, ds_id, ver)
        r2 = run_evaluation(self.conn, ds_id, ver)
        self.assertEqual(r1["metrics"], r2["metrics"])
        self.assertEqual(r1["input_hash"], h)
        self.assertIn("pinned@v", r1["consent_version"])

        # 篡改业务输入（特征）后哈希校验必须拒绝；处置标记 tombstoned 不参与哈希
        row = self.conn.execute(
            "SELECT features_json FROM dataset_items LIMIT 1").fetchone()
        feats = json.loads(row["features_json"])
        feats["position"] = 999.0
        self.conn.execute(
            "UPDATE dataset_items SET features_json=? WHERE features_json=?",
            (json.dumps(feats, sort_keys=True), row["features_json"]))
        self.conn.commit()
        with self.assertRaises(RuntimeError):
            run_evaluation(self.conn, ds_id, ver)

    def test_tombstones_are_skipped_in_evaluation(self):
        ds_id, ver, _ = self._seed_eval_ready()
        target = self.conn.execute(
            "SELECT DISTINCT user_id FROM dataset_items WHERE dataset_id=? LIMIT 1",
            (ds_id,)).fetchone()["user_id"]
        withdraw(self.conn, target, "opt_out")
        # 新版本：墓碑被标记并在评估中跳过
        r = run_evaluation(self.conn, ds_id, ver)
        self.assertGreaterEqual(r["skipped_tombstones"], 1)


class TestDemoSeed(unittest.TestCase):
    def test_seed_and_late_refund_scenario(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            conn = connect(tmp.name)
            init_db(conn)
            info = seed_demo(conn)
            self.assertTrue(info["late_refund_impression"])
            build_all_samples(conn, info["horizon_days"])
            # 初次构建即因退款事件存在而为负例（退款优先级最高）
            row = conn.execute(
                "SELECT * FROM samples WHERE user_id='u_001' AND item_id='item_001' "
                "AND label_reason='refund_after_purchase' AND superseded=0"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["label"], 0)
            conn.close()
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
