#!/bin/sh
set -eu

# 无头服务器入口：与 entrypoint.sh 的唯一区别是不套 xvfb-run。
# 这个镜像不装浏览器，没有需要虚拟显示的任务。

APP_DIR="/app"
RUNTIME_DIR="${APP_RUNTIME_DIR:-/runtime}"

mkdir -p "${RUNTIME_DIR}" "${RUNTIME_DIR}/logs"
touch "${RUNTIME_DIR}/account_manager.db"

ln -sfn "${RUNTIME_DIR}/account_manager.db" "${APP_DIR}/account_manager.db"

if ! command -v node >/dev/null 2>&1; then
  echo "[entrypoint] 警告: 找不到 node，ChatGPT 的 Sentinel PoW 无法求解，注册会收不到验证码" >&2
fi

echo "[entrypoint] Starting backend (headless, solver disabled)"
exec python main.py
