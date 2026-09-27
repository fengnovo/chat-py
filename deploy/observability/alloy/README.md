# Grafana Alloy 本地采集器

Grafana Alloy 是应用与观测后端之间的唯一采集器（方案 3）：应用通过 OTLP
（gRPC `4317` / HTTP `4318`）只发给本机 Alloy，由它分流到 Tempo / Prometheus / Loki。

## 组件与数据流

| 入口 | 处理 | 出口 |
| --- | --- | --- |
| `otelcol.receiver.otlp`（4317/4318） | memory_limiter → batch | traces→Tempo `4318`、metrics→Prometheus remote-write、logs→Loki |
| `loki.source.journal`（宿主 systemd 日志） | 静态标签 environment/deployment/source | Loki |
| `discovery.docker` + `loki.source.docker`（容器日志） | 容器名/stream relabel + 静态标签 | Loki |
| `prometheus.scrape.alloy`（自监控） | — | Prometheus |

保护策略：

- **memory limiter**：collector 内存触顶时拒绝数据，不拖垮宿主（compose 限制 512MiB）。
- **sending queue + retry**：Tempo 暂时不可用时本地排队（2000 条 / 最长重试 5 分钟）。
- **fail-open**：collector 停摆时 OTel SDK 只丢弃遥测，业务请求不受影响（应用侧
  OTLP exporter 也是非阻塞 batch）。

## 配置校验

```bash
# 语法/格式校验（不启动服务）
docker run --rm -v "$PWD/deploy/observability/alloy:/etc/alloy:ro" \
  grafana/alloy:v1.4.3 fmt --diff /etc/alloy/config.alloy
```

## 运行时排查

- Alloy UI：<http://127.0.0.1:12345>，查看每个 component 的健康状态与导出量。
- Ready 探针：`curl http://127.0.0.1:12345/-/ready`。
- 指标：`curl http://127.0.0.1:12345/metrics | grep otelcol_`，关注
  `otelcol_exporter_send_failed_*`（导出失败）与 `otelcol_receiver_refused_*`（接收拒绝）。

## 安全说明

- 4317/4318/12345 只绑定宿主 loopback，公网不可达。
- collector 不做任何鉴权，因此**不可**直接监听非回环地址；跨主机部署需另加 mTLS。
- journal 与 docker.sock 均只读挂载。
