# 部署手册

面向：**1 台 8 核 16G / 40G 系统盘 / 3Mbps 的 ECS（Debian 12）**，先跑通聊天 + Agent + 沙箱。

## 0. 本次范围（重要）

| 组件 | 本次 | 说明 |
|---|---|---|
| Postgres / Redis / MinIO | ✅ Docker Compose | `deploy/compose.infra.yaml` |
| Web / API / Worker | ✅ systemd 宿主机进程 | Worker 需要宿主 Docker，见 §1.3 |
| Docker 沙箱 | ✅ 宿主 Docker | 需预先构建 `chat-agent-sandbox` 镜像 |
| **knowledge-service** | ✅ systemd 宿主机进程 | |

> 本目录下**全部是新增文件**，仓库里任何现有文件都没有被修改。

---

## 1. 前置条件

### 1.1 服务器
- 8 核 / 16 GB / 40 GB 系统盘（ESSD），Debian 12 或 Ubuntu 22.04+
- 公网带宽 3 Mbps（只有浏览器下载走上限；上传不受影响）

### 1.2 安全组（只开这些）
| 端口 | 用途 | 来源 |
|---|---|---|
| 22 | SSH | 你的固定 IP |
| 80 | HTTP → HTTPS 跳转 + Let's Encrypt 校验 | 0.0.0.0/0 |
| 443 | Web + API + MinIO 同源代理（`/agent-artifacts/`，§8.3） | 0.0.0.0/0 |
| 9443 | （可选）MinIO 独立端口备选方案，同源方案下不需要 | 0.0.0.0/0 |

**不要**对外开放 3020 / 8002 / 55432 / 56379 / 59000 —— 这些在 `.env` 里都绑定在 127.0.0.1。

### 1.3 为什么应用跑在宿主机而不是 Docker
Worker 通过 `spawn('docker', ['run', ...])` 创建沙箱容器，并把宿主目录 bind mount 进去。
如果 Worker 本身跑在容器里，挂载路径和宿主机路径对不上，沙箱会挂载失败。
所以：**基础设施用 Compose，应用用 systemd。**

### 1.4 域名与 HTTPS（硬性要求）
`AUTH_MODE=password` 时，生产环境的会话 cookie 是 `Secure` 的 —— **没有 HTTPS 就登录不了**
（表现为"登录成功但立刻跳回登录页"）。两种方案：

- **有域名且已完成 ICP 备案**：用 Let's Encrypt（§8.2），推荐。
- **没有域名 / 未备案**：用自签证书（§8.4）。大陆地域未备案的域名在 80/443 会被拦截，
  可以用非标准端口（如 8443）+ 自签证书先验证。

---

## 2. 把代码固化下来

工作区里有一批**未提交**的改动（密码登录、注册、admin 用户管理、迁移 010 等），
其中 `AUTH_MODE=password` 是这个功能才有的。直接部署工作区无法复现，建议先提交：

```bash
# 在本地 worktree 里
git add -A && git commit -m "feat: password auth, admin user management and kb grants"
git push   # 或者在下一节用 rsync 直接把工作区传上去
```

> 我没动任何代码；这一步需要你自己决定提交信息。

---

## 3. 系统初始化

```bash
# 上传 deploy 目录后（或在服务器上已有仓库时）
sudo bash /opt/chat/deploy/scripts/host-setup.sh
```

脚本做这些事：装 Docker（官方源）+ Nginx + certbot；配置 Docker 日志上限（`max-size=10m`，
否则沙箱容器日志会吃满磁盘）；装 Node 22 + pnpm 11；创建 `chatapp` 用户并加入 `docker` 组；
创建 `/opt/chat/data/{workspaces,sandboxes}`。

可选环境变量：
```bash
# 阿里云容器镜像加速（控制台可获取专属地址），加快拉镜像
sudo DOCKER_MIRROR=https://xxxx.mirror.aliyuncs.com bash deploy/scripts/host-setup.sh
# 4G swap（16G 内存不必须）
sudo SWAP_GB=4 bash deploy/scripts/host-setup.sh
```

---

## 4. 代码就位

