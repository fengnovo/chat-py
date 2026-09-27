-- 知识库图片资源 VLM caption 异步任务表。设计与 knowledge_index_jobs 对齐：
--   - 同租户 / 同 KB / 幂等补偿（reconciler 通过 listQueuedOrStaleCaptionJobs 扫描）。
--   - 资源级失败只能把对应图片降级为「无 caption」，不应阻塞整文档索引。
--   - 通过 (tenant_id, asset_id) 唯一约束保证「同一张图最多一个进行中的任务」。
CREATE TABLE knowledge_caption_jobs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  asset_id uuid NOT NULL REFERENCES knowledge_assets(id) ON DELETE CASCADE,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'skipped')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  lease_expires_at timestamptz,
  model text,
  error_code text,
  error_message text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX knowledge_caption_jobs_active_asset_idx
  ON knowledge_caption_jobs (asset_id)
  WHERE status IN ('queued', 'running');
CREATE INDEX knowledge_caption_jobs_claim_idx
  ON knowledge_caption_jobs (status, next_attempt_at, created_at);
CREATE INDEX knowledge_caption_jobs_tenant_kb_idx
  ON knowledge_caption_jobs (tenant_id, kb_id, created_at DESC);

-- 资产表的 caption 缓存 + VLM 来源 / 错误，便于 /api/v1/knowledge-bases/:kb/assets 列表直接读。
ALTER TABLE knowledge_assets
  ADD COLUMN IF NOT EXISTS caption_status text NOT NULL DEFAULT 'pending'
    CHECK (caption_status IN ('pending', 'running', 'ready', 'failed', 'skipped', 'disabled')),
  ADD COLUMN IF NOT EXISTS caption_model text,
  ADD COLUMN IF NOT EXISTS caption_error text,
  ADD COLUMN IF NOT EXISTS caption_attempts integer NOT NULL DEFAULT 0 CHECK (caption_attempts >= 0),
  ADD COLUMN IF NOT EXISTS caption_updated_at timestamptz;

CREATE INDEX IF NOT EXISTS knowledge_assets_caption_status_idx
  ON knowledge_assets (kb_id, caption_status) WHERE deleted_at IS NULL;