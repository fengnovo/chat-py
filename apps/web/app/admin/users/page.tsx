'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState, type FormEvent } from 'react';

import {
  createAdminUser,
  deleteAdminUser,
  fetchKnowledgeBases,
  fetchUserKbGrants,
  listAdminUsers,
  replaceUserKbGrants,
  updateAdminUser,
  type AdminUser,
  type KnowledgeBase,
} from '../../components/resilient-chat/api';
import { Icon } from '../../components/resilient-chat/icon';
import { useAuth } from '../../components/auth/auth-context';
import { AuthGate } from '../../components/auth/auth-gate';
import { UserMenu } from '../../components/auth/user-menu';
import { scheduleMicrotask } from '../../lib/schedule-microtask';

const ROLE_OPTIONS: Array<{ value: AdminUser['role']; label: string }> = [
  { value: 'admin', label: '管理员' },
  { value: 'owner', label: '知识库拥有者' },
  { value: 'member', label: '普通成员' },
];

function AdminUsersView() {
  const { user } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [grantUser, setGrantUser] = useState<AdminUser | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);

  const refresh = useCallback(async () => {
    try {
      setUsers(await listAdminUsers());
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '加载用户失败');
    }
  }, []);

  useEffect(() => {
    scheduleMicrotask(() => { void refresh(); });
  }, [refresh]);

  if (user && user.role !== 'admin') {
    return (
      <main className="admin-page">
        <header className="page-nav">
          <Link href="/" className="page-nav-back" title="返回首页" aria-label="返回首页">
            <Icon name="arrow-left" size={16} />
            <span>返回</span>
          </Link>
          <h1 className="page-nav-title">用户管理</h1>
          <div className="page-nav-user"><UserMenu /></div>
        </header>
        <p role="alert">需要管理员权限。</p>
      </main>
    );
  }

  return (
    <main className="admin-page">
      <header className="page-nav">
        <Link href="/" className="page-nav-back" title="返回首页" aria-label="返回首页">
          <Icon name="arrow-left" size={16} />
          <span>返回</span>
        </Link>
        <h1 className="page-nav-title">用户管理</h1>
        <div className="page-nav-user"><UserMenu /></div>
      </header>
      {error && <p role="alert">{error}</p>}
      <div className="admin-toolbar">
        <button type="button" onClick={() => setShowCreate(true)}>创建用户</button>
        <button type="button" onClick={() => void refresh()}>刷新</button>
      </div>
      <table className="admin-user-table">
        <thead>
          <tr>
            <th>用户名</th>
            <th>显示名</th>
            <th>角色</th>
            <th>已授权知识库</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {users.map((item) => (
            <tr key={item.id}>
              <td>{item.username ?? '—'}</td>
              <td>{item.displayName}</td>
              <td>
                <select
                  value={item.role}
                  aria-label={`${item.displayName}的角色`}
                  onChange={(event) => {
                    const role = event.target.value as AdminUser['role'];
                    void updateAdminUser(item.id, { role })
                      .then(refresh)
                      .catch((caught: unknown) =>
                        setError(caught instanceof Error ? caught.message : '更新角色失败'),
                      );
                  }}
                >
                  {ROLE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </td>
              <td>{item.grantedKbCount}</td>
              <td>
                <div className="admin-user-actions">
                  <button type="button" onClick={() => setGrantUser(item)}>分配知识库</button>
                  <button
                    type="button"
                    className="admin-delete-btn"
                    disabled={item.id === user?.id}
                    title={item.id === user?.id ? '不能删除自己' : undefined}
                    onClick={() => setDeleteTarget(item)}
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {showCreate && (
        <CreateUserDialog
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            void refresh();
          }}
        />
      )}
      {grantUser && (
        <GrantKbDialog
          target={grantUser}
          onClose={() => setGrantUser(null)}
        />
      )}
      {deleteTarget && (
        <DeleteUserDialog
          target={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => {
            setDeleteTarget(null);
            void refresh();
          }}
        />
      )}
    </main>
  );
}

function CreateUserDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<AdminUser['role']>('member');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await createAdminUser({
        username: username.trim(),
        displayName: displayName.trim(),
        password,
        role,
      });
      onCreated();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '';
      setError(message === 'username_taken' ? '用户名已存在' : '创建用户失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="admin-dialog-backdrop" role="presentation" onClick={onClose}>
      <form
        className="admin-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="创建用户"
        onSubmit={handleSubmit}
        onClick={(event) => event.stopPropagation()}
      >
        <h2>创建用户</h2>
        <label htmlFor="create-username">用户名</label>
        <input
          id="create-username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          required
          minLength={3}
          maxLength={64}
        />
        <label htmlFor="create-display-name">显示名</label>
        <input
          id="create-display-name"
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          required
        />
        <label htmlFor="create-password">密码（至少 8 位）</label>
        <input
          id="create-password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          minLength={8}
        />
        <label htmlFor="create-role">角色</label>
        <select
          id="create-role"
          value={role}
          onChange={(event) => setRole(event.target.value as AdminUser['role'])}
        >
          {ROLE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        {error && <p role="alert" className="admin-dialog-error">{error}</p>}
        <div className="admin-dialog-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={submitting}>
            {submitting ? '创建中…' : '创建'}
          </button>
        </div>
      </form>
    </div>
  );
}

function GrantKbDialog({
  target,
  onClose,
}: {
  target: AdminUser;
  onClose: () => void;
}) {
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([fetchKnowledgeBases(), fetchUserKbGrants(target.id)])
      .then(([nextBases, granted]) => {
        if (cancelled) return;
        setBases(nextBases);
        setSelected(new Set(granted));
      })
      .catch(() => {
        if (!cancelled) setError('加载知识库授权失败');
      });
    return () => {
      cancelled = true;
    };
  }, [target.id]);

  const toggle = (kbId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(kbId)) next.delete(kbId);
      else next.add(kbId);
      return next;
    });
  };

  const handleSave = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await replaceUserKbGrants(target.id, [...selected]);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '保存授权失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="admin-dialog-backdrop" role="presentation" onClick={onClose}>
      <div
        className="admin-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`为${target.displayName}分配知识库`}
        onClick={(event) => event.stopPropagation()}
      >
        <h2>为 {target.displayName} 分配知识库</h2>
        {bases.length === 0 ? (
          <p>暂无可分配的知识库。</p>
        ) : (
          <ul className="admin-grant-list">
            {bases.map((base) => (
              <li key={base.id}>
                <label>
                  <input
                    type="checkbox"
                    checked={selected.has(base.id)}
                    onChange={() => toggle(base.id)}
                  />
                  <span>{base.name}</span>
                </label>
              </li>
            ))}
          </ul>
        )}
        {error && <p role="alert" className="admin-dialog-error">{error}</p>}
        <div className="admin-dialog-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="button" onClick={() => void handleSave()} disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