```bash
sudo mkdir -p /opt/chat && sudo chown chatapp:chatapp /opt/chat
```

**方式 A：git**
```bash
sudo -iu chatapp git clone <你的仓库> /opt/chat
cd /opt/chat && git checkout <要部署的分支/commit>
```

**方式 B：从本地 rsync（未提交也能用）**
```bash
rsync -av --delete \
  --exclude node_modules --exclude .next --exclude dist --exclude .turbo \
  --exclude .git --exclude data \
  ./ chatapp@<服务器IP>:/opt/chat/
```

> `node_modules` / `.next` / `dist` 都不要传：它们含平台相关的原生二进制，
> 必须在目标机上重新安装构建。

---

## 5. 配置 `.env`

```bash
cd /opt/chat
cp deploy/env.production.example .env
chmod 600 .env
```

必改项（其余看注释）：

```bash
# 生成三处密钥
openssl rand -hex 32   # → AUTH_JWT_SECRET
openssl rand -hex 16   # → POSTGRES_PASSWORD（同时替换 DATABASE_URL 里的密码）
openssl rand -hex 16   # → MINIO_ROOT_PASSWORD
```

| 变量 | 值 |
|---|---|
| `WEB_ORIGIN` | `https://你的域名`（自签方案填 `https://公网IP`） |
| `S3_PUBLIC_ENDPOINT` | `https://你的域名`（与主站同源，Nginx 代理 `/agent-artifacts/`；独立端口方案才用 `:9443`） |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | 与 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 一致 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL` | 你的模型服务 |
| `AUTH_JWT_SECRET` | 上一步生成，≥32 字符 |
| `EMBEDDING_PROFILE` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | **即使不上知识库也必须填全**，否则 API 拒绝启动 |

---

## 6. 启动基础设施

```bash
cd /opt/chat
docker compose -f deploy/compose.infra.yaml --env-file .env up -d --wait
docker compose -f deploy/compose.infra.yaml ps
```

---

## 7. 迁移数据库 + 建账号

```bash
cd /opt/chat
pnpm install --frozen-lockfile        # 若报 ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY，加 CI=true
pnpm db:migrate
pnpm db:seed                          # 建默认租户 + admin/owner/user 三个账号
```

> API 启动时也会自动迁移，但显式跑一次更清楚。

**立刻改掉种子密码**（三个账号默认 `admin123` / `owner123` / `user123`）：

```bash
BASE=http://127.0.0.1:8002
# 1) 用 admin 登录，cookie 存到文件
curl -s -c /tmp/admin.jar -H 'content-type: application/json' \
  -d '{"username":"admin","password":"admin123"}' $BASE/api/auth/login

# 2) 查用户列表拿 id
curl -s -b /tmp/admin.jar $BASE/api/admin/users | jq '.data[] | {id, username, role}'

# 3) 改密码（把 <uid> 换成上一步的 id）
curl -s -b /tmp/admin.jar -X PATCH -H 'content-type: application/json' \
  -d '{"password":"改成你自己的强密码"}' $BASE/api/admin/users/<uid>

# 4) 为第 2 个人建号
curl -s -b /tmp/admin.jar -X POST -H 'content-type: application/json' \
  -d '{"username":"alice","displayName":"Alice","password":"另一个强密码","role":"member"}' \
  $BASE/api/admin/users
```

> 也可以临时设 `AUTH_SIGNUP_ENABLED=true`，让两个人在 `/register` 自助注册（自动是 member）。

---

## 8. 构建发布

```bash
cd /opt/chat
bash deploy/scripts/app-build.sh
```

它依次做：`pnpm install --frozen-lockfile` → `pnpm build` → 构建沙箱镜像
（`docker build -f infra/sandbox/Dockerfile -t chat-agent-sandbox:latest infra/sandbox`）
→ 安装 3 个 systemd unit → 重启服务 → 自检 `/health/ready`。

沙箱镜像要下载约 1 GB（apt + pip 科学栈 + vite/react），国内直连 pypi.org 会慢。
加速方式（不改仓库文件）：

```bash
cd /opt/chat
docker build -f deploy/sandbox/Dockerfile.mirror -t chat-agent-sandbox:latest infra/sandbox
```

`pnpm install` 慢的话：`pnpm config set registry https://registry.npmmirror.com`。

