#!/usr/bin/env bash
# 可观测性 fail-open 故障注入演练（Task 12）。
#
# 目的：验证「遥测链路/基础设施故障时业务进程不受影响」。所有故障都自动恢复，
# Ctrl-C 也会走恢复逻辑。仅面向本地/单机演练环境，禁止对生产执行。
#
# 用法：
#   bash scripts/fault-injection-observability.sh --list
#   bash scripts/fault-injection-observability.sh --case 1
#   bash scripts/fault-injection-observability.sh --all
#
# 可选环境变量：
#   API_BASE             默认 http://127.0.0.1:${API_PORT:-8002}
#   DWELL_SECS           故障持续观察窗口，默认 20
#   APP_SMOKE_CMD        故障期间额外执行业务路径的命令（返回 0 视为业务未受影响）
#   OBS_PROJECT_PREFIX   观测栈容器前缀，默认 chat-observability
#   POSTGRES_CONTAINER / REDIS_CONTAINER / QDRANT_CONTAINER
#
# 通过标准（每个 case）：
#   1) 故障注入期间 GET /health/live 始终 200（进程不崩溃、不 hang）；
#   2) APP_SMOKE_CMD（如配置）退出码 0；
#   3) 恢复后被注入组件重新 healthy。

set -uo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:${API_PORT:-8002}}"
DWELL_SECS="${DWELL_SECS:-20}"
OBS="${OBS_PROJECT_PREFIX:-chat-observability}"
PG_CTR="${POSTGRES_CONTAINER:-chat-infra-postgres-1}"
REDIS_CTR="${REDIS_CONTAINER:-chat-infra-redis-1}"
QDRANT_CTR="${QDRANT_CONTAINER:-chat-agent-qdrant-1}"
MOCK_LANGFUSE_PORT="${MOCK_LANGFUSE_PORT:-18099}"
MOCK_MODEL_PORT="${MOCK_MODEL_PORT:-18098}"
MOCK_PIDS=()
TMP_DIR="$(mktemp -d)"

PASS=0
FAIL=0

log()  { printf '\033[36m[case]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[PASS]\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; FAIL=$((FAIL+1)); }

trap recover_all EXIT

docker_healthy() { # container -> 0/1
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null)" = healthy ]
}
docker_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]
}
wait_healthy() {
  local ctr=$1 timeout=${2:-60} i
  for ((i=0; i<timeout; i++)); do docker_healthy "$ctr" && return 0; sleep 1; done
  return 1
}
wait_running() {
  local ctr=$1 timeout=${2:-30} i
  for ((i=0; i<timeout; i++)); do docker_running "$ctr" && return 0; sleep 1; done
  return 1
}

business_alive() { # live 探针 + 可选业务冒烟
  curl -fsS -m 5 "$API_BASE/health/live" >/dev/null || return 1
  if [ -n "${APP_SMOKE_CMD:-}" ]; then
    bash -c "$APP_SMOKE_CMD" || return 1
  fi
  return 0
}

# 在故障窗口内每 2s 探一次业务，全程存活才算 PASS
observe_window() {
  local rounds=$(( DWELL_SECS / 2 )) i
  for ((i=0; i<rounds; i++)); do
    if business_alive; then
      printf '.'
    else
      echo; bad "业务探针在故障期间失败（$API_BASE/health/live 或 APP_SMOKE_CMD）"
      return 1
    fi
    sleep 2
  done
  echo
  ok "故障窗口 ${DWELL_SECS}s 内业务存活"
}

