#!/usr/bin/env bash
# 在 /opt/chat 里构建并发布：安装依赖 → 构建 → 构建沙箱镜像 → 安装 systemd unit → 重启服务。
# 使用 chatapp 用户执行（需要能访问 docker.sock 和 sudo systemctl）。
#
#   cd /opt/chat && bash deploy/scripts/app-build.sh
#
# 首次部署、以及每次更新代码后都执行这个脚本。
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/chat}"
APP_USER="${APP_USER:-chatapp}"
SANDBOX_IMAGE="${DOCKER_SANDBOX_IMAGE:-chat-agent-sandbox:latest}"
SKIP_SANDBOX_IMAGE="${SKIP_SANDBOX_IMAGE:-0}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

cd "$REPO_DIR"

if [ ! -f .env ]; then
  echo "缺少 ${REPO_DIR}/.env，先从 deploy/env.production.example 复制并填写" >&2
  exit 1
fi

log "安装依赖（严格按 lockfile）"
export CI=true   # 避免 pnpm 在非 TTY 下中止 node_modules 重建
pnpm install --frozen-lockfile

log "构建全部 workspace"
export TURBO_TELEMETRY_DISABLED=1
pnpm build

if [ "$SKIP_SANDBOX_IMAGE" != "1" ]; then
  if docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then
    log "沙箱镜像已存在，跳过构建（强制重建：SKIP_SANDBOX_IMAGE=0 并先 docker rmi $SANDBOX_IMAGE）"
  else
    log "构建沙箱镜像 $SANDBOX_IMAGE（约 1GB 下载，国内可能较慢）"
    # 国内慢的话把下面这行换成：-f deploy/sandbox/Dockerfile.mirror
    docker build -f infra/sandbox/Dockerfile -t "$SANDBOX_IMAGE" infra/sandbox
  fi
fi

log "安装 systemd unit"
sudo cp deploy/systemd/chat-api.service deploy/systemd/chat-worker.service deploy/systemd/chat-web.service deploy/systemd/chat-knowledge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable chat-api chat-worker chat-web chat-knowledge >/dev/null

log "重启服务"
sudo systemctl restart chat-api
sudo systemctl restart chat-worker
sudo systemctl restart chat-web
sudo systemctl restart chat-knowledge

sleep 3
log "服务状态"
systemctl --no-pager --lines=0 status chat-api chat-worker chat-web chat-knowledge || true

log "自检"
for i in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${API_PORT:-8002}/health/ready" >/dev/null 2>&1; then
    echo "API ready: $(curl -fsS "http://127.0.0.1:${API_PORT:-8002}/health/ready")"
    break
  fi
  [ "$i" = 20 ] && { echo "API 未就绪，检查：journalctl -u chat-api -n 80 --no-pager" >&2; exit 1; }
  sleep 2
done
curl -fsS -o /dev/null -w "Web: HTTP %{http_code}\n" "http://127.0.0.1:${PORT:-3020}/" || true
curl -fsS -o /dev/null -w "Knowledge: HTTP %{http_code}\n" "http://127.0.0.1:${KNOWLEDGE_SERVICE_PORT:-8090}/healthz" || true
