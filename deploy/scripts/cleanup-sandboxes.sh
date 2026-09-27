#!/usr/bin/env bash
# 清理过期的沙箱会话目录。
#
# 每个会话在 DOCKER_SANDBOX_SESSIONS_ROOT 下有一个目录，代码里没有清理策略，
# 不清理会一直涨到磁盘满。建议 cron 每天跑一次：
#
#   sudo crontab -e
#   30 4 * * * /opt/chat/deploy/scripts/cleanup-sandboxes.sh >> /var/log/chat-cleanup.log 2>&1
set -euo pipefail

SESSIONS_ROOT="${DOCKER_SANDBOX_SESSIONS_ROOT:-/opt/chat/data/sandboxes}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DRY_RUN="${DRY_RUN:-0}"

if [ ! -d "$SESSIONS_ROOT" ]; then
  echo "目录不存在：$SESSIONS_ROOT"
  exit 0
fi

echo "[$(date -Is)] 清理 $SESSIONS_ROOT 中超过 ${RETENTION_DAYS} 天的会话目录"

# 超过保留期的会话目录
mapfile -t stale < <(find "$SESSIONS_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}")

if [ "${#stale[@]}" -eq 0 ]; then
  echo "无需清理"
else
  for dir in "${stale[@]}"; do
    if [ "$DRY_RUN" = "1" ]; then
      echo "[dry-run] 将删除 $dir"
      continue
    fi
    rm -rf -- "$dir"
    echo "已删除 $dir"
  done
fi

# 清理可能残留的沙箱容器（worker 被杀时可能留下 --rm 之前中断的容器）
if command -v docker >/dev/null 2>&1; then
  orphans=$(docker ps -aq --filter "name=docker-" --filter "status=exited" | wc -l | tr -d ' ')
  if [ "$orphans" -gt 0 ] && [ "$DRY_RUN" != "1" ]; then
    docker ps -aq --filter "name=docker-" --filter "status=exited" | xargs -r docker rm >/dev/null
    echo "清理退出容器 $orphans 个"
  fi
fi

echo "[$(date -Is)] 磁盘：$(df -h "$SESSIONS_ROOT" | tail -1)"
