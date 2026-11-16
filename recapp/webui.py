"""本地网页与 JSON API（标准库 ThreadingHTTPServer，无第三方依赖）。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import jobs as jobs_mod
from . import samples as samples_mod
from .db import connect, init_db
from .evaluation import run_evaluation
from .ingestion import (ConsentError, ValidationError, add_event, restore,
                        withdraw)
from .models import parse_iso

STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()  # 串行化写操作，配合 WAL 保证读不阻塞

    def conn(self):
        c = connect(self.db_path)
        init_db(c)
        return c


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RecSampleApp/1.0"

        def log_message(self, fmt, *args):  # 安静日志
            pass

        # ---------- 基础工具 ----------
        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode())

        def _query(self) -> dict:
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        def _handle(self, fn, write: bool = True):
            try:
                with state.lock if write else _Null():
                    conn = state.conn()
                    try:
                        result = fn(conn)
                    finally:
                        conn.close()
                # 处理方法若已自行发送响应（返回 None）则不重复发送
                if result is not None:
                    return self._json(result)
                return None
            except (ValidationError, ConsentError, ValueError) as exc:
                return self._json({"error": str(exc)}, 400)
            except PermissionError as exc:
                return self._json({"error": str(exc)}, 403)
            except FileNotFoundError as exc:
                return self._json({"error": str(exc)}, 404)
            except Exception as exc:  # noqa: BLE001
                return self._json(
                    {"error": f"{type(exc).__name__}: {exc}"}, 500)

        # ---------- 静态资源 ----------
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/" or path == "/index.html":
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return self._static("app.js", "application/javascript; charset=utf-8")
            if path == "/styles.css":
                return self._static("styles.css", "text/css; charset=utf-8")
            return self._route_get(path)

        def _static(self, name: str, ctype: str):
            f = STATIC_DIR / name
            if not f.exists():
                return self.send_error(404)
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---------- GET 路由 ----------
        def _route_get(self, path: str):
            q = self._query()
            if path == "/api/overview":
                return self._handle(self._overview, write=False)
            if path == "/api/events":
                return self._handle(lambda c: self._events(c, q), write=False)
            if path == "/api/samples":
                return self._handle(lambda c: self._samples(c, q), write=False)
            if path == "/api/sample":
                return self._handle(
                    lambda c: samples_mod.sample_lineage(c, q["sample_id"]),
                    write=False)
            if path == "/api/datasets":
                return self._handle(self._datasets, write=False)
            if path == "/api/evaluations":
                return self._handle(self._evaluations, write=False)
            if path == "/api/jobs":
                return self._handle(self._jobs, write=False)
            if path == "/api/consent":
                return self._handle(self._consent, write=False)
            if path == "/api/disposal":
                return self._handle(self._disposal, write=False)
            if path == "/api/caches":
                return self._handle(self._caches, write=False)
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            body = self._body()
            routes = {
                "/api/events": self._post_event,
                "/api/withdraw": self._post_withdraw,
                "/api/restore": self._post_restore,
                "/api/jobs/backfill": self._post_backfill,
                "/api/jobs/rebuild": self._post_rebuild,
                "/api/jobs/build-all": self._post_build_all,
                "/api/windows": self._post_window,
                "/api/jobs/build-window": self._post_build_window,
                "/api/jobs/evaluate": self._post_evaluate,
                "/api/evaluate-direct": self._post_evaluate_direct,
            }
            fn = routes.get(path)
            if not fn:
                return self._json({"error": "not found"}, 404)
            return self._handle(lambda c: fn(c, body))

        # ---------- 各资源处理 ----------
        def _overview(self, conn):
            stats = {
                "events": conn.execute(
                    "SELECT event_type, COUNT(*) n FROM events GROUP BY event_type"
                ).fetchall(),
                "samples": conn.execute(
                    "SELECT label_status, COUNT(*) n FROM samples "
                    "WHERE superseded=0 GROUP BY label_status").fetchall(),
                "withdrawn": conn.execute(
                    "SELECT COUNT(*) n FROM users WHERE status='withdrawn'"
                ).fetchone()["n"],
                "consent_version": conn.execute(
                    "SELECT COALESCE(MAX(version),0) v FROM consent_versions"
                ).fetchone()["v"],
                "datasets": conn.execute("SELECT COUNT(*) n FROM datasets"
                                         ).fetchone()["n"],
                "evaluations": conn.execute(
                    "SELECT COUNT(*) n FROM evaluations").fetchone()["n"],
            }
            return {
                "event_counts": {r["event_type"]: r["n"] for r in stats["events"]},
                "sample_counts": {r["label_status"]: r["n"]
                                  for r in stats["samples"]},
                "withdrawn_users": stats["withdrawn"],
                "consent_version": stats["consent_version"],
                "dataset_count": stats["datasets"],
                "evaluation_count": stats["evaluations"],
            }

        def _events(self, conn, q):
            limit = int(q.get("limit", 100))
            rows = conn.execute(
                "SELECT * FROM events ORDER BY event_ts DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]

        def _samples(self, conn, q):
            limit = int(q.get("limit", 100))
            rows = conn.execute(
                "SELECT * FROM samples WHERE superseded=0 ORDER BY anchor_ts DESC "
                "LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["features"] = json.loads(d.pop("features_json"))
                d["source_event_ids"] = json.loads(d.pop("source_event_ids_json"))
                out.append(d)
            return out

        def _datasets(self, conn):
            ds = conn.execute("SELECT * FROM datasets ORDER BY created_ts").fetchall()
            result = []
            for d in ds:
                versions = conn.execute(
                    "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version",
                    (d["dataset_id"],)).fetchall()
                result.append({**dict(d), "versions": [dict(v) for v in versions]})
            return result

        def _evaluations(self, conn):
            rows = conn.execute(
                "SELECT * FROM evaluations ORDER BY created_ts DESC").fetchall()
            return [{**dict(r), "metrics": json.loads(r["metrics_json"])}
                    for r in rows]

        def _jobs(self, conn):
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_ts DESC LIMIT 50").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                if d.get("result_json"):
                    d["result"] = json.loads(d["result_json"])
                out.append(d)
            return out

        def _consent(self, conn):
            users = conn.execute(
                "SELECT user_id, status, created_ts FROM users ORDER BY user_id"
            ).fetchall()
            versions = conn.execute(
                "SELECT * FROM consent_versions ORDER BY version DESC LIMIT 50"
            ).fetchall()
            return {"users": [dict(u) for u in users],
                    "recent_versions": [dict(v) for v in versions]}

        def _disposal(self, conn):
            logs = conn.execute(
                "SELECT * FROM disposal_log ORDER BY id DESC LIMIT 100").fetchall()
            archives = conn.execute(
                "SELECT id, user_id, entity_type, entity_id, archived_ts, restored, "
                "restored_ts FROM disposal_archive ORDER BY id DESC LIMIT 100"
            ).fetchall()
            return {"logs": [dict(r) for r in logs],
                    "archives": [dict(r) for r in archives]}

        def _caches(self, conn):
            rows = conn.execute(
                "SELECT cache_key, user_ids_json, created_ts, invalidated "
                "FROM cache_entries ORDER BY created_ts DESC").fetchall()
            return [{**dict(r), "user_ids": json.loads(r["user_ids_json"])}
                    for r in rows]

        # ---------- POST ----------
        def _post_event(self, conn, body):
            ts = body["event_ts"] if isinstance(body.get("event_ts"), int) \
                else parse_iso(body["event_ts"])
            res = add_event(
                conn, event_id=body["event_id"],
                impression_id=body["impression_id"], user_id=body["user_id"],
                item_id=body["item_id"], event_type=body["event_type"],
                event_ts=ts, position=body.get("position"),
                payload=body.get("payload"))
            conn.commit()
            return res

        def _post_withdraw(self, conn, body):
            return withdraw(conn, body["user_id"],
                            body.get("reason", "user_request"))

        def _post_restore(self, conn, body):
            return restore(conn, body["user_id"],
                           body.get("reason", "re_granted"))

        def _post_backfill(self, conn, body):
            return jobs_mod.enqueue(
                conn, "backfill",
                {"horizon_days": int(body.get("horizon_days", 14))})

        def _post_rebuild(self, conn, body):
            return jobs_mod.enqueue(
                conn, "rebuild",
                {"horizon_days": int(body.get("horizon_days", 14))})

        def _post_build_all(self, conn, body):
            return jobs_mod.enqueue(
                conn, "build_all",
                {"horizon_days": int(body.get("horizon_days", 14))})

        def _post_window(self, conn, body):
            return samples_mod.create_window(
                conn, body["name"], body["strategy"],
                body["start_ts"], body["end_ts"],
                int(body.get("horizon_days", 14)))

        def _post_build_window(self, conn, body):
            return jobs_mod.enqueue(
                conn, "build_window", {"dataset_id": body["dataset_id"]})

        def _post_evaluate(self, conn, body):
            return jobs_mod.enqueue(
                conn, "evaluate", {"dataset_id": body["dataset_id"],
                                   "version": int(body["version"])})

        def _post_evaluate_direct(self, conn, body):
            # 同步执行，便于演示页面即时拿到结果
            return run_evaluation(conn, body["dataset_id"], int(body["version"]))

    return Handler


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_server(db_path: str, host: str = "127.0.0.1", port: int = 8000):
    state = AppState(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"推荐反馈样本与评估应用: http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
