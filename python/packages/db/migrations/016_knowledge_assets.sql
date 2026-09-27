-- 知识库多模态资源（图片等）。设计原则：
-- 1. 与 knowledge_documents 平级，document_id 可空（孤立资源）；
-- 2. rel_path 存 kb 级相对路径（如 breakfast/苏格兰蛋/egg1.png），用于跨文档去重与检索时与 chunk 引用对齐；
-- 3. content_hash 在同一 kb 内去重，避免重复上传相同图片；
-- 4. 通过 document_id 关联到具体文档，文档删除时保留资源（仅解绑），便于资源复用与统计。

CREATE TABLE knowledge_assets (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  document_id uuid REFERENCES knowledge_documents(id) ON DELETE SET NULL,
  rel_path text NOT NULL,
  name text NOT NULL,
  mime text NOT NULL,
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  content_hash text NOT NULL,
  object_key text NOT NULL,
  caption text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX knowledge_assets_tenant_kb_idx ON knowledge_assets (tenant_id, kb_id) WHERE deleted_at IS NULL;
CREATE INDEX knowledge_assets_document_idx ON knowledge_assets (tenant_id, document_id) WHERE deleted_at IS NULL;
CREATE INDEX knowledge_assets_rel_path_idx ON knowledge_assets (kb_id, rel_path) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX knowledge_assets_active_hash_idx ON knowledge_assets (kb_id, content_hash) WHERE deleted_at IS NULL;