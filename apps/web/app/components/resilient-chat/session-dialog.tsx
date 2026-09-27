import { useState } from 'react';

import { Icon } from './icon';
import type { SessionDialog as SessionDialogType } from './types';

function SessionActionDialog({
  busy,
  dialog,
  error,
  onClose,
  onDelete,
  onRename,
}: {
  busy: boolean;
  dialog: SessionDialogType;
  error: string | null;
  onClose: () => void;
  onDelete: () => void;
  onRename: (title: string) => void;
}) {
  const [title, setTitle] = useState(dialog.session.title);
  const deleting = dialog.kind === 'delete';

  return (
    <div className="modal-layer">
      <button
        aria-label="关闭会话操作窗口"
        className="modal-scrim"
        disabled={busy}
        type="button"
        onClick={onClose}
      />
      <section
        aria-labelledby="session-dialog-title"
        aria-modal="true"
        className="modal-card session-dialog"
        role="dialog"
      >
        <header className="modal-head">
          <div>
            <span>{deleting ? 'DELETE SESSION' : 'RENAME SESSION'}</span>
            <h2 id="session-dialog-title">
              {deleting ? '删除这条会话？' : '重命名会话'}
            </h2>
          </div>
          <button
            aria-label="关闭"
            autoFocus={deleting}
            className="modal-close"
            disabled={busy}
            type="button"
            onClick={onClose}
          >
            <Icon name="x" size={18} />
          </button>
        </header>

        {deleting ? (
          <p className="delete-copy">
            “{dialog.session.title}”的历史记录和对应工作区文件将被彻底删除，且无法恢复。正在运行的会话需要先停止。
          </p>
        ) : (
          <form
            className="dialog-form"
            onSubmit={(event) => {
              event.preventDefault();
              onRename(title.trim());
            }}
          >
            <label>
              会话名称
              <input
                autoFocus
                disabled={busy}
                maxLength={120}
                required
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
            <div className="dialog-actions">
              <button className="secondary-action" disabled={busy} type="button" onClick={onClose}>
                取消
              </button>
              <button className="primary-action" disabled={busy || !title.trim()} type="submit">
                {busy ? '正在保存…' : '保存'}
              </button>
            </div>
          </form>
        )}

        {deleting && (
          <div className="dialog-actions">
            <button className="secondary-action" disabled={busy} type="button" onClick={onClose}>
              取消
            </button>
            <button className="danger-action" disabled={busy} type="button" onClick={onDelete}>
              {busy ? '正在删除…' : '确认删除'}
            </button>
          </div>
        )}
        {error && <p className="dialog-error" role="alert">{error}</p>}
      </section>
    </div>
  );
}

export { SessionActionDialog };
