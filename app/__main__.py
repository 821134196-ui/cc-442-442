"""python -m app [--reset] [--host] [--port]

一条本地命令：建库 -> 载入堵塞/退回/已完成样例 -> 启动平台。
"""
import argparse

from . import config
from .seed import seed
from .server import run


def main() -> None:
    parser = argparse.ArgumentParser(description="社区雨水口清掏管理平台")
    parser.add_argument("--reset", action="store_true", help="删除旧库并重建样例")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    config.ensure_dirs()
    seed(reset=args.reset)
    print(f"数据库：{config.DB_PATH}    附件目录：{config.UPLOAD_DIR}")
    run(args.host, args.port)


if __name__ == "__main__":
    main()
