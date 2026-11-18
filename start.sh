#!/bin/bash
# 启动 ds-trim-gateway（Flask 服务 + 可选 Gradio UI）
# 用法:
#   ./start.sh              # 只启动网关服务 (8001)
#   ./start.sh --ui         # 网关 + Gradio UI (7860)
set -e
cd "$(dirname "$0")"

PY=.venv/bin/python
[ -x "$PY" ] || { echo "未找到 venv，先运行: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }

if [ "$1" = "--ui" ]; then
  echo "启动 Gradio UI (http://127.0.0.1:7860) 和网关 (http://127.0.0.1:8001)..."
  "$PY" ui.py --port 7860 &
  UI_PID=$!
  "$PY" server.py --port 8001
  kill $UI_PID 2>/dev/null || true
else
  echo "启动网关 http://127.0.0.1:8001 (加 --ui 可同时开配置界面)"
  exec "$PY" server.py --port 8001
fi
