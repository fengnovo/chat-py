#!/usr/bin/env bash
# 合成探针包装脚本（Task 11）。
#
# 用法：
#   run-synthetic.sh             # 常规探针（live/ready/auth_reject）
#   run-synthetic.sh --canary    # canary 探针（额外 chat run + knowledge retrieval）
#   run-synthetic.sh --dry-run   # 只打印解析后的目标与检查项，不发任何请求
#
# 退出码：探针检查有失败返回 1；指标推送失败不影响退出码（fail-open）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$SCRIPT_DIR/health-probe.ts"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# systemd EnvironmentFile 不会自动导出到本脚本（EnvironmentFile 仅供 service 进程），
# 这里显式 source，仅取探针相关变量；文件缺失不报错（用默认值）。
if [ -f /opt/chat/.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /opt/chat/.env 2>/dev/null || true
  set +a
elif [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env" 2>/dev/null || true
  set +a
fi

export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:4318}"
export PROBE_BASE_URL="${PROBE_BASE_URL:-http://127.0.0.1:${API_PORT:-8000}}"
# canary 凭据按需配置：PROBE_API_TOKEN / PROBE_KB_ID（专用合成用户，最小权限）

# 运行器优先级：仓库 tsx（dev/CI）-> Node 原生 type stripping（Node 22.6+/23+/26）
if [ -x "$REPO_ROOT/node_modules/.bin/tsx" ]; then
  exec "$REPO_ROOT/node_modules/.bin/tsx" "$PROBE" "$@"
fi

NODE_BIN="${NODE_BIN:-node}"
if "$NODE_BIN" --experimental-strip-types -e "process.exit(0)" >/dev/null 2>&1; then
  exec "$NODE_BIN" --experimental-strip-types "$PROBE" "$@"
fi

# Node 23.6+ / 24+ 默认启用 type stripping，无需 flag
exec "$NODE_BIN" "$PROBE" "$@"
