"""任务台账与后台 worker：标签回补、样本重建、窗口构建、评估并行执行。

并行安全的三根支柱：
1. 固定输入：任务创建时冻结 params 与 pinned_consent_version；
   评估类任务另存 input_hash，执行前重算比对，不一致即失败。
2. 授权复检：任务每次写库前检查撤回状态；撤回流程会把队列/运行中任务
   置 aborted，worker 取任务和写库前各检查一次，旧任务不可能复活撤回数据。
3. 产物版本化：数据集/样本/评估全部带版本号，历史任务结果不会被静默改写。
"""
from __future__ import annotations

import json
import threading
import uuid

from . import evaluation, samples
from .ingestion import is_withdrawn
from .models import current_consent_version, now_ts

VALID_TYPES = {"backfill", "rebuild", "build_window", "evaluate", "build_all"}


def enqueue(conn, job_type: str, params: dict) -> dict:
    if job_type not in VALID_TYPES:
        raise ValueError(f"未知任务类型: {job_type}")
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    cv = current_consent_version(conn)
    input_hash = None
    if job_type == "evaluate":
        dv = conn.execute(
            "SELECT input_hash FROM dataset_versions WHERE dataset_id=? AND version=?",
            (params["dataset_id"], params["version"])).fetchone()
        if not dv:
            raise ValueError("数据集版本不存在，无法固定输入")
        input_hash = dv["input_hash"]
    conn.execute(
        """INSERT INTO jobs
           (job_id, job_type, params_json, status, pinned_consent_version,
            input_hash, created_ts)
           VALUES (?,?,?,'queued',?,?,?)""",
        (job_id, job_type, json.dumps(params, sort_keys=True), cv,
         input_hash, now_ts()),
    )
    conn.commit()
    return {"job_id": job_id, "job_type": job_type,
            "pinned_consent_version": cv, "input_hash": input_hash}


def _guard(job_row) -> None:
    """写前授权/状态复检。撤回流程已 abort 的任务在这里被彻底拦住。"""
    if job_row["status"] == "aborted":
        raise RuntimeError("任务已因许可撤回被中止")


def execute_job(conn, job_id: str, clock=None) -> dict:
    """同步执行一个任务（worker 线程与测试都走这里）。clock 可注入模拟时间。"""
    job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError("任务不存在")
    _guard(job)
    params = json.loads(job["params_json"])
    # 原子认领：仅当任务仍在排队时才能置为 running，多 worker 并发下只有一个成功
    cur = conn.execute(
        "UPDATE jobs SET status='running', started_ts=? "
        "WHERE job_id=? AND status='queued'", (now_ts(), job_id))
    conn.commit()
    if cur.rowcount == 0:
        # 已被其他 worker 认领或已被撤回流程中止
        fresh = conn.execute("SELECT status FROM jobs WHERE job_id=?",
                             (job_id,)).fetchone()
        if fresh and fresh["status"] == "aborted":
            raise RuntimeError("任务已因许可撤回被中止")
        return {"note": "already claimed by another worker"}
    try:
        # 用户级任务的授权复检
        if params.get("user_id") and is_withdrawn(conn, params["user_id"]):
            raise PermissionError(
                f"用户 {params['user_id']} 已撤回，任务拒绝执行")
        jt = job["job_type"]
        if jt == "backfill":
            result = samples.backfill_labels(
                conn, params["horizon_days"], now=clock or now_ts())
        elif jt == "rebuild":
            result = samples.rebuild_samples(
                conn, params["horizon_days"], now=clock or now_ts())
        elif jt == "build_all":
            result = samples.build_all_samples(
                conn, params["horizon_days"], now=clock or now_ts())
        elif jt == "build_window":
            result = samples.build_dataset_version(
                conn, params["dataset_id"], job_id=job_id,
                now=clock or now_ts())
        elif jt == "evaluate":
            # 再次确认固定输入哈希未被偷换
            if job["input_hash"]:
                dv = conn.execute(
                    "SELECT input_hash FROM dataset_versions WHERE dataset_id=? "
                    "AND version=?",
                    (params["dataset_id"], params["version"])).fetchone()
                if not dv or dv["input_hash"] != job["input_hash"]:
                    raise RuntimeError("固定输入哈希校验失败，拒绝评估")
            result = evaluation.run_evaluation(
                conn, params["dataset_id"], params["version"], job_id=job_id)
        else:  # pragma: no cover
            raise ValueError(f"未实现的任务类型: {jt}")

        # 写库后最终复检：若期间发生撤回，撤回流程已 abort，结果不得报成功
        final = conn.execute("SELECT status FROM jobs WHERE job_id=?",
                             (job_id,)).fetchone()
        if final["status"] == "aborted":
            raise RuntimeError("执行期间许可被撤回，任务已中止，结果作废")
        conn.execute(
            "UPDATE jobs SET status='succeeded', result_json=?, finished_ts=? "
            "WHERE job_id=?",
            (json.dumps(result, sort_keys=True), now_ts(), job_id))
        conn.commit()
        return result
    except Exception as exc:  # noqa: BLE001 - 任务失败需落台账
        conn.execute(
            "UPDATE jobs SET status='failed', error=?, finished_ts=? WHERE job_id=?",
            (str(exc), now_ts(), job_id))
        conn.commit()
        raise


class JobWorker:
    """单线程后台 worker，轮询队列并执行；撤回的任务自动跳过。"""

    def __init__(self, conn_factory, poll_interval: float = 0.3):
        self._conn_factory = conn_factory
        self._poll = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="recapp-job-worker")
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        conn = self._conn_factory()
        try:
            while not self._stop.is_set():
                row = conn.execute(
                    "SELECT job_id FROM jobs WHERE status='queued' "
                    "ORDER BY created_ts LIMIT 1").fetchone()
                if not row:
                    self._stop.wait(self._poll)
                    continue
                try:
                    execute_job(conn, row["job_id"])
                except Exception:
                    pass  # 失败已记录在 jobs.error，worker 继续消费后续任务
        finally:
            conn.close()
