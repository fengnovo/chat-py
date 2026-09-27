-- 一次性数据修复脚本（手动跑）。
-- 把所有 size_bytes < 1024（基本就是 git-lfs pointer）且 caption_status 未到终态的资产
-- 标成 skipped。
--
-- 跑之前建议先确认数量：
--   SELECT caption_status, count(*)
--   FROM knowledge_assets WHERE deleted_at IS NULL AND size_bytes < 1024
--   GROUP BY 1 ORDER BY 1;
--
-- 预期：pending ≈ 324，queued/running/failed ≈ 0。
-- 跑完之后再跑一次验证：
--   SELECT caption_status, count(*) FROM knowledge_assets
--   WHERE deleted_at IS NULL AND size_bytes < 1024 GROUP BY 1;
-- 预期：只剩 skipped。
--
-- 为什么不删：保留行便于事后审计 rel_path/上传人/时间，且 LFS pointer 的 object 体积极小、
-- 不会真的撑爆存储。
UPDATE knowledge_assets
SET caption_status   = 'skipped',
    caption_model    = 'system',
    caption_error    = 'pre_orphan_backfill: size_bytes < 1024 (likely git-lfs pointer)',
    caption_updated_at = now(),
    updated_at       = now()
WHERE deleted_at IS NULL
  AND size_bytes < 1024
  AND caption_status IN ('pending', 'queued', 'running', 'failed');