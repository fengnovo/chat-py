-- OAuth 社交登录账号关联：一个内部用户可绑定多个外部 OAuth 提供商。
-- provider + subject 唯一确定一个外部账号；同一 provider 下 subject 不可重复。

CREATE TABLE IF NOT EXISTS oauth_accounts (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider text NOT NULL CHECK (provider IN ('github', 'google')),
  subject text NOT NULL,
  email text,
  display_name text,
  avatar_url text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, subject)
);

CREATE INDEX IF NOT EXISTS oauth_accounts_user_idx
  ON oauth_accounts (user_id);
