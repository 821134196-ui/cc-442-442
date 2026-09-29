"""python3 -m app [--no-seed] [--port 8000] [--host 127.0.0.1]
一条本地启动命令：自动载入堵塞、退回与已完成样例后启动服务。"""
from __future__ import annotations

import argparse
from pathlib import Path

from .seed import seed
from .server import run


def main() -> None:
    ap = argparse.ArgumentParser(description="社区雨水口清掏管理平台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/platform.db")
    ap.add_argument("--uploads", default="uploads")
    ap.add_argument("--no-seed", action="store_true", help="不重置/载入样例数据")
    args = ap.parse_args()

    if not args.no_seed:
        print("载入本地样例数据（堵塞 / 验收退回 / 已完成）……")
        seed(args.db, args.uploads)
        print(f"样例就绪：{Path(args.db).resolve()}")
    run(host=args.host, port=args.port, db_path=args.db, upload_dir=args.uploads)


if __name__ == "__main__":
    main()
