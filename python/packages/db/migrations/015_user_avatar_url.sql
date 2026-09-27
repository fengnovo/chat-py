-- 用户头像：OAuth 登录时从第三方提供商同步头像 URL 到 users 表，
-- 供 /api/auth/me 等接口直接返回，避免每次查询 JOIN oauth_accounts。

ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url text;
