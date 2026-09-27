'use client';

import Image from 'next/image';
import { useRouter } from 'next/navigation';
import { useEffect, type ReactNode } from 'react';

import { useAuth } from './auth-context';

// 全屏 Logo 呼吸加载屏：鉴权态校验中、以及未登录跳转登录页的间隙统一使用，
// 避免白屏或登录表单闪现。
export function AuthLoading() {
  return (
    <div className="auth-gate-loading" role="status" aria-live="polite">
      <Image
        alt="正在加载"
        className="auth-gate-logo"
        height={150}
        priority
        src="/keen-ai-logo.png"
        width={150}
      />
    </div>
  );
}

// 未登录访问受保护页面时跳转登录页；/login 页面自身不包裹本组件。
export function AuthGate({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) {
      router.replace('/login');
    }
  }, [loading, user, router]);

  // user 未就绪（含跳转登录页的过渡帧）始终停留在加载屏，避免内容/白屏闪烁。
  if (loading || !user) return <AuthLoading />;
  return <>{children}</>;
}
