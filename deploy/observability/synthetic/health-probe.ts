/**
 * 合成探针（Task 11）：
 *
 * - 常规（每 60s）：live、ready、鉴权失败预期。
 * - canary（每 15m，--canary）：最小 chat run 入队 + 最小知识检索。
 *
 * 结果只写两类东西：
 *   1. OTLP 指标 -> Alloy（synthetic.probe.result / synthetic.probe.duration），
 *      label 只有 check/outcome，低基数；
 *   2. stdout 结构化日志（由 journal 采集），不含用户内容、不含 token。
 *
 * 探针脚本独立于 monorepo 构建，仅使用 Node 内建模块；
 * 运行方式见 run-synthetic.sh（tsx 或 Node 原生 type stripping）。
 */

declare const process: {
  env: Record<string, string | undefined>;
  argv: string[];
  exit(code?: number): never;
};

const env = process.env;
const BASE_URL = (env.PROBE_BASE_URL ?? `http://127.0.0.1:${env.API_PORT ?? '8000'}`).replace(/\/+$/, '');
const OTLP_ENDPOINT = (env.OTEL_EXPORTER_OTLP_ENDPOINT ?? 'http://127.0.0.1:4318').replace(/\/+$/, '');
const API_TOKEN = env.PROBE_API_TOKEN?.trim();
const KB_ID = env.PROBE_KB_ID?.trim();
const REQUEST_TIMEOUT_MS = Number(env.PROBE_TIMEOUT_MS ?? 8_000);
const DRY_RUN = process.argv.includes('--dry-run');
const CANARY = process.argv.includes('--canary');

type CheckName = 'live' | 'ready' | 'auth_reject' | 'canary_chat' | 'canary_retrieval';
type Outcome = 'success' | 'failure';

interface CheckResult {
  check: CheckName;
  outcome: Outcome;
  durationMs: number;
  detail?: string;
}

const results: CheckResult[] = [];

async function timed(check: CheckName, fn: () => Promise<unknown>): Promise<boolean> {
  const started = performance.now();
  try {
    await fn();
    results.push({ check, outcome: 'success', durationMs: Math.round(performance.now() - started) });
    return true;
  } catch (error) {
    // detail 只取错误构造器名，绝不取 message 原文（可能带 URL/参数）
    results.push({
      check,
      outcome: 'failure',
      durationMs: Math.round(performance.now() - started),
      detail: error instanceof Error ? error.constructor.name.slice(0, 40) : 'UnknownError',
    });
    return false;
  }
}

async function httpJson(
  path: string,
  init: { method?: string; token?: string; body?: unknown } = {},
): Promise<{ status: number; json: any }> {
  const response = await fetch(`${BASE_URL}${path}`, {
    method: init.method ?? 'GET',
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    headers: {
      'content-type': 'application/json',
      ...(init.token ? { authorization: `Bearer ${init.token}` } : {}),
    },
    ...(init.body !== undefined ? { body: JSON.stringify(init.body) } : {}),
  });
  let json: any;
  try { json = await response.json(); } catch { json = undefined; }
  return { status: response.status, json };
}