---

## 9. Nginx + HTTPS

### 9.1 装配置
```bash
sudo cp /opt/chat/deploy/nginx/chat.conf /etc/nginx/conf.d/chat.conf
sudo sed -i 's/chat\.example\.com/你的域名/g' /etc/nginx/conf.d/chat.conf
```

### 9.2 签发证书（有备案域名）
```bash
sudo mkdir -p /var/www/certbot
sudo certbot --nginx -d 你的域名
sudo nginx -t && sudo systemctl reload nginx
sudo certbot renew --dry-run     # 确认自动续期
```

### 9.3 关键配置说明
- `proxy_buffering off` + `chunked_transfer_encoding on` + 长超时：**SSE 流式输出必需**，
  少了这些聊天会一次性卡住不出字。
- `/api/` 直接转发到 `8002`，绕过 Next 的 rewrite，少一跳、流式更稳。
- MinIO 走主站同源时，443 段必须有 `location /agent-artifacts/ { proxy_pass http://chat_minio; }`，
  且 `.env` 的 `S3_PUBLIC_ENDPOINT=https://你的域名`（不带端口）。
- 独立端口方案（:9443）下 MinIO 段的 `proxy_set_header Host $http_host;` **必须保留端口**：
  预签名 URL 的 SigV4 签名包含 Host，丢了端口会 `SignatureDoesNotMatch`。
- `location = /knowledge { return 404; }` 暂时挡掉知识库页面（§11）。启用后删掉。

### 9.4 没有域名：自签证书
```bash
sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/chat.key -out /etc/nginx/ssl/chat.crt \
  -subj "/CN=你的公网IP" -addext "subjectAltName=IP:你的公网IP"
```
把 `chat.conf` 里的 `ssl_certificate*` 指到上面两个文件、`server_name` 改成 IP、
`listen 443 ssl` 改成 `listen 8443 ssl`，并把 `.env` 的 `WEB_ORIGIN` 与
`S3_PUBLIC_ENDPOINT` 改成 `https://IP:8443` / `https://IP:9443`，安全组放行 8443。
浏览器首次访问需要手动信任证书。

---

## 10. 验收清单

```bash
# 1) 进程与健康
systemctl status chat-api chat-worker chat-web --no-pager
curl -s http://127.0.0.1:8002/health/live
curl -s http://127.0.0.1:8002/health/ready      # 期望 {"status":"ready"}（含 pg/redis/s3 探测）

# 2) 外网入口
curl -I https://你的域名/

# 3) 跑一条消息时观察沙箱
docker ps --filter name=docker-            # 执行中应能看到临时容器
watch -n2 'ls -la /opt/chat/data/sandboxes'
```

浏览器验收：
- [ ] 用 admin 登录成功，刷新后仍是登录态（验证 cookie + HTTPS 正确）
- [ ] 新建会话、发一条消息，**逐字流式输出**（验证 SSE 没被 Nginx 缓冲）
- [ ] 第 2 个账号能同时使用，两边互不干扰
- [ ] 让 Agent 跑一次多步任务（写文件/执行命令），确认沙箱正常
- [ ] 历史会话刷新后仍在，可切换、重命名、删除
- [ ] `journalctl -u chat-worker -f` 无持续报错

---

## 11. 日常运维

```bash
# 日志
journalctl -u chat-api -f
journalctl -u chat-worker -f
journalctl -u chat-web -f

# 更新代码后重新发布
cd /opt/chat && git pull && bash deploy/scripts/app-build.sh

# 基础设施
docker compose -f deploy/compose.infra.yaml --env-file .env ps
docker compose -f deploy/compose.infra.yaml --env-file .env logs -f postgres

# 备份（数据库 + 对象存储）
docker compose -f deploy/compose.infra.yaml --env-file .env exec -T postgres \
  pg_dump -U agent agent | gzip > /root/backup-$(date +%F).sql.gz

# 磁盘（40G 要盯着）
df -h /                    # 系统盘
docker system df           # 镜像/容器占用
pnpm store prune           # 清理 pnpm 全局存储
```

