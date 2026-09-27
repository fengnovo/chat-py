-- 知识文档在知识库内的目录路径（不含文件名），用于拼接 chunk 内 md 相对图片引用得到 kb 级 rel_path。
-- 016 之前把这句 DDL 追加进了已应用的迁移，已落库的环境不会重跑 016，故单独作为一条迁移补齐。
-- 全程使用 IF NOT EXISTS，对「016 已带该列」的环境可安全跳过。
ALTER TABLE knowledge_documents
  ADD COLUMN IF NOT EXISTS directory text NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS knowledge_documents_directory_idx ON knowledge_documents (kb_id, directory) WHERE deleted_at IS NULL;
