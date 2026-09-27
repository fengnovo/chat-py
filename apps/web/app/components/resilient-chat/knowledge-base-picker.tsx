'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { Icon } from './icon';

export type KnowledgeBase = { id: string; name: string; status?: string };

type StorageLike = Pick<Storage, 'getItem' | 'setItem'> | Map<string, string>;

function storageGet(storage: StorageLike, key: string) {
  return storage instanceof Map ? storage.get(key) ?? null : storage.getItem(key);
}
function storageSet(storage: StorageLike, key: string, value: string) {
  if (storage instanceof Map) storage.set(key, value);
  else storage.setItem(key, value);
}

function storageKey(chatId: string) {
  return `knowledge-bases:${chatId}`;
}

function toggleKnowledgeBase(ids: string[], id: string) {
  return ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id];
}

/**
 * 读取某个会话已保存的知识库选择。
 * 返回 null 表示从未设置过（调用方应按“默认全选”处理），空数组表示用户主动清空。
 */
function knowledgeBaseIdsForChat(chatId: string, storage?: StorageLike): string[] | null {
  if (!storage && typeof window === 'undefined') return null;
  const source = storage ?? window.localStorage;
  const raw = storageGet(source, storageKey(chatId));
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((id): id is string => typeof id === 'string')
      : null;
  } catch {
    return null;
  }
}

function persistKnowledgeBaseIds(
  chatId: string,
  ids: string[],
  storage?: StorageLike,
) {
  if (!storage && typeof window === 'undefined') return;
  storageSet(storage ?? window.localStorage, storageKey(chatId), JSON.stringify(ids));
}

/**
 * 输入框内的知识库选择按钮：点击上浮出多选弹层（交互对齐右上角头像菜单），
 * 列出当前用户角色可访问的知识库，支持全选/清空，底部提供管理入口。
 */
function KnowledgeBaseMenu({
  bases,
  value,
  onToggle,
  onChangeAll,
}: {
  bases: KnowledgeBase[];
  value: string[];
  onToggle: (id: string) => void;
  onChangeAll: (ids: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const allSelected = bases.length > 0 && value.length === bases.length;

  return (
    <div className={`kb-menu ${open ? 'is-open' : ''}`} ref={rootRef}>
      <button
        className="composer-icon-button"
        type="button"
        aria-label="选择知识库"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <Icon name="layers" size={18} />
      </button>

      <div className="kb-menu-popover" role="menu">
        <div className="kb-menu-head">
          <strong>关联知识库</strong>
          <button
            type="button"
            className="kb-menu-all"
            disabled={bases.length === 0}
            onClick={() =>
              onChangeAll(allSelected ? [] : bases.map((base) => base.id))
            }
          >
            {allSelected ? '清空' : '全选'}
          </button>
        </div>
        <div className="kb-menu-list">
          {bases.length === 0 ? (
            <p className="kb-menu-empty">暂无可访问的知识库</p>
          ) : (
            bases.map((base) => (
              <label key={base.id} className="kb-menu-item">
                <input
                  type="checkbox"
                  checked={value.includes(base.id)}
                  onChange={() => onToggle(base.id)}
                />
                <span className="kb-menu-name" title={base.name}>
                  {base.name}
                </span>
                {base.status && base.status !== 'ready' && (
                  <span className="kb-menu-status">{base.status}</span>
                )}
              </label>
            ))
          )}
        </div>
        <Link className="kb-menu-manage" href="/knowledge" onClick={() => setOpen(false)}>
          <Icon name="folder" size={14} />
          管理知识库
        </Link>
      </div>
    </div>
  );
}

export {
  KnowledgeBaseMenu,
  knowledgeBaseIdsForChat,
  persistKnowledgeBaseIds,
  toggleKnowledgeBase,
};
