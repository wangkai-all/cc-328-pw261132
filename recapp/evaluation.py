"""评估存储与简单基线（纯标准库实现的逻辑回归）。

可复现性约束：
- 评估只接受数据集的某个冻结版本（dataset_id + version）
- 读入后重算输入哈希，与 dataset_versions.input_hash 不一致则拒绝执行
- 墓碑样本跳过并计数（撤回数据绝不参与训练/评估）
- 评估产物记录 consent_version / code_version / feature_version / input_hash
"""
from __future__ import annotations

import json
import math
import uuid

from . import CODE_VERSION, FEATURE_VERSION
from .models import FEATURE_KEYS, now_ts, stable_hash


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------- 纯 Python 逻辑回归（梯度下降，固定随机种子） ----------
class LogisticRegression:
    def __init__(self, n_features: int, lr: float = 0.1, epochs: int = 200,
                 l2: float = 0.001, seed: int = 42):
        rng = _Lcg(seed)
        self.w = [(rng.next() - 0.5) * 0.01 for _ in range(n_features)]
        self.b = 0.0
        self.lr, self.epochs, self.l2 = lr, epochs, l2
        self.mean = [0.0] * n_features
        self.scale = [1.0] * n_features

    def _standardize_fit(self, X):
        n = len(X)
        for j in range(len(self.w)):
            mu = sum(row[j] for row in X) / n
            var = sum((row[j] - mu) ** 2 for row in X) / n
            self.mean[j] = mu
            self.scale[j] = math.sqrt(var) if var > 1e-12 else 1.0

    def _z(self, row):
        z = self.b
        for j, x in enumerate(row):
            z += self.w[j] * ((x - self.mean[j]) / self.scale[j])
        return z

    @staticmethod
    def _sigmoid(z):
        if z >= 0:
            ez = math.exp(-z)
            return 1.0 / (1.0 + ez)
        ez = math.exp(z)
        return ez / (1.0 + ez)

    def fit(self, X, y):
        self._standardize_fit(X)
        n = len(X)
        for _ in range(self.epochs):
            gw = [0.0] * len(self.w)
            gb = 0.0
            for row, yi in zip(X, y):
                p = self._sigmoid(self._z(row))
                err = p - yi
                for j, x in enumerate(row):
                    gw[j] += err * ((x - self.mean[j]) / self.scale[j])
                gb += err
            for j in range(len(self.w)):
                self.w[j] -= self.lr * (gw[j] / n + self.l2 * self.w[j])
            self.b -= self.lr * gb / n
        return self

    def predict_proba(self, X):
        return [self._sigmoid(self._z(row)) for row in X]


class _Lcg:
    """确定性线性同余随机数，保证跨机器可复现。"""
    def __init__(self, seed: int):
        self.state = seed & 0x7FFFFFFF

    def next(self) -> float:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state / 0x7FFFFFFF


