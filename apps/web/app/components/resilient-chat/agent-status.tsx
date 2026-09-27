import { useEffect, useRef, useState } from 'react';

import { Icon } from './icon';
import type {
  AgentActivityEntry,
  AgentStatus,
  SubagentCard,
  SubagentReview,
} from './types';
import { formatDuration, toolCallSummary, toolDetailText } from './utils';

/** 过程区固定高度，超出滚动；避免长任务的旁白把页面撑爆。 */
const BODY_MAX_HEIGHT = 200;
const VISIBLE_ENTRIES = 40;
/** 单条工具输出在面板里的展示上限。 */
const MAX_OUTPUT_CHARS = 1_200;

function ToolEntryBody({ entry }: { entry: Extract<AgentActivityEntry, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false);
  const summary = toolCallSummary(entry.tool, entry.input);
  const output = toolDetailText(entry.output).trim();
  const detail = output.length > MAX_OUTPUT_CHARS ? `${output.slice(0, MAX_OUTPUT_CHARS)}…` : output;
  const expandable = Boolean(summary || detail);

  return (
    <div className={`process-tool is-${entry.phase}`}>
      <button
        aria-expanded={open}
        className="process-tool-head"
        disabled={!expandable}
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        <Icon name={entry.phase === 'start' ? 'wrench' : 'check'} size={13} />
        <span className="process-tool-name">{entry.tool}</span>
        <span className="process-tool-state">
          {entry.phase === 'start' ? '请求调用' : '完成'}
        </span>
        {expandable && <Icon name="chevron" size={13} />}
      </button>
      {open && expandable && (
        <div className="process-tool-detail">
          {summary && <code className="process-tool-cmd">{summary}</code>}
          {detail && <pre className="process-tool-output">{detail}</pre>}
        </div>
      )}
    </div>
  );
}

function ProcessEntry({ entry }: { entry: AgentActivityEntry }) {
  if (entry.kind === 'narration') {
    return <p className="process-narration">{entry.text}</p>;
  }
  return <ToolEntryBody entry={entry} />;
}

/** 单轮评审结论块：评分 + 逐条验收项 + 整改意见。 */
function ReviewBlock({ review }: { review: SubagentReview }) {
  const missed = review.checklist.filter((item) => !item.met);
  return (
    <div className={`subagent-review is-${review.passed ? 'passed' : 'failed'}`}>
      <p className="subagent-review-head">
        <span className="subagent-review-round">第 {review.attempt} 轮评审</span>
        <span className={`subagent-review-badge is-${review.passed ? 'passed' : 'failed'}`}>
          {review.passed ? `通过 · ${review.score} 分` : `未达标 · ${review.score} 分`}
        </span>
      </p>
      {review.checklist.length > 0 && (
        <ul className="subagent-review-list">
          {review.checklist.map((item, index) => (
            <li key={index} className={item.met ? 'is-met' : 'is-missed'}>
              <Icon name={item.met ? 'check' : 'x'} size={11} />
              <span>{item.item}</span>
            </li>
          ))}
        </ul>
      )}
      {!review.passed && review.feedback && (
        <p className="subagent-review-feedback">整改意见：{review.feedback}</p>
      )}
      {!review.passed && missed.length === 0 && !review.feedback && (
        <p className="subagent-review-feedback">评审器判定未达标，需整改重派。</p>
      )}
    </div>
  );
}

/** 子 Agent 卡片：角色名/状态/耗时/工具调用数，展开看摘要与各轮评审。 */
function SubagentCardView({ card }: { card: SubagentCard }) {
  const [open, setOpen] = useState(false);
  const running = card.status === 'running';
  const latestReview = card.reviews[card.reviews.length - 1] ?? null;
  const retryInFlight = running && card.attempt > 1;
  const exhausted = !running && latestReview !== null && !latestReview.passed;
  const stateText = running
    ? retryInFlight
      ? `整改重派中 · 第 ${card.attempt} 轮`
      : card.background
        ? '后台运行中'
        : '运行中'
    : card.status === 'completed'
      ? latestReview?.passed === false
        ? '已完成（评审未达标）'
        : '已完成'
      : card.status === 'timeout'
        ? '已超时'
        : '失败';
  const expandable = running
    ? Boolean(card.description) || card.reviews.length > 0
    : Boolean(card.summary) || card.reviews.length > 0;

  return (
    <div
      className={`subagent-card is-${card.status}${exhausted ? ' is-review-exhausted' : ''}${
        retryInFlight ? ' is-retrying' : ''
      }`}
    >
      <button
        aria-expanded={open}
        className="subagent-card-head"
        disabled={!expandable}
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        {running ? (
          <span className="activity-spinner" aria-hidden="true" />
        ) : (
          <Icon
            name={card.status === 'completed' && !exhausted ? 'check' : 'triangle'}
            size={13}
          />
        )}
        <span className="subagent-card-role">{card.role}</span>
        {card.background && <span className="subagent-card-bg">后台</span>}
        {card.attempt > 1 && <span className="subagent-card-attempt">第 {card.attempt} 轮</span>}
        <span className="subagent-card-state">{stateText}</span>
        {latestReview?.passed && !running && (
          <span className="subagent-card-meta">评审 {latestReview.score} 分</span>
        )}
        {card.durationMs !== null && (
          <span className="subagent-card-meta">
            {formatDuration(Math.round(card.durationMs / 1_000))}
          </span>
        )}
        {card.toolCalls > 0 && (
          <span className="subagent-card-meta">{card.toolCalls} 次工具调用</span>
        )}
        {expandable && <Icon name="chevron" size={13} />}
      </button>
      {open && expandable && (
        <div className="subagent-card-body">
          {card.reviews.map((review) => (
            <ReviewBlock key={review.attempt} review={review} />
          ))}
          <p className="subagent-card-summary">{card.summary ?? card.description}</p>
        </div>
      )}
    </div>
  );
}

