# 观测链路 Smoke 手册

验证「应用 → Alloy → Tempo/Prometheus/Loki → Grafana」以及「Worker → Langfuse」整条链路。
适用于本地 PoC；生产步骤相同，仅 endpoint 与凭据来源不同。

## 0. 前置：启动观测栈与业务

```bash
# 观测栈（仓库根目录）
GRAFANA_ADMIN_PASSWORD=change-me \
  docker compose -f deploy/compose.observability.yaml up -d --wait

# 业务基础设施 + 应用（systemd 或本地 dev）
docker compose -f deploy/compose.infra.yaml --env-file .env up -d --wait
```

应用侧环境（dev）：

```dotenv
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
OTEL_TRACES_SAMPLER_ARG=1
LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=https://cloud.langfuse.com   # 或自托管地址
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_SAMPLE_RATE=1
```

## 1. OTLP 入口存活

```bash
# 无 body 也应返回非连接拒绝（400/405 都说明端口在监听）
curl -i http://127.0.0.1:4318/v1/traces
curl -s http://127.0.0.1:12345/-/ready
```

## 2. 发两条最小流量

1. 一条登录请求（产生 `mcp.request`/`http.server.duration` 之外的 API SERVER span）。
2. 一条最小 agent run（让 Worker 产生 `worker.job.execute` → `agent.run` → GenAI span）。

## 3. Tempo：按 service / trace id 查 trace

Grafana → Explore → Tempo → Search：

- Service Name 选 `agent-api` / `agent-worker` / `knowledge-service`。
- 打开一次 `worker.job.execute` trace，应看到：
  - span link 指向 outbox producer；
  - 子 span 包含 `agent.run`、`gen_ai.*`、`tool.*`、`sandbox.*`；
  - 属性中**不应出现** prompt/completion/工具参数正文。
- API 与 Knowledge 的 trace 中不应出现 Langfuse 专属 observation。

## 4. Loki：按 request_id / service 查日志

Grafana → Explore → Loki，LogQL 示例：

```logql
{deployment="chat"} |= "worker.job.execute"
{service_name="agent-api"} | json | request_id != ""
```

journal 日志带 `source="journal"`、`unit` 标签；容器日志带 `source="docker"`、
`container_name` 标签。

## 5. Prometheus：查业务指标

Grafana → Explore → Prometheus，PromQL 示例：

```promql
rate(http_server_requests_total[1m])
histogram_quantile(0.95, sum(rate(http_server_request_duration_seconds_bucket[5m])) by (le, route))
rate(queue_jobs_completed_total[1m])
rate(knowledge_operation_duration_seconds_count[1m])
```

OTel 指标命名经 Prometheus exporter 归一化（`.`/`-` → `_`，加单位后缀）；
label 保持低基数：只有 route/method/status/outcome/queue/job/operation 等，
没有 run_id/user_id/tenant_id。

## 6. Langfuse：查 GenAI trace

在 Langfuse UI：

- 一次 run 对应**一个** trace，trace 下挂模型与工具 observation；
- trace metadata 只有 allow-list：`run_id`（8 位短引用）、`run_kind`、
  `environment`、`tempo_trace_id`、可选 provider/model/model_family；
- userId 为 `u_` 开头的伪名，不含原始用户标识；
- 默认内容开关关闭时看不到 prompt/completion/tool args；
- 用 `tempo_trace_id` 可在 Tempo 中找到同一 run 的完整业务 trace。

## 7. 采集器故障注入（fail-open 验证）

```bash
docker stop chat-observability-alloy-1
```

期间应用登录与 run **必须仍然成功**（仅 OTLP 导出失败计数上涨、本地日志出现
脱敏告警）；重新 `start` 后链路自动恢复，无需重启应用。