# ---------- 指标 ----------
def _auc(scores, labels) -> float | None:
    """秩和法 AUC；只有一个类别时返回 None。"""
    if len(set(labels)) < 2:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1 起始秩
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    rank_sum_pos = sum(ranks[i] for i, y in enumerate(labels) if y == 1)
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def compute_metrics(probs, labels, threshold: float = 0.5) -> dict:
    preds = [1 if p >= threshold else 0 for p in probs]
    tp = sum(1 for p, y in zip(preds, labels) if p == 1 and y == 1)
    fp = sum(1 for p, y in zip(preds, labels) if p == 1 and y == 0)
    fn = sum(1 for p, y in zip(preds, labels) if p == 0 and y == 1)
    tn = sum(1 for p, y in zip(preds, labels) if p == 0 and y == 0)
    acc = (tp + tn) / len(labels) if labels else 0.0
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = (2 * prec * rec / (prec + rec)) if prec is not None and rec is not None \
        and prec + rec > 0 else None
    return {
        "threshold": round(threshold, 6),
        "accuracy": round(acc, 6),
        "precision": None if prec is None else round(prec, 6),
        "recall": None if rec is None else round(rec, 6),
        "f1": None if f1 is None else round(f1, 6),
        "auc": None if (a := _auc(probs, labels)) is None else round(a, 6),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def adaptive_threshold(train_probs, train_labels) -> float:
    """按训练集正例率选阈值：测试集分数从高到低取到同比例位置。

    小样本类别不平衡时，0.5 固定阈值常常预测不出正例；该阈值只用训练分布，
    不触碰测试标签，避免阈值选择本身泄漏。
    """
    if not train_labels:
        return 0.5
    rate = sum(train_labels) / len(train_labels)
    ordered = sorted(train_probs, reverse=True)
    k = max(1, int(round(rate * len(ordered))))
    return ordered[min(k, len(ordered)) - 1]


def dataset_input_hash(items: list[dict], ds_row, consent_version: int) -> str:
    """数据集版本输入哈希的唯一实现，构建与评估校验共用，防止两处漂移。

    只哈希业务内容（样本版本/标签/状态/特征）与窗口、授权、代码版本；
    tombstoned 是处置标记而非业务输入，不参与哈希（撤回后评估仍可复现校验，
    撤回数据由墓碑跳过逻辑排除）。
    """
    canonical = sorted((i["sample_id"], i["sample_version"], i["label"],
                        i["label_status"], i["features_json"]) for i in items)
    return stable_hash({
        "items": canonical, "strategy": ds_row["strategy"],
        "start_ts": ds_row["start_ts"], "end_ts": ds_row["end_ts"],
        "horizon_days": ds_row["horizon_days"],
        "consent_version": consent_version,
        "feature_version": FEATURE_VERSION, "code_version": CODE_VERSION,
    })


# ---------- 评估执行 ----------
def run_evaluation(conn, dataset_id: str, version: int,
                   job_id: str | None = None) -> dict:
    dv = conn.execute(
        "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?",
        (dataset_id, version)).fetchone()
    if not dv:
        raise ValueError("数据集版本不存在")

    rows = conn.execute(
        "SELECT * FROM dataset_items WHERE dataset_id=? AND version=? "
        "ORDER BY anchor_ts, sample_id", (dataset_id, version)).fetchall()

    # 1) 输入哈希复检：固定输入，防止业务内容被偷换。
    #    tombstoned 不参与哈希（见 dataset_input_hash 说明）。
    ds = conn.execute("SELECT * FROM datasets WHERE dataset_id=?",
                      (dataset_id,)).fetchone()
    recomputed = dataset_input_hash([dict(r) for r in rows], dict(ds),
                                    dv["built_consent_version"])
    if recomputed != dv["input_hash"]:
        raise RuntimeError(
            f"输入哈希不一致：登记 {dv['input_hash']}，实算 {recomputed}，拒绝评估")

    # 2) 墓碑样本跳过（撤回数据不复活），仅用成熟标签做监督评估
    usable, skipped_tomb = [], 0
    for r in rows:
        if r["tombstoned"]:
            skipped_tomb += 1
            continue
        if r["label_status"] != "mature":
            continue  # rolling 窗里的 pending 样本不参与指标计算
        usable.append(r)

    if len(usable) < 4 or len({r["label"] for r in usable}) < 2:
        raise RuntimeError("可用成熟样本不足或只有单一类别，无法评估")

    # 3) 时间序切分 80/20（绝不随机打乱，避免未来样本进训练集）
    cut = int(len(usable) * 0.8)
    train, test = usable[:cut], usable[cut:]
    if len({r["label"] for r in train}) < 2 or len({r["label"] for r in test}) < 2:
        cut = max(2, len(usable) // 2)
        train, test = usable[:cut], usable[cut:]

    def matrix(rs):
        X = [[json.loads(r["features_json"])[k] for k in FEATURE_KEYS] for r in rs]
        y = [r["label"] for r in rs]
        return X, y

    X_train, y_train = matrix(train)
    X_test, y_test = matrix(test)
    model = LogisticRegression(len(FEATURE_KEYS)).fit(X_train, y_train)
    train_probs = model.predict_proba(X_train)
    probs = model.predict_proba(X_test)
    thr_adapt = adaptive_threshold(train_probs, y_train)
    metrics = {
        "at_0.5": compute_metrics(probs, y_test, 0.5),
        "at_adaptive": compute_metrics(probs, y_test, thr_adapt),
        "adaptive_threshold": round(thr_adapt, 6),
        "train_positive_rate": round(sum(y_train) / len(y_train), 6),
    }
    # AUC 与阈值无关，提升到顶层便于页面比较
    metrics["auc"] = metrics["at_adaptive"]["auc"]

    eval_id = _new_id("eval")
    consent_pin = f"pinned@v{dv['built_consent_version']}"
    conn.execute(
        """INSERT INTO evaluations
           (eval_id, dataset_id, dataset_version, input_hash, consent_version,
            code_version, feature_version, metrics_json, n_train, n_test,
            skipped_tombstones, job_id, created_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eval_id, dataset_id, version, dv["input_hash"], consent_pin,
         CODE_VERSION, FEATURE_VERSION, json.dumps(metrics), len(train),
         len(test), skipped_tomb, job_id, now_ts()),
    )
    conn.commit()
    return {"eval_id": eval_id, "metrics": metrics,
            "n_train": len(train), "n_test": len(test),
            "skipped_tombstones": skipped_tomb,
            "input_hash": dv["input_hash"],
            "consent_version": consent_pin,
            "code_version": CODE_VERSION, "feature_version": FEATURE_VERSION,
            "weights": dict(zip(FEATURE_KEYS, [round(w, 6) for w in model.w])),
            "intercept": round(model.b, 6)}