function DeleteUserDialog({
  target,
  onClose,
  onDeleted,
}: {
  target: AdminUser;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const handleConfirm = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteAdminUser(target.id);
      onDeleted();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '';
      setError(
        message === 'user_owns_knowledge_bases'
          ? '该用户拥有知识库，需先转移或删除其知识库后再删除用户'
          : message === 'cannot_delete_self'
            ? '不能删除自己'
            : '删除用户失败',
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="admin-dialog-backdrop" role="presentation" onClick={onClose}>
      <div
        className="admin-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`删除用户 ${target.displayName}`}
        onClick={(event) => event.stopPropagation()}
      >
        <h2>删除用户</h2>
        <p className="admin-delete-warning">
          确定要删除用户 <strong>{target.displayName}</strong> 吗？
        </p>
        <p className="admin-delete-hint">
          此操作将永久删除该用户的会话历史、运行记录及生成的文件，且不可恢复。
        </p>
        {error && <p role="alert" className="admin-dialog-error">{error}</p>}
        <div className="admin-dialog-actions">
          <button type="button" onClick={onClose} disabled={busy}>取消</button>
          <button
            type="button"
            className="admin-delete-confirm"
            onClick={() => void handleConfirm()}
            disabled={busy}
          >
            {busy ? '删除中…' : '确认删除'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function AdminUsersPage() {
  return (
    <AuthGate>
      <AdminUsersView />
    </AuthGate>
  );
}
