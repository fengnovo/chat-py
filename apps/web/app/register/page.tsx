'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState, type FormEvent } from 'react';

import { useAuth } from '../components/auth/auth-context';
import { AuthLoading } from '../components/auth/auth-gate';
import { register } from '../components/resilient-chat/api';

export default function RegisterPage() {
  const { user, loading, refresh } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 已登录用户访问注册页时直接回首页。
  useEffect(() => {
    if (!loading && user) {
      router.replace('/');
    }
  }, [loading, user, router]);

  // 鉴权态未决时只显示加载屏；已登录则留空白帧等待 replace('/')。
  if (loading) return <AuthLoading />;
  if (user) return null;

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await register({
        username: username.trim(),
        displayName: displayName.trim(),
        password,
      });
      // 注册成功后端已直接下发会话 Cookie，刷新用户态后进首页（默认 member 角色）。
      await refresh();
      router.replace('/');
    } catch (caught) {
      const code = caught instanceof Error ? caught.message : '';
      if (code === 'username_taken') {
        setError('用户名已被占用，请换一个');
      } else if (code === 'signup_disabled') {
        setError('当前环境未开放自助注册，请联系管理员创建账号');
      } else if (code === 'invalid_request') {
        setError('用户名需为 3-64 位字母、数字或 ._-，密码至少 8 位');
      } else {
        setError('注册失败，请稍后重试');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1>注册账号</h1>
        <p className="login-subtitle">注册后默认为普通成员，知识库权限由管理员分配</p>
        <label htmlFor="register-username">用户名</label>
        <input
          id="register-username"
          name="username"
          autoComplete='username'
          placeholder='3-64 位字母、数字或 ._-'
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          minLength={3}
          maxLength={64}
          pattern='[A-Za-z0-9_.\-]{3,64}'
          title='3-64 位字母、数字或 ._-'
          required
        />
        <label htmlFor='register-display-name'>显示名称</label>
        <input
          id='register-display-name'
          name='displayName'
          autoComplete='name'
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          maxLength={120}
          required
        />
        <label htmlFor="register-password">密码</label>
        <input
          id="register-password"
          name="password"
          type="password"
          autoComplete="new-password"
          placeholder="至少 8 位"
          value={password}
          minLength={8}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
        {error && (
          <p className="login-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={submitting}>
          {submitting ? '注册中…' : '注册并进入'}
        </button>
        <p className="login-alt">
          已有账号？<Link href="/login">去登录</Link>
        </p>
      </form>
    </main>
  );
}
