import { Icon } from './icon';
import type { RunCapabilities } from '@/lib/contracts';
import type { PipelineEvent } from './types';

function TracePanel({
  inactive,
  open,
  onClose,
  trace,
  capabilities,
  contextInputTokens,
  generatedTokens,
  compressing,
}: {
  inactive: boolean;
  open: boolean;
  onClose: () => void;
  trace: PipelineEvent[];
  capabilities: RunCapabilities | null;
  contextInputTokens: number;
  generatedTokens: number;
  compressing: boolean;
}) {
  const formatTokens = (value: number) =>
    value >= 10_000 ? `${(value / 1000).toFixed(1)}k` : String(value);
  return (
    <>
      {open && !inactive && (
        <button
          className="trace-scrim"
          type="button"
          aria-label="关闭可靠性轨迹"
          onClick={onClose}
        />
      )}
      <aside
        aria-hidden={!open}
        className={`trace-panel ${open ? 'is-open' : ''}`}
        inert={!open || inactive}
      >
        <div className="trace-head">
          <div>
            <span className="eyebrow">LIVE OBSERVABILITY</span>
            <h2>Agent 运行轨迹</h2>
          </div>
          <button
            className="icon-button trace-close-button"
            type="button"
            aria-label="关闭可靠性轨迹"
            onClick={onClose}
          >
            <Icon name="x" />
          </button>
        </div>

        <div className="trace-summary">
          <div>
            <span>事件流</span>
            <strong>DURABLE SSE</strong>
          </div>
          <div>
            <span>任务执行</span>
            <strong>QUEUED</strong>
          </div>
        </div>

        <ol className="trace-list">
          {trace.map((item, index) => (
            <li className={`trace-item is-${item.status}`} key={item.id}>
              <div className="trace-line">
                <span className="trace-node">
                  {item.status === 'success' ? (
                    <Icon name="check" size={12} />
                  ) : item.status === 'error' || item.status === 'warning' ? (
                    <Icon name="triangle" size={12} />
                  ) : (
                    <span className="pulse-dot" />
                  )}
                </span>
                {index < trace.length - 1 && <span className="trace-rail" />}
              </div>
              <div>
                <div className="trace-title">
                  <strong>{item.title}</strong>
                  <time>{item.timestamp}</time>
                </div>
                <p>{item.detail}</p>
                <code>{item.stage.toUpperCase()}</code>
              </div>
            </li>
          ))}
        </ol>

        <div className="trace-capabilities">
          <section>
            <span className="nav-label">会话上下文</span>
            <div className="cap-grid">
              <div>
                <span>输入 tokens</span>
                <code>{formatTokens(contextInputTokens)}</code>
              </div>
              <div>
                <span>输出 tokens</span>
                <code>{formatTokens(generatedTokens)}</code>
              </div>
              <div>
                <span>压缩阈值</span>
                <code>
                  {capabilities
                    ? capabilities.contextTriggerTokens > 0
                      ? formatTokens(capabilities.contextTriggerTokens)
                      : '未启用'
                    : '--'}
                </code>
              </div>
              <div>
                <span>沙箱</span>
                <code>{capabilities?.backendMode ?? '--'}</code>
              </div>
              <div>
                <span>知识库</span>
                <code>
                  {capabilities
                    ? capabilities.knowledgeEnabled
                      ? '已关联'
                      : '未关联'
                    : '--'}
                </code>
              </div>
              {compressing && (
                <div className="cap-compressing">上下文压缩中…</div>
              )}
            </div>
          </section>
          <section>
            <span className="nav-label">可调用 MCP 工具</span>
            <div className="cap-chips">
              {(capabilities?.tools.length ? capabilities.tools : ['无']).map(
                (tool) => (
                  <code key={tool}>{tool}</code>
                ),
              )}
            </div>
          </section>
          <section>
            <span className="nav-label">Skills</span>
            <div className="cap-chips">
              {(capabilities?.skills.length ? capabilities.skills : ['无']).map(
                (skill) => (
                  <code key={skill}>{skill}</code>
                ),
              )}
            </div>
          </section>
        </div>
      </aside>
    </>
  );
}

export { TracePanel };
