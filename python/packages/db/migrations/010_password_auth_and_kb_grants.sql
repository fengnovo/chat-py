-- 本地账号密码登录 + 知识库授权（admin 将知识库关联给用户在聊天中使用 RAG）。
-- 角色模型保持 001 迁移的 owner/admin/member 三角色，不做角色迁移。

ALTER TABLE users ADD COLUMN username text UNIQUE;
ALTER TABLE users ADD COLUMN password_hash text;

CREATE TABLE knowledge_base_grants (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  granted_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX knowledge_base_grants_kb_user_idx
  ON knowledge_base_grants (kb_id, user_id);
CREATE INDEX knowledge_base_grants_tenant_user_idx
  ON knowledge_base_grants (tenant_id, user_id);