**务必加清理定时任务**：会话目录没有 TTL，不清理会把 40G 吃满。

```bash
sudo crontab -e
30 4 * * * /opt/chat/deploy/scripts/cleanup-sandboxes.sh >> /var/log/chat-cleanup.log 2>&1
```

---

## 12. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 登录成功但立刻跳回登录页 | 走的是 HTTP，`Secure` cookie 存不下 | 必须 HTTPS；检查 `WEB_ORIGIN` 与实际访问地址一致 |
| API 起不来：`AUTH_MODE=dev is forbidden in production` | 忘了改 | `.env` 设 `AUTH_MODE=password` |
| API 起不来：`AUTH_JWT_SECRET ... required` | 缺密钥 | `openssl rand -hex 32` 填入 |
| API 起不来：`Knowledge embedding profile is required` | 缺 `EMBEDDING_*` | 三个变量一起填上（只是元数据） |
| Worker 报 `docker: command not found` / 权限拒绝 | `chatapp` 未在 docker 组或服务未重启 | `sudo usermod -aG docker chatapp && sudo systemctl restart chat-worker` |
| 聊天不出字、最后一次性刷出 | Nginx 缓冲了 SSE | 确认 `proxy_buffering off` 与长 `proxy_read_timeout` 生效 |
| 上传/下载 `SignatureDoesNotMatch` | 预签名 Host 与 Nginx 转发的不一致 | MinIO 段必须 `proxy_set_header Host $http_host;`，且 `S3_PUBLIC_ENDPOINT` 与访问地址完全一致 |
| 上传一直 pending / 超时 / 控制台 CORS 报错 | 预签名 URL 走 `:9443` 但安全组没放行，或跨域被拦 | 改用同源方案：443 加 `location /agent-artifacts/`，`S3_PUBLIC_ENDPOINT=https://域名`，重启 API；或安全组放行 9443 |
| `pnpm install` 报 `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` | 非 TTY 下 pnpm 拒绝重建 node_modules | `CI=true pnpm install --frozen-lockfile` |
| 沙箱命令返回 124 | 单条命令超时 | 调整 `DOCKER_SANDBOX_COMMAND_TIMEOUT_MS`（上限 600000） |
| 磁盘逐渐满 | 会话目录 + 容器日志 | 上 §12 的清理 cron；确认 Docker `log-opts` 已生效 |

---

## 13. 可观测性（OpenTelemetry + Alloy + Grafana）

可选组件，**默认全部关闭**（`OTEL_ENABLED=false` / `LANGFUSE_ENABLED=false`），
遥测任何环节故障都不影响业务（fail-open）。

### 13.1 启动本地观测栈

```bash
cd /opt/chat
# 首次必须设置 Grafana 管理员密码
GRAFANA_ADMIN_PASSWORD='强密码' \
  docker compose -f deploy/compose.observability.yaml up -d --wait
```

| 服务 | 地址（仅 127.0.0.1） |
|---|---|
| Grafana | http://127.0.0.1:33000 （admin / `GRAFANA_ADMIN_PASSWORD`，自动 provision 5 个 dashboard） |
| Prometheus | http://127.0.0.1:39090 （含 SLO 录制/告警规则） |
| Alertmanager | http://127.0.0.1:39093 |
| Alloy OTLP | 127.0.0.1:4317 (gRPC) / 4318 (HTTP) |
| Tempo / Loki | 仅集群内暴露，通过 Grafana 查询 |

告警通知 webhook 通过 `PAGE_WEBHOOK_URL` / `URGENT_WEBHOOK_URL` / `TICKET_WEBHOOK_URL`
注入（compose 同目录 `.env` 或 shell 环境）；不配置时指向 `.invalid`，规则照常评估但永不投递。

### 13.2 应用接入（已内置，零改动）

