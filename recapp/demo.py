"""命令行入口：初始化演示数据库并启动本地网页。

用法：
  python -m recapp.demo --db ./data/recapp.db          # 初始化并启动
  python -m recapp.demo --db ./data/recapp.db --reset  # 删除旧库重建
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import MATURITY_DAYS_DEFAULT
from .db import connect, init_db
from .jobs import JobWorker
from .seed import seed_demo
from .webui import run_server


def init_demo(db_path: str, reset: bool = False, with_worker: bool = True):
    p = Path(db_path)
    if reset and p.exists():
        p.unlink()
    for suffix in ("-wal", "-shm"):
        sp = Path(str(p) + suffix)
        if reset and sp.exists():
            sp.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(p)
    init_db(conn)
    info = seed_demo(conn, horizon_days=MATURITY_DAYS_DEFAULT)
    conn.close()

    worker = None
    if with_worker:
        worker = JobWorker(lambda: connect(p))
        worker.start()
    print("演示数据已生成:")
    for k, v in info.items():
        print(f"  - {k}: {v}")
    print("提示：")
    print("  1. 先在“任务”页入队标签回补，观察迟来退款样本从正例翻为负例")
    print("  2. 在“切窗口”页创建冻结/滚动窗口，比较样本数（新鲜度 vs 完整性）")
    print("  3. 对 u_withdraw 撤回许可，再到审计页查看完整处置链并可恢复")
    return p, worker


def main(argv=None):
    ap = argparse.ArgumentParser(description="推荐反馈样本与评估演示应用")
    ap.add_argument("--db", default="./data/recapp.db")
    ap.add_argument("--reset", action="store_true", help="删除旧库后重建")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    _, worker = init_demo(args.db, args.reset)
    try:
        run_server(args.db, args.host, args.port)
    finally:
        if worker:
            worker.stop()


if __name__ == "__main__":
    sys.exit(main())
