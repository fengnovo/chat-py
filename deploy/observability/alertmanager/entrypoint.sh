#!/bin/sh
# Alertmanager 不支持配置文件环境变量展开（与 Prometheus 不同），
# 因此入口脚本先把占位符替换为容器环境变量，再启动。
# 未注入 *_WEBHOOK_URL 时 compose 默认指向 RFC 2606 .invalid 域名（永不投递）。
set -eu

TEMPLATE=/etc/alertmanager/alertmanager.yaml
OUT=/tmp/alertmanager.yaml

sed \
  -e "s|\${PAGE_WEBHOOK_URL}|${PAGE_WEBHOOK_URL}|g" \
  -e "s|\${URGENT_WEBHOOK_URL}|${URGENT_WEBHOOK_URL}|g" \
  -e "s|\${TICKET_WEBHOOK_URL}|${TICKET_WEBHOOK_URL}|g" \
  "$TEMPLATE" > "$OUT"

exec /bin/alertmanager \
  --config.file="$OUT" \
  --storage.path=/alertmanager \
  "$@"
