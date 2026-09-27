'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { AuthGate } from '../components/auth/auth-gate';
import { UserMenu } from '../components/auth/user-menu';
import { apiFetch } from '../components/resilient-chat/api';
import { Icon } from '../components/resilient-chat/icon';
import { scheduleMicrotask } from '../lib/schedule-microtask';

type Memory = { id: string; kind: string; scope: string; content: string; importance: number; confidence: number; updatedAt: string };

function MemoryPageContent() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState('');

  const load = async () => {
    setLoading(true);
    try {
      const response = await apiFetch('/api/agent/memories?limit=200');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setMemories(((await response.json()) as { data: Memory[] }).data);
    } catch {
      setError('长期记忆加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { scheduleMicrotask(() => { void load(); }); }, []);

  const remove = async (id: string) => {
    await apiFetch(`/api/agent/memories/${encodeURIComponent(id)}`, { method: 'DELETE' });
    setMemories((items) => items.filter((item) => item.id !== id));
  };

  const clear = async () => {
    if (!window.confirm('确定清空全部长期记忆吗？')) return;
    await apiFetch('/api/agent/memories?assistantKey=chat', { method: 'DELETE' });
    setMemories([]);
  };

  const save = async (id: string) => {
    const response = await apiFetch(`/api/agent/memories/${encodeURIComponent(id)}`, { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ content: draft }) });
    if (!response.ok) return;
    const updated = (await response.json()) as Memory;
    setMemories((items) => items.map((item) => item.id === id ? updated : item));
    setEditing(null);
  };

  return (
    <main className="memory-page">
      <header className="page-nav">
        <Link href="/" className="page-nav-back" title="返回首页" aria-label="返回首页">
          <Icon name="arrow-left" size={16} />
          <span>返回</span>
        </Link>
        <h1 className="page-nav-title">长期记忆</h1>
        <div className="page-nav-user"><UserMenu /></div>
      </header>
      <section className="memory-card">
        <div className="memory-card-head"><strong>已保存的信息</strong><button type="button" onClick={() => void clear()} disabled={memories.length === 0}>全部清空</button></div>
        {loading && <p className="memory-muted">加载中…</p>}
        {error && <p className="memory-error">{error}</p>}
        {!loading && !error && memories.length === 0 && <p className="memory-muted">暂无已确认的长期记忆。</p>}
        <div className="memory-list">{memories.map((memory) => <article className="memory-row" key={memory.id}><div className="memory-row-main"><span className="memory-kind">{memory.kind}</span>{editing === memory.id ? <div className="memory-edit"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} /><div><button type="button" onClick={() => void save(memory.id)}>保存</button><button type="button" onClick={() => setEditing(null)}>取消</button></div></div> : <><p>{memory.content}</p><small>{memory.scope === 'global' ? '全局' : memory.scope} · 信心度 {Math.round(memory.confidence * 100)}%</small></>}</div><div className="memory-row-actions">{editing !== memory.id && <button type="button" onClick={() => { setEditing(memory.id); setDraft(memory.content); }}>编辑</button>}<button type="button" aria-label="删除记忆" onClick={() => void remove(memory.id)}>删除</button></div></article>)}</div>
      </section>
    </main>
  );
}

export default function MemoryPage() { return <AuthGate><MemoryPageContent /></AuthGate>; }
