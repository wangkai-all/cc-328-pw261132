"""端到端 HTTP 冒烟脚本：走完整业务闭环并断言关键不变量。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
H = {"Content-Type": "application/json"}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=H, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait_job(predicate_desc, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = call("GET", "/api/jobs")
        pending = [j for j in jobs if j["status"] in ("queued", "running")]
        if not pending:
            return jobs
        time.sleep(0.2)
    raise AssertionError(f"等待任务完成超时: {predicate_desc}")


def main():
    print("== 1. 首次全量构建样本 ==")
    j = call("POST", "/api/jobs/build-all", {"horizon_days": 14})
    wait_job("build_all")
    ov = call("GET", "/api/overview")
    print("   样本状态分布:", ov["sample_counts"])
    assert ov["sample_counts"].get("mature", 0) > 0

    print("== 2. 标签回补：迟来退款必须是负例 ==")
    call("POST", "/api/jobs/backfill", {"horizon_days": 14})
    wait_job("backfill")
    samples = call("GET", "/api/samples?limit=300")
    lr = [s for s in samples if s["user_id"] == "u_001" and s["item_id"] == "item_001"]
    assert lr and all(s["label"] == 0 for s in lr), "退款样本仍为正例"
    assert all(s["label_reason"] == "refund_after_purchase" for s in lr)
    # 全量不变量：没有任何退款样本带正标签
    assert not [s for s in samples if s["label"] == 1
                and s["label_reason"] == "refund_after_purchase"]
    print(f"   迟来退款样本 {lr[0]['sample_id']} label=0 ✓；总样本 {len(samples)}")

    print("== 3. 同输入创建冻结/滚动窗口 ==")
    now = int(time.time())
    body = {"name": "smoke", "start_ts": now - 40 * 86400,
            "end_ts": now + 86400, "horizon_days": 14}
    f = call("POST", "/api/windows", {**body, "name": "smoke-frozen",
                                      "strategy": "frozen"})
    r = call("POST", "/api/windows", {**body, "name": "smoke-rolling",
                                      "strategy": "rolling"})
    for ds in (f, r):
        call("POST", "/api/jobs/build-window", {"dataset_id": ds["dataset_id"]})
    wait_job("windows")
    datasets = {d["name"]: d for d in call("GET", "/api/datasets")}
    fv = datasets["smoke-frozen"]["versions"][0]
    rv = datasets["smoke-rolling"]["versions"][0]
    print(f"   冻结窗样本={fv['n_items']}（全 mature），滚动窗样本={rv['n_items']}（含 pending）")
    assert rv["n_items"] > fv["n_items"], "滚动窗应更新（新鲜度）"
    assert fv["input_hash"] != rv["input_hash"]

    print("== 4. 评估两个窗口并比较指标 ==")
    call("POST", "/api/jobs/evaluate",
         {"dataset_id": f["dataset_id"], "version": 1})
    call("POST", "/api/jobs/evaluate",
         {"dataset_id": r["dataset_id"], "version": 1})
    wait_job("evals")
    evals = call("GET", "/api/evaluations")
    assert len(evals) >= 2
    for e in evals[:2]:
        assert "pinned@v" in e["consent_version"]
        print(f"   {e['dataset_id'][:16]} v{e['dataset_version']}: "
              f"AUC={e['metrics']['auc']} "
              f"F1(自适应)={e['metrics']['at_adaptive']['f1']} "
              f"hash={e['input_hash'][:10]}")

    print("== 5. 撤回 u_withdraw：处置链全验证 ==")
    rep = call("POST", "/api/withdraw",
               {"user_id": "u_withdraw", "reason": "smoke_opt_out"})
    print("  ", rep)
    assert rep["events_archived_and_deleted"] > 0
    assert rep["sample_versions_tombstoned"] > 0
    assert rep["cache_entries_invalidated"] >= 1
    # 原始事件硬删
    events = call("GET", "/api/events?limit=300")
    assert not [e for e in events if e["user_id"] == "u_withdraw"]
    # 样本全墓碑
    samples2 = call("GET", "/api/samples?limit=300")
    wd = [s for s in samples2 if s["user_id"] == "u_withdraw"]
    assert all(s["tombstoned"] == 1 for s in wd)
    # 缓存失效
    caches = call("GET", "/api/caches")
    assert all(c["invalidated"] == 1 for c in caches
               if "u_withdraw" in c["user_ids"])
    # 撤回后拒绝新事件
    try:
        call("POST", "/api/events", {
            "event_id": "x_smoke_1", "impression_id": "ix",
            "user_id": "u_withdraw", "item_id": "item_001",
            "event_type": "click", "event_ts": now})
        raise AssertionError("撤回后事件应被拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 400

    print("== 6. 撤回后再评估：墓碑跳过、哈希仍可校验 ==")
    call("POST", "/api/jobs/evaluate",
         {"dataset_id": r["dataset_id"], "version": 1})
    wait_job("re-eval")
    evals_after = call("GET", "/api/evaluations")
    rolling_evals = [e for e in evals_after
                     if e["dataset_id"] == r["dataset_id"]]
    # 成熟墓碑必须被跳过（pending 行本就不参与监督指标）
    assert any(e["skipped_tombstones"] >= 1 for e in rolling_evals)
    # 同一冻结输入的哈希在撤回前后保持一致（可复现）
    hashes = {e["input_hash"] for e in rolling_evals}
    assert len(hashes) == 1
    print(f"   成熟墓碑跳过已记录，撤回前后 input_hash 恒为 {hashes.pop()[:10]} ✓")

    print("== 7. 恢复许可并验证留痕 ==")
    restored = call("POST", "/api/restore",
                    {"user_id": "u_withdraw", "reason": "smoke_come_back"})
    assert restored["restored_events"] > 0
    consent = call("GET", "/api/consent")
    flow = [v["granted"] for v in consent["recent_versions"]
            if v["user_id"] == "u_withdraw"]
    assert sorted(flow) == [0, 1]
    print("  ", restored, "；授权流水 0→1 ✓")

    print("== 8. 审计日志只追加、归档可追溯 ==")
    disposal = call("GET", "/api/disposal")
    actions = {l["action"] for l in disposal["logs"]}
    # abort_job 的确定性覆盖见单元测试 test_old_job_cannot_revive_withdrawn_data
    assert {"withdraw", "tombstone_samples", "purge_cache",
            "restore"} <= actions
    assert any(a["user_id"] == "u_withdraw" for a in disposal["archives"])
    print("   处置动作:", sorted(actions))

    print("\n全部端到端断言通过 ✓")


if __name__ == "__main__":
    main()
