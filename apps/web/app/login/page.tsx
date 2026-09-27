'use client';

import Link from 'next/link';
import Image from 'next/image';
import { useRouter, useSearchParams } from 'next/navigation';
import { useEffect, useState, Suspense, type FormEvent, type ReactNode } from 'react';

import { useAuth } from '../components/auth/auth-context';
import { AuthLoading } from '../components/auth/auth-gate';
import { login, fetchOAuthProviders, getOAuthLoginUrl } from '../components/resilient-chat/api';

const OAUTH_ERROR_MESSAGES: Record<string, string> = {
  oauth_denied: '授权已取消，请重试',
  oauth_missing_params: '登录参数缺失，请重试',
  oauth_state_invalid: '安全校验失败，请重新登录',
  oauth_failed: '第三方登录失败，请稍后重试',
};

// GitHub / Google 品牌 SVG 图标（内联避免额外依赖）。
function GitHubIcon() {
  return (
    <svg viewBox="0 0 16 16" width="20" height="20" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

function GoogleIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1Z" fill="#4285F4" />
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23Z" fill="#34A853" />
      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62Z" fill="#FBBC05" />
      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53Z" fill="#EA4335" />
    </svg>
  );
}

const OAUTH_ICONS: Record<string, () => ReactNode> = {
  github: GitHubIcon,
  google: GoogleIcon,
};

const OAUTH_LABELS: Record<string, string> = {
  github: 'GitHub',
  google: 'Google',
};

export default function LoginPage() {
  return (
    <Suspense fallback={<AuthLoading />}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const { user, loading, refresh } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [oauthProviders, setOAuthProviders] = useState<string[]>([]);

  // 已登录用户访问登录页时直接回首页。
  useEffect(() => {
    if (!loading && user) {
      router.replace('/');
    }
  }, [loading, user, router]);

  // 加载可用的 OAuth 提供商。
  useEffect(() => {
    fetchOAuthProviders().then(setOAuthProviders);
  }, []);

  // 处理 OAuth 回调错误；从 URL 派生，避免首帧后再写入表单错误状态。
  const oauthError = searchParams.get('error');
  const oauthDetail = searchParams.get('detail');
  const oauthErrorMessage = oauthError
    ? `${OAUTH_ERROR_MESSAGES[oauthError] ?? '登录失败，请稍后重试'}${oauthDetail ? `（${oauthDetail}）` : ''}`
    : null;
  const visibleError = error ?? oauthErrorMessage;

  // 鉴权态未决时只显示加载屏；已登录则留空白帧等待 replace('/')，
  // 避免刷新过程中登录表单一闪而过。
  if (loading) return <AuthLoading />;
  if (user) return null;

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(username.trim(), password);
      await refresh();
      router.replace('/');
    } catch (caught) {
      setError(
        caught instanceof Error && caught.message === 'invalid_credentials'
          ? '用户名或密码错误'
          : '登录失败，请稍后重试',
      );
    } finally {
      setSubmitting(false);
    }
  };

  const hasOAuth = oauthProviders.length > 0;

  return (
    <main className="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="login-brand">
          <Image
            alt="Keen Agent"
            className="login-logo"
            height={64}
            priority
            src="/keen-ai-logo.png"
            width={64}
          />
          <h1>登录</h1>
        </div>

        {/* OAuth 社交登录按钮 */}
        {hasOAuth && (
          <div className="oauth-buttons">
            {oauthProviders.map((provider) => {
              const IconComponent = OAUTH_ICONS[provider];
              return (
                <a
                  key={provider}
                  className={`oauth-btn oauth-btn-${provider}`}
                  href={getOAuthLoginUrl(provider)}
                >
                  {IconComponent && <IconComponent />}
                  <span>使用 {OAUTH_LABELS[provider] ?? provider} 登录</span>
                </a>
              );
            })}
          </div>
        )}

        {/* 分隔线 */}
        {hasOAuth && (
          <div className="login-divider">
            <span>或使用账号密码</span>
          </div>
        )}

        <label htmlFor="login-username">用户名</label>
        <input
          id="login-username"
          name="username"
          autoComplete="username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          required
        />
        <label htmlFor="login-password">密码</label>
        <input
          id="login-password"
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
        {visibleError && (
          <p className="login-error" role="alert">
            {visibleError}
          </p>
        )}
        <button type="submit" disabled={submitting}>
          {submitting ? '登录中…' : '登录'}
        </button>
        <p className="login-alt">
          没有账号？<Link href="/register">立即注册</Link>
        </p>
      </form>
    </main>
  );
}
