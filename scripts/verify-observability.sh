#!/usr/bin/env bash
# 可观测性静态校验（Task 12）：不依赖真实凭据，可在开发机直接运行。
#
#   bash scripts/verify-observability.sh
#
# 内容：promtool rules/config、amtool config（按 entrypoint.sh 同逻辑展开占位符）、
# compose config 插值、dashboard JSON 与变量白名单、敏感字段与高基数 label 扫描、
# systemd 单元护栏；若本机装了 alloy，则额外 alloy fmt --check。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROM_IMAGE=prom/prometheus:v2.54.1
AM_IMAGE=prom/alertmanager:v0.27.0
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $* =="; }

# --- 1. Prometheus 录制/告警规则与主配置 -------------------------------------
step "promtool check rules"
docker run --rm -v "$ROOT/deploy/observability:/etc/obs:ro" \
  --entrypoint /bin/sh "$PROM_IMAGE" -c 'promtool check rules /etc/obs/prometheus/rules/*.yaml'

step "promtool check config"
docker run --rm -v "$ROOT/deploy/observability:/etc/obs:ro" \
  --entrypoint promtool "$PROM_IMAGE" check config /etc/obs/prometheus/prometheus.yaml

# --- 2. Alertmanager（占位符必须与 entrypoint.sh 完全一致地展开） -------------
step "amtool check-config"
docker run --rm -v "$ROOT/deploy/observability/alertmanager:/am:ro" \
  --entrypoint /bin/sh "$AM_IMAGE" -c '
    set -eu
    sed -e "s|\${PAGE_WEBHOOK_URL}|http://example.invalid/page|g" \
        -e "s|\${URGENT_WEBHOOK_URL}|http://example.invalid/urgent|g" \
        -e "s|\${TICKET_WEBHOOK_URL}|http://example.invalid/ticket|g" \
      /am/alertmanager.yaml > /tmp/am.yaml
    amtool check-config /tmp/am.yaml
  '

# --- 3. compose 插值 -----------------------------------------------------------
step "docker compose config"
GRAFANA_ADMIN_PASSWORD=dummy docker compose -f deploy/compose.observability.yaml config -q
# infra 的 ${VAR:?} 需要非空插值；用生产示例占位（不连任何真实服务）
docker compose -f deploy/compose.infra.yaml --env-file deploy/env.production.example config -q

# --- 4. dashboard JSON + 低基数变量白名单 --------------------------------------
step "dashboards"
python3 - <<'PY'
import json, glob, sys
bad = []
paths = sorted(glob.glob('deploy/observability/grafana/dashboards/*.json'))
if len(paths) < 5:
    bad.append(f'expected >=5 dashboards, got {len(paths)}')
for path in paths:
    with open(path) as f:
        d = json.load(f)
    for v in d.get('templating', {}).get('list', []):
        if v.get('type') == 'query' and v.get('datasource'):
            if v.get('name') not in {'service', 'route', 'quantile'}:
                bad.append(f'{path}: high-cardinality/unexpected variable {v.get("name")}')
    if d.get('schemaVersion') != 39:
        bad.append(f'{path}: schemaVersion {d.get("schemaVersion")} != 39')
if bad:
    print('\n'.join(bad)); sys.exit(1)
print('dashboards OK:', len(paths))
PY

# --- 5. 敏感字段 / 高基数 label / 单元护栏 --------------------------------------
step "secret & guardrail scan"
if grep -rnE 'sk-[A-Za-z0-9]{20,}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY' \
     deploy/observability deploy/systemd .github/workflows 2>/dev/null; then
  fail 'possible key material in observability deploy assets'
fi
# 明文口令形态只扫描配置/单元/脚本，扫描代码（探针等）会产生伪报
if grep -rnE --include='*.yaml' --include='*.yml' --include='*.conf' \
     --include='*.service' --include='*.timer' --include='*.sh' \
     'password\s*[:=]\s*[^$<" ]{8,}' \
     deploy/observability deploy/systemd .github/workflows 2>/dev/null \
   | grep -vE 'synthetic-|dummy|REPLACE_|changeme|example\.'; then
  fail 'possible plaintext password in observability config assets'
fi
if grep -rnE 'run_id|user_id|tenant_id|session_id|trace_id' \
     deploy/observability/prometheus deploy/observability/prometheus/rules; then
  fail 'high-cardinality dimension in prometheus rules'
fi
for unit in chat-api chat-worker chat-knowledge; do
  grep -q -- '--import /opt/chat/packages/observability/dist/register.js' \
    "deploy/systemd/$unit.service" || fail "$unit missing observability --import"
  grep -q '^TimeoutStopSec=15$' "deploy/systemd/$unit.service" \
    || fail "$unit missing TimeoutStopSec=15"
done
grep -q '^Environment="NODE_OPTIONS=--import /opt/chat/packages/observability/dist/register.js"$' \
  deploy/systemd/chat-web.service || fail 'chat-web missing NODE_OPTIONS --import'

# --- 6. alloy fmt（本机有 alloy 才检查） ---------------------------------------
if command -v alloy >/dev/null 2>&1; then
  step "alloy fmt --check"
  alloy fmt --check deploy/observability/alloy/config.alloy
else
  echo
  echo "SKIP: alloy binary not found (CI 不做此项；改动 config.alloy 请本机 alloy fmt)"
fi

# --- 7. 探针 dry-run -----------------------------------------------------------
step "synthetic probe dry-run"
deploy/observability/synthetic/run-synthetic.sh --dry-run >/dev/null

echo
echo "ALL OBSERVABILITY CHECKS PASSED"