function AgentStatusPanel({
  busy,
  status,
  subagents,
}: {
  busy: boolean;
  status: AgentStatus;
  /** 当前 run 折叠出的子 Agent 卡片；有卡片时即使没有工具日志也显示面板。 */
  subagents: SubagentCard[];
}) {
  const [open, setOpen] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const visible = status.entries.slice(-VISIBLE_ENTRIES);
  // 过程记录更新时自动滚动到底部，让用户看到最新动作。
  useEffect(() => {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [status.entries.length, open]);
  // 只有真的产生了过程记录（工具调用/旁白）或存在子 Agent 卡片才显示，避免空面板。
  if (visible.length === 0 && subagents.length === 0) return null;

  // 有工具调用才算「执行日志」；只有思考旁白时就是「正在思考」。
  const hasToolCalls = status.entries.some((entry) => entry.kind === 'tool');
  const title = busy
    ? status.runningTool
      ? `正在执行 ${status.runningTool}`
      : hasToolCalls
        ? '正在执行'
        : '正在思考'
    : hasToolCalls
      ? '执行日志'
      : '思考过程';

  // 标题栏中间固定展示最新一条日志内容（单行省略）：
  // 工具行与展开列表里的文案保持一致，旁白则直接显示原文。
  const latestEntry = status.entries[status.entries.length - 1];
  let latestLine: string | null = null;
  if (latestEntry) {
    if (latestEntry.kind === 'narration') {
      latestLine = latestEntry.text;
    } else {
      const stateText = latestEntry.phase === 'start' ? '请求调用' : '完成';
      const summary = toolCallSummary(latestEntry.tool, latestEntry.input);
      latestLine = summary
        ? `${latestEntry.tool} ${stateText} · ${summary}`
        : `${latestEntry.tool} ${stateText}`;
    }
  }

  return (
    <section className="agent-process" aria-label="Agent 执行过程">
      <button
        aria-expanded={open}
        className="agent-process-head"
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        {busy ? (
          <span className="activity-spinner" aria-hidden="true" />
        ) : (
          <Icon name={hasToolCalls ? 'wrench' : 'check'} size={15} />
        )}
        <span className="agent-process-title">{title}</span>
        {/* 运行中：中间实时显示最新一条日志；结束后状态字消失，
            用等宽占位把时间/收起始终顶在最右，标题栏不发生跳动 */}
        {busy && latestLine ? (
          <span className="agent-process-live" title={latestLine}>
            {latestLine}
          </span>
        ) : (
          <span className="agent-process-spacer" aria-hidden="true" />
        )}
        {status.elapsedSeconds > 0 && (
          <span className="agent-process-time">· 本轮 {formatDuration(status.elapsedSeconds)}</span>
        )}
        <span className="agent-process-toggle">
          {open ? '收起' : '展开'}
          <Icon name="chevron" size={13} />
        </span>
      </button>
      {/* 子 Agent 卡片常驻在标题栏下方：运行中也能直接看到进度，无需先展开。 */}
      {subagents.length > 0 && (
        <div className="subagent-cards">
          {subagents.map((card) => (
            <SubagentCardView card={card} key={card.subagentId} />
          ))}
        </div>
      )}
      {open && (
        <div className="agent-process-body" ref={bodyRef} style={{ maxHeight: BODY_MAX_HEIGHT }}>
          {visible.map((entry) => (
            <ProcessEntry entry={entry} key={entry.id} />
          ))}
        </div>
      )}
    </section>
  );
}

export { AgentStatusPanel };
