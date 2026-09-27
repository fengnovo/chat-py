-- 附件秒传（租户内去重）与透明压缩支持。
-- 1) 同一对象键允许被多个附件行引用：去重命中时新行复用旧行的对象，
--    因此把 (tenant_id, object_key) 的唯一索引降级为普通索引。
DROP INDEX IF EXISTS chat_attachments_tenant_object_key_idx;
CREATE INDEX IF NOT EXISTS chat_attachments_tenant_object_key_idx
  ON chat_attachments (tenant_id, object_key);

-- 2) 原始内容哈希：用于秒传/去重（与对象存储字节的 sha256 区分，
--    gzip 压缩后存储字节哈希会变化，但同一原文的内容哈希恒定）。
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS content_sha256 text;
UPDATE chat_attachments SET content_sha256 = sha256 WHERE content_sha256 IS NULL;
ALTER TABLE chat_attachments ALTER COLUMN content_sha256 SET NOT NULL;

-- 3) 对象存储内容编码：当前仅可能为 'gzip'，NULL 表示原文直存。
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS content_encoding text
  CHECK (content_encoding IS NULL OR content_encoding IN ('gzip'));

-- 4) S3/MinIO 分片上传 ID：用于失败/取消时 AbortMultipartUpload。
ALTER TABLE chat_attachments ADD COLUMN IF NOT EXISTS upload_id text;

-- 5) 秒传查询索引：仅在已就绪且已关联 run 的附件中去重，
--    保证复用的对象不会被源用户删除（关联 run 后不允许删除）。
CREATE INDEX IF NOT EXISTS chat_attachments_dedup_idx
  ON chat_attachments (tenant_id, content_sha256)
  WHERE status = 'ready' AND run_id IS NOT NULL;
