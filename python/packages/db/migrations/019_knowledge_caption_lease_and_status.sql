-- 修复 018 引入 caption 子系统时 schema 与仓库层不一致的两处问题：
--
-- 1. caption_status 的 CHECK 漏了 'queued'。但 KnowledgeRepository.enqueueCaptionJob
--    会把资产置为 caption_status='queued'，于是每次入队都在 UPDATE 阶段抛
--    check constraint violation，整个事务回滚 —— 任务行不落库、资产永远停在 'pending'。
--    API 侧只在 knowledge-routes.ts 里 catch 后打一条 'failed to enqueue caption job'，
--    不返回给前端，所以外部完全看不出失败。
--
-- 2. 租约令牌（lease token）此前只存在 worker 内存里。worker 重启后 captionLeases 为空，
--    completeCaptionJob / failCaptionJobWithRetry 一律返回 false，任务会卡在 'running'
--    直到租约过期才被对账循环捞回。补一列落库，让租约校验对重启与多进程都成立。
ALTER TABLE knowledge_assets
  DROP CONSTRAINT IF EXISTS knowledge_assets_caption_status_check;
ALTER TABLE knowledge_assets
  ADD CONSTRAINT knowledge_assets_caption_status_check
  CHECK (caption_status IN ('pending', 'queued', 'running', 'ready', 'failed', 'skipped', 'disabled'));

ALTER TABLE knowledge_caption_jobs
  ADD COLUMN IF NOT EXISTS lease_token uuid;