4 个 systemd 单元已加 `--import .../observability/dist/register.js` 与 `TimeoutStopSec=15`，
并强制各自的 `OTEL_SERVICE_NAME`（agent-api / agent-worker / knowledge-service / chat-web），
shutdown 时遥测 flush 上限 5s。只需在 `/opt/chat/.env` 中开启：

```bash
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
OTEL_ENVIRONMENT=production
# 可选：Langfuse GenAI 专项观测（只有 Worker 会建 callback）
# LANGFUSE_ENABLED=true / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL
```

改完 `systemctl restart chat-api chat-worker chat-knowledge chat-web`。
SLO/告警说明见 `docs/observability/slo-and-alerts.md`，故障处置见
`docs/observability/runbooks/`。

### 13.3 合成探针（主动拨测）

结果仅以低基数指标（`synthetic_probe_result` / `synthetic_probe_duration`，
label 只有 check/outcome）和 journal 日志输出，绝不记录用户内容。

```bash
# 手工自检（不发任何业务请求）
/opt/chat/deploy/observability/synthetic/run-synthetic.sh --dry-run

# 启用：60s 常规探针（live/ready/鉴权失败预期）+ 15m canary（最小 run + 检索）
sudo cp /opt/chat/deploy/systemd/chat-synthetic-{probe,canary}.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chat-synthetic-probe.timer chat-synthetic-canary.timer
```

canary 需要专用合成用户（最小权限）的凭据，写入 `/opt/chat/.env`：

```bash
PROBE_API_TOKEN=<合成用户 JWT 或长效 token>
PROBE_KB_ID=<可选，用于检索 canary 的知识库 id>
```

---

## 附：文件清单

```
deploy/
├── README.md                        # 本文件
├── compose.infra.yaml               # Postgres / Redis / MinIO
├── compose.observability.yaml       # Alloy / Tempo / Loki / Prometheus / AM / Grafana
├── env.production.example           # 生产 .env 模板
├── nginx/chat.conf                  # 主站 + MinIO 反代（含 SSE 配置）
├── observability/
│   ├── alloy/config.alloy           # OTLP 接收 -> Tempo/Prometheus/Loki
│   ├── alertmanager/                # 三级路由 + env 注入 entrypoint
│   ├── prometheus/prometheus.yaml   # 含 rule_files / alerting
│   ├── tempo/ loki/ grafana/        # 存储与 provisioning
│   └── synthetic/                   # 合成探针 health-probe.ts + run-synthetic.sh
├── systemd/
│   ├── chat-api.service             # --import 预载遥测，TimeoutStopSec=15
│   ├── chat-worker.service          # SupplementaryGroups=docker
│   ├── chat-knowledge.service
│   ├── chat-web.service             # NODE_OPTIONS=--import
│   ├── chat-synthetic-probe.{service,timer}    # 60s 拨测
│   └── chat-synthetic-canary.{service,timer}   # 15m canary
├── sandbox/Dockerfile.mirror        # 沙箱镜像国内加速变体（可选）
└── scripts/
    ├── host-setup.sh                # 宿主机初始化
    ├── app-build.sh                 # 构建 + 发布
    ├── cleanup-sandboxes.sh         # 会话目录清理（cron）
    ├── verify-observability.sh      # 可观测性静态校验
    └── fault-injection-observability.sh  # 9 类故障 fail-open 演练
```

---

## 14. 上线到 chat.keen-tech.top（2026-09 可观测性版本）

面向服务器 **101.96.208.160**、域名 **https://chat.keen-tech.top**、部署目录 `/opt/chat`。
本次更新在已有部署上叠加：可观测性（OTel + Alloy + Grafana 栈）、合成探针、
4 个 systemd 单元的 `--import` 预载。首次裸机部署仍走第 1~12 节，本节是**更新清单**。

> 密钥纪律：`deploy/env.keen-tech.top` 已被 `.gitignore` 忽略且模板内密钥已全部换成
> `REPLACE_*` 占位。真实值只存在于服务器 `/opt/chat/.env`（chmod 600），
> rsync/git 都不应带走它。

### 14.1 同步代码到服务器

