-- 知识库上传接入 Wasm 哈希 + gzip + 分片：
-- 与 chat 附件一致，DB 同时记录原始大小/哈希（业务展示与去重键）和实际存储的大小/哈希（服务端校验对象用）。

ALTER TABLE knowledge_documents
  ADD COLUMN IF NOT EXISTS stored_size_bytes bigint,
  ADD COLUMN IF NOT EXISTS stored_sha256 text,
  ADD COLUMN IF NOT EXISTS content_encoding text,
  ADD COLUMN IF NOT EXISTS upload_id text;

ALTER TABLE knowledge_assets
  ADD COLUMN IF NOT EXISTS stored_size_bytes bigint,
  ADD COLUMN IF NOT EXISTS stored_sha256 text,
  ADD COLUMN IF NOT EXISTS content_encoding text,
  ADD COLUMN IF NOT EXISTS upload_id text,
  -- 资源表没有文档那样的 status 字段，用上传完成时间区分“对象已存在”（秒传可复用）与“上传被放弃”（需重新预签名）。
  ADD COLUMN IF NOT EXISTS uploaded_at timestamptz;
