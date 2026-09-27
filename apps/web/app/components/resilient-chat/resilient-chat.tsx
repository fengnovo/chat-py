'use client';

import { useSyncExternalStore } from 'react';

import { AppSkeleton, ChatRuntime } from './chat-runtime';

export function ResilientChat() {
  const isClient = useSyncExternalStore(
    () => () => undefined,
    () => true,
    () => false,
  );

  if (!isClient) return <AppSkeleton />;

  return <ChatRuntime />;
}