async function runChecks(): Promise<void> {
  await timed('live', async () => {
    const { status } = await httpJson('/health/live');
    if (status !== 200) throw new Error(`unexpected_status_${status}`);
  });

  await timed('ready', async () => {
    const { status } = await httpJson('/health/ready');
    if (status < 200 || status >= 300) throw new Error(`unexpected_status_${status}`);
  });

  // 伪凭据必须被拒绝；返回 2xx 才是真正的故障（鉴权失效）
  await timed('auth_reject', async () => {
    const { status } = await httpJson('/api/auth/login', {
      method: 'POST',
      body: { email: 'synthetic-nonexistent@invalid', password: 'synthetic-invalid' },
    });
    if (status !== 400 && status !== 401 && status !== 403) {
      throw new Error(`auth_not_rejected_${status}`);
    }
  });

  if (!CANARY) return;

  if (!API_TOKEN) {
    results.push({ check: 'canary_chat', outcome: 'failure', durationMs: 0, detail: 'MissingProbeToken' });
    if (KB_ID !== undefined) {
      results.push({ check: 'canary_retrieval', outcome: 'failure', durationMs: 0, detail: 'MissingProbeToken' });
    }
    return;
  }

  // 最小 chat run：建会话 -> 入队（202 accepted 即探针成功，不等待模型执行）。
  await timed('canary_chat', async () => {
    const created = await httpJson('/api/agent/sessions', {
      method: 'POST',
      token: API_TOKEN,
      body: { title: 'synthetic-canary' },
    });
    if (created.status !== 201 && created.status !== 200) {
      throw new Error(`session_create_${created.status}`);
    }
    const sessionId = created.json?.id;
    if (typeof sessionId !== 'string' || !sessionId) throw new Error('session_id_missing');
    const run = await httpJson(`/api/agent/sessions/${encodeURIComponent(sessionId)}/runs`, {
      method: 'POST',
      token: API_TOKEN,
      body: { message: 'synthetic ping' },
    });
    if (run.status !== 200 && run.status !== 202) throw new Error(`run_not_accepted_${run.status}`);
  });

  if (KB_ID) {
    await timed('canary_retrieval', async () => {
      const { status } = await httpJson(
        `/api/knowledge-bases/${encodeURIComponent(KB_ID)}/retrieval`,
        { method: 'POST', token: API_TOKEN, body: { query: 'synthetic ping', topK: 1 } },
      );
      if (status < 200 || status >= 300) throw new Error(`unexpected_status_${status}`);
    });
  }
}

function otlpAttributes(labels: Array<[string, string]>) {
  return labels.map(([key, value]) => ({ key, value: { stringValue: value } }));
}

async function pushMetrics(): Promise<void> {
  const now = String(BigInt(Date.now()) * 1_000_000n);
  const resultPoints = results.map((r) => ({
    attributes: otlpAttributes([['check', r.check], ['outcome', r.outcome]]),
    asInt: '1',
    timeUnixNano: now,
  }));
  const durationPoints = results.map((r) => ({
    attributes: otlpAttributes([['check', r.check]]),
    asInt: String(Math.max(0, r.durationMs)),
    timeUnixNano: now,
  }));
  const body = {
    resourceMetrics: [{
      resource: { attributes: [{ key: 'service.name', value: { stringValue: 'chat-synthetic-probe' } }] },
      scopeMetrics: [{
        scope: { name: 'synthetic-probe' },
        metrics: [
          {
            name: 'synthetic.probe.result',
            sum: { aggregationTemporality: 2, isMonotonic: true, dataPoints: resultPoints },
          },
          {
            name: 'synthetic.probe.duration',
            sum: { aggregationTemporality: 2, isMonotonic: false, dataPoints: durationPoints },
          },
        ],
      }],
    }],
  };
  await fetch(`${OTLP_ENDPOINT}/v1/metrics`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(3_000),
  }).catch(() => undefined);
}

async function main(): Promise<number> {
  if (DRY_RUN) {
    console.log(JSON.stringify({
      level: 'info',
      msg: 'synthetic probe dry-run',
      baseUrl: BASE_URL,
      otlpEndpoint: OTLP_ENDPOINT,
      canary: CANARY,
      checks: ['live', 'ready', 'auth_reject', ...(CANARY ? ['canary_chat', 'canary_retrieval'] : [])],
    }));
    return 0;
  }
  await runChecks();
  // 指标推送失败不改变探针退出码（fail-open：遥测故障不影响健康结论本身）
  await pushMetrics();
  const failed = results.filter((r) => r.outcome === 'failure');
  console.log(JSON.stringify({
    level: failed.length > 0 ? 'error' : 'info',
    msg: 'synthetic probe finished',
    canary: CANARY,
    checks: results.map((r) => `${r.check}:${r.outcome}`),
    ...(failed.length > 0 ? { failures: failed.map((f) => ({ check: f.check, detail: f.detail })) } : {}),
  }));
  return failed.length > 0 ? 1 : 0;
}

main().then((code) => process.exit(code)).catch(() => process.exit(1));
