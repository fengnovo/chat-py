#!/usr/bin/env bash
# === 关闭开发环境（web / api / worker / knowledge-service）===
# infra 容器默认保留运行；加 --infra 一并停止 docker 基础设施。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 从 .env 读端口（与 dev-start.sh 保持一致）
PORT=3021; API_PORT=8003; KNOWLEDGE_SERVICE_PORT=8091
if [ -f "$REPO_ROOT/.env" ]; then
  set -a; source "$REPO_ROOT/.env"; set +a
  PORT="${PORT:-3021}"; API_PORT="${API_PORT:-8003}"; KNOWLEDGE_SERVICE_PORT="${KNOWLEDGE_SERVICE_PORT:-8091}"
fi

echo "[dev-stop] 优雅终止端口 $PORT / $API_PORT / $KNOWLEDGE_SERVICE_PORT ..."
for port in "$PORT" "$API_PORT" "$KNOWLEDGE_SERVICE_PORT"; do
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  [ -n "$pids" ] && echo "$pids" | xargs kill -TERM 2>/dev/null || true
done

# 本仓库的 Python 服务（worker 不监听端口，必须按进程清理）
for pid in $(pgrep -f 'python[0-9.]* -m (worker\.worker|knowledge_service\.main|api\.server)' 2>/dev/null || true); do
  cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | sed 's/^n//' | head -1)
  case "$cwd" in
    "$REPO_ROOT"/*) kill -TERM "$pid" 2>/dev/null || true ;;
  esac
done

sleep 2

# 强制兜底
for port in "$PORT" "$API_PORT" "$KNOWLEDGE_SERVICE_PORT"; do
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  [ -n "$pids" ] && echo "$pids" | xargs kill -KILL 2>/dev/null || true
done
for pid in $(pgrep -f 'python[0-9.]* -m (worker\.worker|knowledge_service\.main|api\.server)' 2>/dev/null || true); do
  cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | sed 's/^n//' | head -1)
  case "$cwd" in
    "$REPO_ROOT"/*) kill -KILL "$pid" 2>/dev/null || true ;;
  esac
done

# 校验
echo "──────── 端口 $PORT / $API_PORT / $KNOWLEDGE_SERVICE_PORT ────────"
if lsof -nP -iTCP:"$PORT" -iTCP:"$API_PORT" -iTCP:"$KNOWLEDGE_SERVICE_PORT" -sTCP:LISTEN 2>/dev/null | grep -E ":(\b$PORT\b|\b$API_PORT\b|\b$KNOWLEDGE_SERVICE_PORT\b)"; then
  echo "⚠️  仍有端口占用"
else
  echo "✅ 端口已释放"
fi

echo "──────── 本仓库 Python 服务 ────────"
leak=0
for pid in $(pgrep -f 'python[0-9.]* -m (worker\.worker|knowledge_service\.main|api\.server)' 2>/dev/null || true); do
  cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | sed 's/^n//' | head -1)
  case "$cwd" in
    "$REPO_ROOT"/*) leak=1; echo "⚠️  残留 pid=$pid cwd=$cwd" ;;
  esac
done
[ "$leak" -eq 0 ] && echo "✅ 无残留进程"

# 可选：一并停止 infra
if [ "${1:-}" = "--infra" ]; then
  echo "──────── 停止 infra 容器 ────────"
  docker compose -f "$REPO_ROOT/infra/compose.yaml" down
  echo "✅ infra 已停止"
fi
