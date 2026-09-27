import type { UIMessage } from 'ai';

// 所有缓存都按用户 ID 隔离：同一浏览器切换账号（退出再登录 / 注册新号）时，
// 绝不能读到上一个用户的会话列表与消息内容。
const RUN_KEY_PREFIX = 'resilient-chat:last-run';
const SESSION_CACHE_KEY_PREFIX = 'resilient-chat:sessions';

function runStorageKey(userId: string) {
  return `${RUN_KEY_PREFIX}:${userId}`;
}

function sessionStorageKey(userId: string) {
  return `${SESSION_CACHE_KEY_PREFIX}:${userId}`;
}

export type PersistedRun = {
  chatId: string;
  runId: string;
  chunkIndex: number;
  messages: UIMessage[];
  pending: boolean;
};

export type SessionCache<T> = {
  data: T[];
  nextCursor: string | null;
};

export function readPersistedRun(userId: string): PersistedRun | null {
  try {
    const value = window.localStorage.getItem(runStorageKey(userId));
    if (!value) return null;
    const parsed = JSON.parse(value) as Partial<PersistedRun>;
    if (
      typeof parsed.chatId !== 'string' ||
      typeof parsed.runId !== 'string' ||
      typeof parsed.chunkIndex !== 'number' ||
      !Array.isArray(parsed.messages) ||
      typeof parsed.pending !== 'boolean'
    ) {
      return null;
    }
    return parsed as PersistedRun;
  } catch {
    return null;
  }
}

export function writePersistedRun(run: PersistedRun, userId: string) {
  window.localStorage.setItem(runStorageKey(userId), JSON.stringify(run)); // 聊天SSE收到的 data保存为chunkIndex
}

export function updatePersistedCursor(
  userId: string,
  runId: string,
  chunkIndex: number,
) {
  const current = readPersistedRun(userId);
  if (!current || current.runId !== runId) return;
  writePersistedRun({ ...current, chunkIndex }, userId);
}

export function clearPersistedRun(userId: string) {
  window.localStorage.removeItem(runStorageKey(userId));
}

// 会话列表在 SPA 内还有一份模块内存缓存（避免切页签反复拉取），同样按用户存放。
const memorySessionCache = new Map<string, SessionCache<unknown> | null>();

export function readSessionCache<T>(userId: string): SessionCache<T> | null {
  const cached = memorySessionCache.get(userId);
  if (cached !== undefined) return cached as SessionCache<T> | null;
  try {
    const value = window.sessionStorage.getItem(sessionStorageKey(userId));
    if (!value) {
      memorySessionCache.set(userId, null);
      return null;
    }
    const parsed = JSON.parse(value) as {
      data?: unknown;
      nextCursor?: unknown;
    };
    if (!Array.isArray(parsed.data)) {
      memorySessionCache.set(userId, null);
      return null;
    }
    const cache: SessionCache<T> = {
      data: parsed.data as T[],
      nextCursor:
        typeof parsed.nextCursor === 'string' ? parsed.nextCursor : null,
    };
    memorySessionCache.set(userId, cache);
    return cache;
  } catch {
    return null;
  }
}

export function writeSessionCache(
  cache: SessionCache<unknown>,
  userId: string,
) {
  memorySessionCache.set(userId, cache);
  try {
    window.sessionStorage.setItem(
      sessionStorageKey(userId),
      JSON.stringify(cache),
    );
  } catch {
    // 隐私模式或超出配额时忽略，下次挂载重新拉取即可
  }
}

// 退出登录时清掉当前用户留在本机的聊天痕迹（内存 + 存储）。
export function clearSessionCache(userId: string) {
  memorySessionCache.delete(userId);
  try {
    window.sessionStorage.removeItem(sessionStorageKey(userId));
  } catch {
    // ignore
  }
}

export function resetUserData(userId: string) {
  clearPersistedRun(userId);
  clearSessionCache(userId);
}
