'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';

import { resetUserData } from '@/app/lib/persistence';

import { changePassword, logout } from '../resilient-chat/api';
import { Icon } from '../resilient-chat/icon';
import { useAuth } from './auth-context';

const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  owner: '知识库拥有者',
  member: '成员',
};

const PASSWORD_ERRORS: Record<string, string> = {
  invalid_current_password: '当前密码不正确',
  weak_password: '新密码至少 8 位，且不能与旧密码相同',
  password_change_unavailable: '当前登录模式不支持修改密码',
};

// 修改密码弹窗：校验当前密码后设置新密码，成功后保留登录态直接关闭。
function ChangePasswordDialog({
  busy,
  error,
  onClose,
  onSubmit,
}: {
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (input: { currentPassword: string; newPassword: string }) => void;
}) {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [confirmError, setConfirmError] = useState<string | null>(null);

  const canSubmit =
    !busy &&
    currentPassword.length > 0 &&
    newPassword.length >= 8 &&
    confirmPassword === newPassword;

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (newPassword !== confirmPassword) {
      setConfirmError('两次输入的新密码不一致');
      return;
    }
    if (newPassword === currentPassword) {
      setConfirmError('新密码不能与当前密码相同');
      return;
    }
    setConfirmError(null);
    onSubmit({ currentPassword, newPassword });
  };

  return (
    <div className="modal-layer">
      <button
        aria-label="关闭修改密码窗口"
        className="modal-scrim"
        disabled={busy}
        type="button"
        onClick={onClose}
      />
      <section
        aria-labelledby="change-password-title"
        aria-modal="true"
        className="modal-card password-dialog"
        role="dialog"
      >
        <header className="modal-head">
          <div>
            <span>CHANGE PASSWORD</span>
            <h2 id="change-password-title">修改密码</h2>
          </div>
          <button
            aria-label="关闭"
            className="modal-close"
            disabled={busy}
            type="button"
            onClick={onClose}
          >
            <Icon name="x" size={18} />
          </button>
        </header>

        <form className="dialog-form" onSubmit={handleSubmit}>
          <label>
            当前密码
            <input
              autoFocus
              autoComplete="current-password"
              disabled={busy}
              type="password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              required
            />
          </label>
          <label>
            新密码
            <span>至少 8 位字符</span>
            <input
              autoComplete="new-password"
              disabled={busy}
              minLength={8}
              maxLength={200}
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              required
            />
          </label>
          <label>
            确认新密码
            <input
              autoComplete="new-password"
              disabled={busy}
              minLength={8}
              type="password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              required
            />
          </label>
          {(confirmError || error) && (
            <p className="dialog-error" role="alert">
              {confirmError ?? (error ? PASSWORD_ERRORS[error] ?? '修改失败，请稍后重试' : null)}
            </p>
          )}
          <div className="dialog-actions">
            <button className="secondary-action" disabled={busy} type="button" onClick={onClose}>
              取消
            </button>
            <button className="primary-action" disabled={!canSubmit} type="submit">
              {busy ? '正在保存…' : '确认修改'}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

// 右上角头像菜单：悬停/聚焦展开浮层，展示角色与导航，提供修改密码与退出登录。
// 触屏无 hover 时可点击头像切换；Esc 或点击外部关闭。
export function UserMenu({ onAiServiceClick }: { onAiServiceClick?: () => void } = {}) {
  const { user, refresh } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordSaved, setPasswordSaved] = useState(false);
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

  // 修改成功后短暂提示再关闭弹窗，给用户明确的完成反馈。
  useEffect(() => {
    if (!passwordSaved) return;
    const timer = window.setTimeout(() => {
      setPasswordSaved(false);
      setPasswordOpen(false);
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [passwordSaved]);

  if (!user) return null;

  // dev 模式下 API 对所有请求注入固定身份，Cookie 无意义，退出登录不可能生效。
  const isDevMode = user.authMode === 'dev';
  const roleLabel = ROLE_LABELS[user.role] ?? user.role;

  const handleLogout = async () => {
    await logout().catch(() => undefined);
    // 清掉当前用户在本机的会话列表/消息缓存，避免下个登录账号看到历史痕迹。
    resetUserData(user.id);
    await refresh();
    router.replace('/login');
  };

  const handleChangePassword = async (input: {
    currentPassword: string;
    newPassword: string;
  }) => {
    setPasswordBusy(true);
    setPasswordError(null);
    try {
      await changePassword(input);
      setPasswordSaved(true);
    } catch (caught) {
      setPasswordError(caught instanceof Error ? caught.message : 'failed');
    } finally {
      setPasswordBusy(false);
    }
  };

  // 有头像时渲染图片，无头像时回退到默认图标。
  const avatarSmall = user.avatarUrl ? (
    <img className="user-badge-avatar-img" src={user.avatarUrl} alt="" width={18} height={18} />
  ) : (
    <Icon name="user" size={18} />
  );
  const avatarLarge = user.avatarUrl ? (
    <img className="user-badge-avatar-img" src={user.avatarUrl} alt="" width={22} height={22} />
  ) : (
    <Icon name="user" size={22} />
  );

  return (
    <div className={`user-badge ${open ? 'is-open' : ''}`} ref={rootRef}>
      <button
        type="button"
        className="user-badge-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="账号菜单"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="user-badge-avatar" aria-hidden="true">
          {avatarSmall}
        </span>
        <span className="user-badge-name">{user.displayName}</span>
      </button>

      <div className="user-badge-popover" role="menu">
        <div className="user-badge-head">
          <span className="user-badge-avatar is-lg" aria-hidden="true">
            {avatarLarge}
          </span>
          <div className="user-badge-meta">
            <p className="user-badge-display-name">{user.displayName}</p>
            <span className={`user-badge-role is-${user.role}`}>{roleLabel}</span>
          </div>
        </div>

        <nav className="user-badge-links" aria-label="页面导航">
          <Link href="/" onClick={() => setOpen(false)}>
            <Icon name="home" size={15} />
            首页
          </Link>
          <Link href="/memory" onClick={() => setOpen(false)}>
            <Icon name="layers" size={15} />
            长期记忆
          </Link>
          {(user.role === 'admin' || user.role === 'owner') && (
            <Link href="/knowledge" onClick={() => setOpen(false)}>
              <Icon name="layers" size={15} />
              知识库
            </Link>
          )}
          {onAiServiceClick && (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onAiServiceClick();
              }}
            >
              <Icon name="support" size={15} />
              AI 客服
            </button>
          )}
          {user.role === 'admin' && (
            <Link href="/admin/users" onClick={() => setOpen(false)}>
              <Icon name="shield" size={15} />
              用户管理
            </Link>
          )}
          <Link href="https://logic-composer.keen-tech.top" target="_blank" rel="noopener noreferrer" onClick={() => setOpen(false)}>
              <Icon name="composer" size={15} />
              Workflow
          </Link>
          {!isDevMode && user.hasPassword && (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                setPasswordError(null);
                setPasswordSaved(false);
                setPasswordOpen(true);
              }}
            >
              <Icon name="lock" size={15} />
              修改密码
            </button>
          )}
        </nav>

        {isDevMode ? (
          <p className="user-badge-dev-hint" title="设置 AUTH_MODE=password 后可使用账号密码登录与退出">
            开发免登录模式（AUTH_MODE=dev）
          </p>
        ) : (
          <button
            type="button"
            className="user-badge-logout"
            role="menuitem"
            onClick={() => void handleLogout()}
          >
            <Icon name="logout" size={15} />
            退出登录
          </button>
        )}
      </div>

      {/* Portal 到 body：知识库页 .kc-steps-user 带 transform 会形成包含块，
          导致 fixed 定位的弹窗被挤成窄条，因此弹窗不能渲染在头像容器内。 */}
      {passwordOpen &&
        createPortal(
          passwordSaved ? (
            <div className="modal-layer">
              <section className="modal-card password-dialog" role="status">
                <p className="password-saved">
                  <Icon name="check" size={16} />
                  密码修改成功
                </p>
              </section>
            </div>
          ) : (
            <ChangePasswordDialog
              busy={passwordBusy}
              error={passwordError}
              onClose={() => setPasswordOpen(false)}
              onSubmit={(input) => void handleChangePassword(input)}
            />
          ),
          document.body,
        )}
    </div>
  );
}
