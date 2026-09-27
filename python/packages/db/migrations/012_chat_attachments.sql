-- 聊天附件：用户在输入框选中后立即直传对象存储（发送前为 pending、run_id 为空），
-- 点发送创建 run 时关联到具体 run；历史会话据此还原图片/文件气泡。
-- 与 artifacts 的区别：artifacts 是 agent 产物，chat_attachments 是用户上传的输入。
CREATE TABLE IF NOT EXISTS chat_attachments (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id),
  run_id uuid REFERENCES agent_runs(id),
  object_key text NOT NULL,
  filename text NOT NULL,
  content_type text NOT NULL,
  size_bytes bigint NOT NULL,
  sha256 text NOT NULL,
  -- image：多模态视觉输入；text：解码后内联正文；file：run 启动前投进沙箱工作区
  kind text NOT NULL CHECK (kind IN ('image', 'text', 'file')),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready')),
  created_at timestamptz NOT NULL DEFAULT now(),
  uploaded_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS chat_attachments_tenant_object_key_idx
  ON chat_attachments (tenant_id, object_key);
CREATE INDEX IF NOT EXISTS chat_attachments_user_idx
  ON chat_attachments (tenant_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS chat_attachments_run_idx
  ON chat_attachments (run_id);
-- 孤儿清理：未关联 run 的附件（传完没发送 / 传到一半放弃）
CREATE INDEX IF NOT EXISTS chat_attachments_unlinked_idx
  ON chat_attachments (created_at)
  WHERE run_id IS NULL;
