#!/usr/bin/env bash
# === 开发环境一键启动（Python 后端版）===
# 启动顺序：清理残留 → infra(docker) → db 迁移 → api / worker / knowledge-service / web
# Ctrl+C 停止全部服务；日志输出到 logs/{api,worker,knowledge,web}.log
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

# ── 1. 加载 .env ───────────────────────────────────────────────
if [ ! -f .env ]; then
  echo "[dev] .env 不存在，请先执行: cp .env.example .env 并填写配置"
  exit 1
fi
set -a; source .env; set +a

PORT="${PORT:-3021}"
API_PORT="${API_PORT:-8003}"
KNOWLEDGE_SERVICE_PORT="${KNOWLEDGE_SERVICE_PORT:-8091}"

# ── 2. 清理端口与残留进程（防多 worker 抢队列 / EADDRINUSE）─────
echo "[dev] 清理端口 $PORT / $API_PORT / $KNOWLEDGE_SERVICE_PORT ..."
for port in "$PORT" "$API_PORT" "$KNOWLEDGE_SERVICE_PORT"; do
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  [ -n "$pids" ] && echo "$pids" | xargs kill -TERM 2>/dev/null || true
done
# 本仓库的残留 Python 服务（worker 不监听端口，端口清理管不到它）
for pid in $(pgrep -f 'python[0-9.]* -m (worker\.worker|knowledge_service\.main|api\.server)' 2>/dev/null || true); do
  cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | sed 's/^n//' | head -1)
  case "$cwd" in
    "$REPO_ROOT"/*) kill -TERM "$pid" 2>/dev/null || true ;;
  esac
done
sleep 1
# 强制兜底
for port in "$PORT" "$API_PORT" "$KNOWLEDGE_SERVICE_PORT"; do
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  [ -n "$pids" ] && echo "$pids" | xargs kill -KILL 2>/dev/null || true
done

# ── 3. 依赖检查（首次运行自动安装）──────────────────────────────
if [ ! -d node_modules ]; then
  echo "[dev] 首次运行：pnpm install ..."
  pnpm install
fi
if [ ! -d python/.venv ]; then
  echo "[dev] 首次运行：uv sync ..."
  (cd python && uv sync --all-packages)
fi

# ── 4. 启动基础设施 ────────────────────────────────────────────
echo "[dev] 启动 infra (postgres / redis / qdrant / minio) ..."
docker compose -f infra/compose.yaml up -d --wait

# ── 5. 数据库迁移 ──────────────────────────────────────────────
# compose 只初始化 POSTGRES_DB=agent；本项目独立使用 agent_py，缺失时自动创建
DB_NAME="${DATABASE_URL##*/}"
echo "[dev] 确保数据库 $DB_NAME 存在 ..."
docker compose -f infra/compose.yaml exec -T postgres \
  psql -U agent -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" \
  | grep -q 1 \
  || docker compose -f infra/compose.yaml exec -T postgres \
       psql -U agent -d postgres -c "CREATE DATABASE $DB_NAME OWNER agent"

echo "[dev] 数据库迁移 ..."
(cd python && uv run --no-sync python -c "import asyncio; from db.migrate import main; asyncio.run(main())")

# ── 6. 启动服务 ────────────────────────────────────────────────
PIDS=()

start_py() {
  local name="$1" module="$2"
  (cd python && exec uv run --no-sync python -m "$module") > "$LOG_DIR/$name.log" 2>&1 &
  PIDS+=($!)
  echo "[dev] $name 启动中 pid=$! log=logs/$name.log"
}

start_py api api.server
start_py worker worker.worker
start_py knowledge knowledge_service.main

(pnpm --filter web dev) > "$LOG_DIR/web.log" 2>&1 &
PIDS+=($!)
echo "[dev] web 启动中 pid=$! log=logs/web.log"

# ── 7. 等待端口就绪 ────────────────────────────────────────────
wait_port() {
  local port="$1" name="$2" tries=60
  while [ "$tries" -gt 0 ]; do
    if lsof -ti tcp:"$port" -sTCP:LISTEN > /dev/null 2>&1; then
      echo "[dev] ✅ $name 就绪 (port $port)"
      return 0
    fi
    sleep 1; tries=$((tries - 1))
  done
  echo "[dev] ⚠️  $name 等待超时 (port $port)，请查看 logs/$name.log"
}

wait_port "$API_PORT" api
wait_port "$KNOWLEDGE_SERVICE_PORT" knowledge
wait_port "$PORT" web

# ── 8. 聚合日志 + 信号处理 ─────────────────────────────────────
cleanup() {
  echo ""
  echo "[dev] 停止所有服务 ..."
  kill "${PIDS[@]}" 2>/dev/null || true
  wait "${PIDS[@]}" 2>/dev/null || true
  echo "[dev] 已全部停止（infra 容器仍在运行，如需停止: pnpm infra:down）"
  exit 0
}
trap cleanup INT TERM

echo ""
echo "[dev] 全部就绪：web=http://127.0.0.1:$PORT api=http://127.0.0.1:$API_PORT knowledge=http://127.0.0.1:$KNOWLEDGE_SERVICE_PORT"
echo "[dev] 日志聚合如下，Ctrl+C 停止全部服务"
echo ""

tail -f "$LOG_DIR"/api.log "$LOG_DIR"/worker.log "$LOG_DIR"/knowledge.log "$LOG_DIR"/web.log &
PIDS+=($!)

wait