```bash
# 在本机（工作区根目录）——注意 --exclude .env，绝不覆盖服务器配置
rsync -av --delete --progress \
  --exclude '.env' \
  --exclude 'deploy/.env' \
  --exclude 'node_modules/' \
  --exclude 'apps/desktop/' \
  --exclude 'docs/' \
  --exclude 'packages/ai-cli/sessions/' \
  --exclude '.superpowers' \
  --exclude '.turbo' \
  --exclude '.worktrees' \
  --exclude '.github' \
  --exclude '.trace' \
  --exclude '.pnpm-store' \
  --exclude '.next/' \
  --exclude 'dist/' \
  --exclude 'data/' \
  --exclude '.git' \
  ./ root@101.96.208.160:/opt/chat/
```

### 14.2 合并 .env 新键（不要 cp 覆盖）

模板新增了一整段可观测性配置（OTEL_* / LANGFUSE_* / PROBE_*）。
在服务器上手工把这些键追加进 `/opt/chat/.env`，**保留服务器上的真实密钥**：

```bash
ssh chatapp@101.96.208.160
cd /opt/chat
# 对照模板把新增段落补进 .env（OTEL_ENABLED=true、OTEL_ENVIRONMENT=production、
# OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 等）；
# OPENAI_API_KEY / EMBEDDING_API_KEY / 各 REPLACE_* 不要动现有的真实值
diff <(grep -oE '^[A-Z_]+' .env | sort) <(grep -oE '^[A-Z_]+' deploy/env.keen-tech.top | sort)
chmod 600 .env
```

注意：`OTEL_SERVICE_NAME` 在 .env 里**留空**，四个 systemd 单元会各自强制设置。

### 14.3 启动观测栈（先于应用重启）

```bash
cd /opt/chat
# compose 自动读取 compose 文件同目录的 .env（deploy/.env）；只在服务器创建，不入库
sudo install -m 600 /dev/null /opt/chat/deploy/.env
sudo tee -a /opt/chat/deploy/.env >/dev/null <<'EOF'
GRAFANA_ADMIN_PASSWORD=换成强密码
# 暂无通知渠道时保留下面三行默认值（.invalid，永不投递，告警照常评估）
PAGE_WEBHOOK_URL=http://notify-page.disabled.invalid/hook
URGENT_WEBHOOK_URL=http://notify-urgent.disabled.invalid/hook
TICKET_WEBHOOK_URL=http://notify-ticket.disabled.invalid/hook
EOF

docker compose -f deploy/compose.observability.yaml up -d --wait
docker compose -f deploy/compose.observability.yaml ps   # 6 个容器全 healthy
```

观测端口全部只绑 127.0.0.1，**安全组只放行 22/80/443**，不要开
33000/39090/39093/4317/4318。Grafana/Prometheus 走 SSH 隧道在本机浏览器访问：

```bash
ssh -N -L 33000:127.0.0.1:33000 -L 39090:127.0.0.1:39090 chatapp@101.96.208.160
# 本机浏览器：http://127.0.0.1:33000（admin / 上一步的 GRAFANA_ADMIN_PASSWORD）
```

资源预算：观测栈上限约 2.4 GiB 内存 / 4.5 CPU，与业务同机 8C16G 有余量；
调参与扩容顺序见 [capacity-baseline.md](../docs/observability/capacity-baseline.md)。

### 14.4 构建并重启应用

```bash
cd /opt/chat
bash deploy/scripts/app-build.sh
```

app-build 会安装新版 systemd 单元（已带 `--import` 预载与 `TimeoutStopSec=15`）
并重启 api/worker/web/knowledge。确认四个服务 active 且无遥测初始化报错：

```bash
systemctl is-active chat-api chat-worker chat-knowledge chat-web
journalctl -u chat-api -u chat-worker --since '2 min ago' --no-pager | grep -iE 'observ|otel' || echo '无遥测报错'
curl -s http://127.0.0.1:8002/health/ready
```

### 14.5 更新 Nginx（upstream 必须改回环）

本次 [chat.keen-tech.top.conf](nginx/chat.keen-tech.top.conf) 把 upstream 从公网 IP
改成了 `127.0.0.1`——应用只监听回环，写公网 IP 会连不上。

