'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { fetchCurrentUser, type CurrentUser } from '../resilient-chat/api';

type AuthContextValue = {
  user: CurrentUser | null;
  loading: boolean;
  refresh: () => Promise<CurrentUser | null>;
};

const AuthContext = createContext<AuthContextValue>({
  user: null,
  loading: true,
  refresh: async () => null,
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    // 普通 fetch：401 时不做全局跳转（由调用方决定），避免登录页死循环。
    const next = await fetchCurrentUser().catch(() => null);
    setUser(next);
    setLoading(false);
    return next;
  }, []);

  useEffect(() => {
    // active 标志用于丢弃 StrictMode 重挂载/组件卸载后才 settle 的旧请求：
    // 被 abort 的首个请求不能把状态置成“未登录”，否则刷新时会出现
    // logo → 登录页一闪 → 首页 的闪烁链路。
    let active = true;
    const controller = new AbortController();
    void fetchCurrentUser(controller.signal)
      .then((next) => {
        if (active) setUser(next);
      })
      .catch((error) => {
        if (!active) return;
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setUser(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, []);

  const value = useMemo(() => ({ user, loading, refresh }), [user, loading, refresh]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}