start_mock() { # port mode(langfuse-500|model-429|model-timeout)
  local port=$1 mode=$2
  cat > "$TMP_DIR/mock-$port.mjs" <<'EOF'
import { createServer } from 'node:http';
const mode = process.argv[3];
const delay = mode === 'model-timeout' ? 60_000 : 0;
const status = mode === 'langfuse-500' ? 500 : 429;
createServer(async (_req, res) => {
  if (delay) await new Promise((r) => setTimeout(r, delay));
  res.statusCode = status;
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify({ error: 'injected fault', mode }));
}).listen(Number(process.argv[2]), '127.0.0.1');
console.log(`mock ${mode} on ${process.argv[2]}`);
EOF
  node "$TMP_DIR/mock-$port.mjs" "$port" "$mode" >/dev/null 2>&1 &
  MOCK_PIDS+=($!)
  disown "${MOCK_PIDS[-1]}" 2>/dev/null || true
  sleep 1
  curl -fsS "http://127.0.0.1:$port/" >/dev/null 2>&1 || true
  log "mock 已启动: 127.0.0.1:$port ($mode)"
}

recover_all() {
  local rc=$?
  set +e
  # 各 case 的恢复是幂等的，统一兜底
  docker start "$OBS-alloy-1" >/dev/null 2>&1
  docker start "$OBS-prometheus-1" >/dev/null 2>&1
  docker start "$OBS-loki-1" >/dev/null 2>&1
  docker start "$OBS-tempo-1" >/dev/null 2>&1
  docker network connect "$OBS"_default "$OBS-tempo-1" >/dev/null 2>&1
  docker network connect "$OBS"_default "$OBS-loki-1" >/dev/null 2>&1
  docker unpause "$REDIS_CTR" >/dev/null 2>&1
  docker start "$PG_CTR" >/dev/null 2>&1
  docker start "$QDRANT_CTR" >/dev/null 2>&1
  for pid in "${MOCK_PIDS[@]:-}"; do kill "$pid" 2>/dev/null; done
  if [ $rc -eq 0 ] && [ "${1:-}" != quiet ]; then
    echo; echo "==== 结果: PASS=$PASS FAIL=$FAIL ===="
    [ "$FAIL" -eq 0 ]
  fi
  exit $rc
}