```bash
sudo cp /opt/chat/deploy/nginx/chat.keen-tech.top.conf /etc/nginx/conf.d/chat.conf
sudo nginx -t && sudo systemctl reload nginx
```

证书沿用 `/etc/nginx/ssl/keen-tech.top.{crt,key}`；MinIO 主链路走 443 的
`/agent-artifacts/`，**9443 不必在安全组放行**。

### 14.6 合成探针

常规探针（live/ready/鉴权拒绝，60s 一次，无需凭据）直接装：

```bash
sudo cp /opt/chat/deploy/systemd/chat-synthetic-probe.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chat-synthetic-probe.timer
systemctl list-timers chat-synthetic-probe.timer
journalctl -u chat-synthetic-probe -n 20 --no-pager     # 看到 synthetic probe finished
```

canary（15m 一次真实建会话+发 run）可选。先在应用里建一个专用合成账号（member），
在服务器上用 jose 签一张长效 token（登录 JWT 本身 7 天过期）：

```bash
cd /opt/chat/apps/api
PROBE_USER_ID=<合成账号 uuid> \
TENANT_ID=00000000-0000-4000-8000-000000000001 \
SECRET=$(grep '^AUTH_JWT_SECRET=' /opt/chat/.env | cut -d= -f2-) \
node --input-type=module -e '
  import { SignJWT } from "jose";
  console.log(await new SignJWT({ tenant_id: process.env.TENANT_ID, roles: ["member"] })
    .setProtectedHeader({ alg: "HS256" }).setSubject(process.env.PROBE_USER_ID)
    .setIssuedAt().setExpirationTime("730d")
    .sign(new TextEncoder().encode(process.env.SECRET)));'
# 把输出写入 /opt/chat/.env：PROBE_API_TOKEN=<token>，然后
sudo cp /opt/chat/deploy/systemd/chat-synthetic-canary.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now chat-synthetic-canary.timer
```

### 14.7 验收（全部走真实 HTTPS 口径，不用 -k / 不走 HTTP）

```bash
# 1) 外网 HTTPS
curl -fsSI https://chat.keen-tech.top/ | head -3
curl -s -o /dev/null -w '%{http_code}\n' https://chat.keen-tech.top/api/auth/login \
  -X POST -H 'content-type: application/json' -d '{}'   # 期望 400 类校验错误，不是 502/超时

# 2) 服务器内健康；Grafana/Prometheus 经 SSH 隧道访问
curl -s http://127.0.0.1:8002/health/ready             # {"status":"ready"}
curl -s http://127.0.0.1:39090/api/v1/targets | grep -o '"health":"up"' | wc -l   # 期望 5
```

浏览器：
- [ ] https://chat.keen-tech.top 登录成功、刷新不掉线（Secure cookie + HTTPS）
- [ ] 发一条消息逐字流式输出；跑一次多步 Agent 任务
- [ ] 隧道内 Grafana：Chat 文件夹 5 个 dashboard；Overview 出现 agent-api 等 job 的曲线；
      Tempo 能查到一条 run 的完整父子链并可跳转 Loki 日志
- [ ] Prometheus `/api/v1/rules` 31 条规则；Alertmanager `/api/v2/receivers` 有 page/urgent/ticket
- [ ] `systemctl list-timers` 中探针 timer 在列，journal 有 finished 记录

告警端到端与 9 类故障演练见第 13 节和 [release-checklist.md](../docs/observability/release-checklist.md)。

### 14.8 回滚

```bash
# 应用层（秒级）：.env 设 OTEL_ENABLED=false、LANGFUSE_ENABLED=false 后重启
sudo systemctl restart chat-api chat-worker chat-knowledge chat-web
# 观测栈整体下线（不影响 infra 与业务）
docker compose -f /opt/chat/deploy/compose.observability.yaml down
# Nginx 异常时：恢复旧 conf 后 nginx -t && systemctl reload
```

发布窗口想屏蔽告警，在 Alertmanager UI（隧道 39093）按 service 建 silence，
不要改 PromQL 做时间排除。
