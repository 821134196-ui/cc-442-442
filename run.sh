#!/usr/bin/env bash
# 一条本地启动命令：自动建库、载入堵塞/退回/已完成样例并启动平台。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m app --reset "$@"