# --- 9 类故障 ----------------------------------------------------------------
case_1_alloy_down() {
  log '1/9 Alloy 停止（OTLP 端点整体消失）'
  docker stop "$OBS-alloy-1" >/dev/null
  observe_window
  docker start "$OBS-alloy-1" >/dev/null
  wait_healthy "$OBS-alloy-1" 90 && ok 'Alloy 恢复 healthy' || bad 'Alloy 未恢复'
}
case_2_tempo_unreachable() {
  log '2/9 Tempo 网络不可达（trace 导出失败）'
  docker network disconnect "$OBS"_default "$OBS-tempo-1"
  observe_window
  docker network connect "$OBS"_default "$OBS-tempo-1"
  wait_healthy "$OBS-tempo-1" 60 && ok 'Tempo 恢复 healthy' || bad 'Tempo 未恢复'
}
case_3_loki_unreachable() {
  log '3/9 Loki 不可达（日志导出失败）'
  docker network disconnect "$OBS"_default "$OBS-loki-1"
  observe_window
  docker network connect "$OBS"_default "$OBS-loki-1"
  wait_healthy "$OBS-loki-1" 60 && ok 'Loki 恢复 healthy' || bad 'Loki 未恢复'
}
case_4_prometheus_down() {
  log '4/9 Prometheus 不可达（指标 remote write + 规则评估失败）'
  docker stop "$OBS-prometheus-1" >/dev/null
  observe_window
  docker start "$OBS-prometheus-1" >/dev/null
  wait_healthy "$OBS-prometheus-1" 60 && ok 'Prometheus 恢复 healthy' || bad 'Prometheus 未恢复'
}
case_5_langfuse_5xx() {
  log '5/9 Langfuse 持续 5xx（GenAI 专项观测投递失败）'
  start_mock "$MOCK_LANGFUSE_PORT" langfuse-500
  cat <<HINT
    请在另一终端用故障端点重启/运行 Worker 冒烟（脚本不会替你重启业务）：
      LANGFUSE_ENABLED=true LANGFUSE_PUBLIC_KEY=pk-lf-local LANGFUSE_SECRET_KEY=sk-lf-local \\
      LANGFUSE_BASE_URL=http://127.0.0.1:$MOCK_LANGFUSE_PORT \\
      pnpm --filter @repo/agent-worker start
    预期：任务正常完成；Langfuse 仅 warnOnce，shutdown/flush 5s 上限内返回。
HINT
  observe_window
}
case_6_redis_stall() {
  log '6/9 Redis 停顿（pause 模拟不可达；纯延迟需 tc+NET_ADMIN，见脚本注释）'
  # 延迟注入参考（需宿主机 root 与 tc）：
  #   docker exec --privileged -u root "$REDIS_CTR" sh -c \
  #     'tc qdisc add dev eth0 root netem delay 2000ms 2>/dev/null || true'
  docker pause "$REDIS_CTR" >/dev/null
  sleep 5
  business_alive && ok 'Redis 停顿期间 API live 正常（任务侧应有排队/重试）' || bad 'API live 失败'
  docker unpause "$REDIS_CTR" >/dev/null
  wait_running "$REDIS_CTR" && ok 'Redis 恢复' || bad 'Redis 未恢复'
}
case_7_postgres_refusal() {
  log '7/9 Postgres 拒绝连接（ready 应变 503，live 必须保持 200）'
  docker stop "$PG_CTR" >/dev/null
  sleep 8
  local live ready
  live=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$API_BASE/health/live")
  ready=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$API_BASE/health/ready")
  [ "$live" = 200 ] && ok "live=$live" || bad "live=$live（期望 200）"
  if [ "$ready" = 503 ] || [ "$ready" = 500 ]; then ok "ready=$ready"; else bad "ready=$ready（期望 5xx）"; fi
  docker start "$PG_CTR" >/dev/null
  wait_healthy "$PG_CTR" 60 || wait_running "$PG_CTR" 60
  ok 'Postgres 已恢复（ready 应在数轮探测内回到 200）'
}
case_8_model_429_timeout() {
  log '8/9 模型提供方 429 / 超时（重试/fallback/熔断路径）'
  start_mock "$MOCK_MODEL_PORT" model-429
  cat <<HINT
    将 OPENAI_BASE_URL=http://127.0.0.1:$MOCK_MODEL_PORT 指向冒烟进程后发起一次会话。
    预期：业务请求优雅失败/降级，不拖垮 Worker；
    Prometheus 可见 model_calls_total{outcome="failure"}、model_retries/fallbacks/circuit_total 增长。
    超时变体：MOCK_MODEL_PORT 换端口并以 model-timeout 模式重启本 case 的 mock（60s 延迟）。
HINT
  observe_window
}
case_9_qdrant_down() {
  log '9/9 Qdrant 不可达（检索/索引路径，聊天主链路应可用）'
  docker stop "$QDRANT_CTR" >/dev/null
  observe_window
  docker start "$QDRANT_CTR" >/dev/null
  wait_running "$QDRANT_CTR" && ok 'Qdrant 恢复' || bad 'Qdrant 未恢复'
}

CASES=(
  case_1_alloy_down case_2_tempo_unreachable case_3_loki_unreachable
  case_4_prometheus_down case_5_langfuse_5xx case_6_redis_stall
  case_7_postgres_refusal case_8_model_429_timeout case_9_qdrant_down
)

case "${1:-}" in
  --list)
    grep -oE 'case_[0-9]_[a-z_0-9]+' "$0" | sort -u
    ;;
  --case)
    [ -n "${2:-}" ] || { echo 'usage: --case <1-9>'; exit 2; }
    "${CASES[$(( $2 - 1 ))]}"
    recover_all quiet
    [ "$FAIL" -eq 0 ]
    ;;
  --all)
    for c in "${CASES[@]}"; do "$c"; done
    recover_all quiet
    [ "$FAIL" -eq 0 ]
    ;;
  *)
    cat <<USAGE
usage: $0 --list | --case <1-9> | --all
前提：观测栈（compose.observability.yaml）与 chat-infra 已在本机运行。
USAGE
    exit 2
    ;;
esac
