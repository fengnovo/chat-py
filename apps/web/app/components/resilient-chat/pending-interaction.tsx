import { useState } from 'react';

import type { PendingInterrupt, QuestionAnswer } from './types';

function PendingInteraction({
  busy,
  error,
  interrupt,
  onApproval,
  onQuestion,
}: {
  busy: boolean;
  error: string | null;
  interrupt: PendingInterrupt;
  onApproval: (
    decision: 'approve' | 'reject',
    scope: 'once' | 'session',
  ) => Promise<void>;
  onQuestion: (answer: QuestionAnswer) => Promise<void>;
}) {
  const [selected, setSelected] = useState<number[]>([]);
  const [customText, setCustomText] = useState('');
  const [approvalIntent, setApprovalIntent] = useState<
    'reject' | 'once' | 'session' | null
  >(null);

  async function submitApproval(
    decision: 'approve' | 'reject',
    scope: 'once' | 'session',
    intent: 'reject' | 'once' | 'session',
  ) {
    setApprovalIntent(intent);
    await onApproval(decision, scope);
    setApprovalIntent(null);
  }

  if (interrupt.type === 'approval.required') {
    return (
      <section className="agent-interrupt" aria-label="等待操作审批">
        <div className="agent-panel-head">
          <h3>Agent 准备执行以下操作</h3>
          <strong>{interrupt.actions.length} 项</strong>
        </div>
        <ul>
          {interrupt.actions.map((action, index) => (
            <li key={`${index}-${action.name}`}>
              <code>{action.name}</code>
              <span>{action.summary}</span>
            </li>
          ))}
        </ul>
        {error && <p className="interaction-error">{error}</p>}
        <div className="interaction-actions">
          <button
            className="secondary-action"
            disabled={busy}
            type="button"
            onClick={() => void submitApproval('reject', 'once', 'reject')}
          >
            {busy && approvalIntent === 'reject' ? '正在拒绝…' : '拒绝'}
          </button>
          <button
            className="primary-action"
            disabled={busy}
            type="button"
            onClick={() => void submitApproval('approve', 'once', 'once')}
          >
            {busy && approvalIntent === 'once' ? '正在提交…' : '仅批准这一次'}
          </button>
          <button
            className="session-action"
            disabled={busy}
            title="本会话后续的写文件、删除、命令和 MCP 操作将自动执行"
            type="button"
            onClick={() => void submitApproval('approve', 'session', 'session')}
          >
            {busy && approvalIntent === 'session'
              ? '正在开启…'
              : '本会话都允许'}
          </button>
        </div>
      </section>
    );
  }

  const canSubmit = selected.length > 0 || customText.trim().length > 0;

  return (
    <section className="agent-interrupt" aria-label="等待问题回答">
      <div className="agent-panel-head">
        <span className="eyebrow">INPUT REQUIRED</span>
        <strong>{interrupt.question.multiple ? '可多选' : '单选'}</strong>
      </div>
      <h3>{interrupt.question.question}</h3>
      <div className="question-options">
        {interrupt.question.options.map((option, index) => {
          const active = selected.includes(index);
          return (
            <button
              aria-pressed={active}
              className={active ? 'is-selected' : ''}
              disabled={busy}
              key={`${index}-${option.label}`}
              type="button"
              onClick={() => {
                setSelected((current) =>
                  interrupt.question.multiple
                    ? current.includes(index)
                      ? current.filter((item) => item !== index)
                      : [...current, index]
                    : [index],
                );
              }}
            >
              <strong>{option.label}</strong>
              {option.description && <small>{option.description}</small>}
            </button>
          );
        })}
      </div>
      {interrupt.question.allowCustom && (
        <input
          className="custom-answer"
          disabled={busy}
          placeholder="或者输入自定义答案"
          value={customText}
          onChange={(event) => setCustomText(event.target.value)}
        />
      )}
      {error && <p className="interaction-error">{error}</p>}
      <div className="interaction-actions">
        <button
          className="primary-action"
          disabled={busy || !canSubmit}
          type="button"
          onClick={() => {
            const answer: QuestionAnswer = {
              selections: selected.map((index) => ({
                index,
                label: interrupt.question.options[index]?.label ?? '',
              })),
              ...(customText.trim() ? { customText: customText.trim() } : {}),
            };
            void onQuestion(answer);
          }}
        >
          {busy ? '正在提交…' : '提交并继续'}
        </button>
      </div>
    </section>
  );
}

export { PendingInteraction };
