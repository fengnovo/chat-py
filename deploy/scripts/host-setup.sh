#!/usr/bin/env bash
# 宿主机初始化：Docker / Node 22 / pnpm / Nginx / 运行用户 / 数据目录 / Docker 日志上限。
# 幂等，可重复执行。需要 root。
#
#   sudo bash deploy/scripts/host-setup.sh
#
# 支持 Debian 12 / Ubuntu 22.04+（apt）。CentOS 系请按 README 手动装 Docker。
set -euo pipefail

APP_USER="${APP_USER:-chatapp}"
APP_DIR="${APP_DIR:-/opt/chat}"
NODE_MAJOR="${NODE_MAJOR:-22}"
# 可选：阿里云容器镜像加速地址，形如 https://xxxx.mirror.aliyuncs.com
DOCKER_MIRROR="${DOCKER_MIRROR:-}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行（sudo bash $0）" >&2
  exit 1
fi

# ---------------------------------------------------------------- 1. 系统包
log "安装基础包"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg git jq rsync nginx certbot python3-certbot-nginx

# ---------------------------------------------------------------- 2. Docker
if ! command -v docker >/dev/null 2>&1; then
  log "安装 Docker（官方源）"
  install -m 0755 -d /etc/apt/keyrings
  if . /etc/os-release && [ "${ID}" = "ubuntu" ]; then
    DOCKER_DISTRO=ubuntu
  else
    DOCKER_DISTRO=debian
  fi
  curl -fsSL "https://download.docker.com/linux/${DOCKER_DISTRO}/gpg" \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DOCKER_DISTRO} $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  log "Docker 已安装：$(docker --version)"
fi

# 容器日志上限：沙箱容器每次 execute 都会新建，默认 json-file 会吃满磁盘
log "配置 Docker 守护进程（日志上限 + 可选镜像加速）"
mkdir -p /etc/docker
# 保留已有配置，只覆盖我们管理的键（jq 在第一步已安装）
if [ -f /etc/docker/daemon.json ] && jq -e . /etc/docker/daemon.json >/dev/null 2>&1; then
  current="$(cat /etc/docker/daemon.json)"
else
  current='{}'
fi
printf '%s' "$current" | jq --arg mirror "$DOCKER_MIRROR" '
  .["log-driver"] = "json-file"
  | .["log-opts"] = { "max-size": "10m", "max-file": "3" }
  | (if $mirror != "" then .["registry-mirrors"] = [$mirror] else . end)
' > /etc/docker/daemon.json
cat /etc/docker/daemon.json
systemctl enable --now docker

# ---------------------------------------------------------------- 3. Node + pnpm
if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt "$NODE_MAJOR" ]; then
  log "安装 Node.js ${NODE_MAJOR}"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y nodejs
else
  log "Node 已安装：$(node --version)"
fi
log "安装 pnpm"
npm i -g pnpm@11 >/dev/null
echo "node $(node --version) / pnpm $(pnpm --version)"

# ---------------------------------------------------------------- 4. 运行用户
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  log "创建运行用户 ${APP_USER}"
  useradd --create-home --shell /bin/bash "$APP_USER"
fi
usermod -aG docker "$APP_USER"   # worker 需要 docker.sock

# ---------------------------------------------------------------- 5. 目录
log "创建目录 ${APP_DIR}"
mkdir -p "${APP_DIR}/data/workspaces" "${APP_DIR}/data/sandboxes"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
# 沙箱容器以 65532:65532 运行，需要可写；会话目录由 worker 自己 chmod 0777
chmod 0755 "${APP_DIR}/data"

# ---------------------------------------------------------------- 6. 可选 swap
SWAP_GB="${SWAP_GB:-0}"
if [ "$SWAP_GB" -gt 0 ] && [ ! -f /swapfile ]; then
  log "创建 ${SWAP_GB}G swap"
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "完成。下一步见 deploy/README.md 第 3 节"
