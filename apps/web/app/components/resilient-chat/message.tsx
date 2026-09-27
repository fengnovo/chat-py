import { AIBoundary } from '@cognicatch/react';
import {
  isValidElement,
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';

import { triggerAttachmentDownload } from './lightbox';
import { Icon } from './icon';
import { messageText } from './utils';
import type { AgentTodo, InsightCard, ResilientMessage } from './types';
import { CitationList, getKnowledgeAssetContentUrl } from './citation-list';

/** MarkdownContent 解析相对路径图片所需的最小引用结构（完整 Citation 可结构化赋值）。 */
type CitationImageSource = {
  kbId: string;
  images?: Array<{ assetId: string; relPath: string }>;
};

// Mermaid 仅在客户端动态加载，避免 SSR 报错与首屏体积膨胀。
let mermaidInitialized = false;
type MermaidApi = {
  initialize: (config: Record<string, unknown>) => void;
  render: (id: string, text: string) => Promise<{ svg: string }>;
};

async function getMermaid(): Promise<MermaidApi | null> {
  if (typeof window === 'undefined') return null;
  try {
    const mod = await import('mermaid');
    const mermaid = (mod.default ?? mod) as unknown as MermaidApi;
    if (!mermaidInitialized) {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'default',
        securityLevel: 'loose',
        fontFamily: 'inherit',
      });
      mermaidInitialized = true;
    }
    return mermaid;
  } catch {
    return null;
  }
}

function MermaidDiagram({
  chart,
  onPreview,
}: {
  chart: string;
  /** 传入后图表可点击，弹出与图片一致的浮层进行缩放查看。 */
  onPreview?: (svg: string) => void;
}) {
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMermaid().then((mermaid) => {
      if (!mermaid || cancelled) return;
      const id = `mermaid-${Math.random().toString(36).slice(2, 9)}`;
      mermaid
        .render(id, chart)
        .then(({ svg: rendered }) => {
          if (!cancelled) {
            setSvg(rendered);
            setError(null);
          }
        })
        .catch((err: unknown) => {
          if (!cancelled) {
            setError(err instanceof Error ? err.message : '图表演染失败');
            setSvg('');
          }
        });
    });
    return () => {
      cancelled = true;
    };
  }, [chart]);

  if (error) {
    return (
      <div className="mermaid-error" role="alert">
        <strong>图表解析失败：</strong>
        {error}
      </div>
    );
  }
  if (!svg) {
    return <div className="mermaid-loading" aria-live="polite">正在渲染图表…</div>;
  }
  const zoomable = Boolean(onPreview);
  const openPreview = () => onPreview?.(svg);

  return (
    <div
      className={`mermaid-container${zoomable ? ' is-zoomable' : ''}`}
      dangerouslySetInnerHTML={{ __html: svg }}
      role={zoomable ? 'button' : undefined}
      tabIndex={zoomable ? 0 : undefined}
      title={zoomable ? '点击放大查看图表' : undefined}
      aria-label={zoomable ? '放大查看 Mermaid 图表' : undefined}
      onClick={zoomable ? openPreview : undefined}
      onKeyDown={
        zoomable
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openPreview();
              }
            }
          : undefined
      }
    />
  );
}

const Message = memo(function Message({
  copied,
  dismissedCards,
  header,
  highlight,
  liveLabel,
  message,
  onBoundaryError,
  onCopy,
  onDismissCard,
  onFileLinkClick,
  onPreviewPage,
  onPreviewDiagram,
  onPreviewImage,
  reasoning,
  runTokens,
  showWaitingDots,
  streaming,
}: {
  copied: boolean;
  dismissedCards: Set<string>;
  /** 本轮执行面板：放在正文上方、与消息正文同列（不高于头像）。 */
  header?: ReactNode;
  /** 是否启用代码语法高亮与 Mermaid 图表渲染。 */
  highlight: boolean;
  /** 流式生成但还没有正文时，气泡内实时展示的当前动作/思考。 */
  liveLabel: string | null;
  message: ResilientMessage;
  onBoundaryError: () => void;
  onCopy: (id: string, text: string) => Promise<void>;
  onDismissCard: (id: string) => void;
  /** 点击聊天正文中引用的文件路径链接时，打开文件面板并定位到该文件。 */
  onFileLinkClick?: (path: string) => void;
  /** 点击页面预览按钮时，打开文件面板的构建预览。 */
  onPreviewPage?: () => void;
  /** 点击 Mermaid 图表时弹出浮层，支持缩放查看。 */
  onPreviewDiagram: (svg: string) => void;
  /** 点击聊天图片时在当前页弹出大图（输入框缩略图与历史消息共用）。 */
  onPreviewImage: (url: string, filename?: string) => void;
  /** 模型思考过程（reasoning_content），按 runId 独立存储，持久化保留。 */
  reasoning: string;
  /** 本轮运行结束后的 token 用量，展示在最后一条 assistant 消息的复制按钮后。 */
  runTokens: number;
  /** 执行面板可见时不再重复显示三点 loading（面板头部自带 spinner）。 */
  showWaitingDots: boolean;
  streaming: boolean;
}) {
  const text = messageText(message);
  const isUser = message.role === 'user';
  const cards = message.parts.filter((part) => part.type === 'data-card');
  const fileParts = isUser
    ? message.parts.filter((part) => part.type === 'file')
    : [];
  const citations = message.parts.filter((part) => part.type === 'data-citations').flatMap((part) => {
    const data = part.data as { citations?: import('./types').Citation[] };
    return data.citations ?? [];
  });
  // 思考过程自动滚动到底部
  const thinkingBodyRef = useRef<HTMLDivElement>(null);
  const [thinkingExpanded, setThinkingExpanded] = useState(false);
  useEffect(() => {
    if (thinkingBodyRef.current) {
      thinkingBodyRef.current.scrollTop = thinkingBodyRef.current.scrollHeight;
    }
  }, [reasoning]);

  return (
    <article className={`message-row ${isUser ? 'is-user' : 'is-assistant'}`}>
      <div className="avatar">
        <Icon name={isUser ? 'user' : 'shield'} size={17} />
      </div>
      <div className="message-body">
        {!isUser && message.metadata?.model && (
          <div className="message-meta">
            <span>{message.metadata.model}</span>
          </div>
        )}
        {!isUser && reasoning && (
          <div className={`thinking-chain ${thinkingExpanded ? 'is-expanded' : ''}`}>
            <button
              type="button"
              className="thinking-chain-header"
              onClick={() => setThinkingExpanded((v) => !v)}
            >
              <span className="thinking-chain-bullet" />
              <span className="thinking-chain-title">
                {text ? '思考过程' : '思考中'}
                {!text && <StreamingDots />}
              </span>
              <span className="thinking-chain-toggle">
                {thinkingExpanded ? '收起' : '展开'}
              </span>
            </button>
            <div className="thinking-chain-body" ref={thinkingBodyRef}>
              <div className="thinking-chain-content">{reasoning}</div>
            </div>
          </div>
        )}
        {header}
        {isUser && fileParts.length > 0 && (
          <div className="message-attachments">
            {fileParts.map((part, index) =>
              part.mediaType.startsWith('image/') ? (
                <button
                  type="button"
                  className="message-attachment-image"
                  key={`${message.id}-file-${index}`}
                  onClick={() => onPreviewImage(part.url, part.filename)}
                  title={part.filename ? `查看大图：${part.filename}` : '查看大图'}
                >
                  <img alt={part.filename ?? '聊天图片'} src={part.url} />
                </button>
              ) : (
                <button
                  type="button"
                  className="message-attachment-file"
                  key={`${message.id}-file-${index}`}
                  title={part.filename ? `下载附件：${part.filename}` : '下载附件'}
                  onClick={async () => {
                    try {
                      await triggerAttachmentDownload(part.url, part.filename);
                    } catch {
                      /* 下载失败静默；用户可重试或点击浏览器 Network 排查 401/404 */
                    }
                  }}
                >
                  <Icon name="paperclip" size={14} />
                  {part.filename ?? '附件'}
                </button>
              ),
            )}
          </div>
        )}
        <div className={`message-copy ${isUser ? '' : 'markdown-content'}`}>
          {text ? (
            isUser ? (
              text
            ) : (
              <MarkdownContent
                content={text}
                citations={citations}
                onFileLinkClick={onFileLinkClick}
                onPreviewPage={onPreviewPage}
                onPreviewDiagram={onPreviewDiagram}
                onPreviewImage={onPreviewImage}
                highlight={highlight}
              />
            )
          ) : streaming && showWaitingDots ? (
            <span className="streaming-live">
              <StreamingDots />
              {liveLabel && (
                <span className="streaming-live-text">{liveLabel}</span>
              )}
            </span>
          ) : (
            !isUser && !streaming && (
              <p className="message-empty">本轮没有返回任何内容，可以重新发送这条消息。</p>
            )
          )}
        </div>

        {!isUser && !dismissedCards.has(message.id) &&
          cards.map((part, index) =>
            part.data ? (
              <AIBoundary
                key={`${message.id}-card-${index}`}
                mode="manual"
                title="AI 组件渲染失败"
                description="模型返回了无效的组件结构，已安全降级；聊天内容不受影响。"
                rawPayload={part.data}
                showRawData={false}
                onError={onBoundaryError}
                onReset={() => onDismissCard(message.id)}
              >
                <GeneratedInsightCard data={part.data} />
              </AIBoundary>
            ) : null,
          )}

        {!isUser && <CitationList citations={citations} onPreviewImage={onPreviewImage} />}

        {!isUser && text && !streaming && (
          <div className="message-actions">
            <button
              type="button"
              onClick={() => void onCopy(message.id, text)}
            >
              <Icon name={copied ? 'check' : 'copy'} size={14} />
              {copied ? '已复制' : '复制'}
            </button>
            {runTokens > 0 && (
              <span className="message-tokens">
                <span className="tokens-dot" aria-hidden="true" />
                本次运行已生成约 {runTokens.toLocaleString('zh-CN')} tokens
              </span>
            )}
          </div>
        )}
      </div>
    </article>
  );
});

function GeneratedInsightCard({ data }: { data: InsightCard }) {
  if (data.kind !== 'reliability-summary') {
    return null;
  }

  return (
    <section className="insight-card" aria-label={data.title}>
      <div>
        <span>{data.eyebrow}</span>
        <h3>{data.title}</h3>
        <p>{data.body}</p>
      </div>
      <div className="insight-metric">
        <strong>{data.metric}</strong>
        <small>{data.metric_label}</small>
      </div>
    </section>
  );
}

/**
 * 校验大模型返回的链接是否安全：只允许 http/https/mailto 协议，
 * 拒绝 javascript:/data:/vbscript: 等可执行或可注入的危险协议。
 * 通过后才渲染为可点击链接，否则降级为纯文本。
 */
function safeExternalUrl(href: string | undefined): string | null {
  if (!href) return null;
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:') {
    return href;
  }
  return null;
}

/**
 * 判断 href 是否指向一个本地文件路径（AI 生成的文件）。
 * 排除带协议的 URL 与危险协议后，包含扩展名或路径分隔符即视为文件路径。
 */
function fileLinkPath(href: string | undefined): string | null {
  if (!href) return null;
  const trimmed = href.trim();
  if (!trimmed) return null;
  // 带协议的 URL 不是文件路径
  if (/^[a-zA-Z][a-zA-Z\d+.-]*:/.test(trimmed)) return null;
  // 锚点或纯查询不算文件
  if (trimmed.startsWith('#')) return null;
  // 至少包含扩展名（.xxx）或路径分隔符，才认为是文件路径引用
  const hasExt = /\.[A-Za-z\d]{1,10}$/.test(trimmed);
  const hasSep = trimmed.includes('/') || trimmed.includes('\\');
  if (!hasExt && !hasSep) return null;
  // 去掉前导的 ./ 和 /，统一成相对路径形式用于匹配 touchedFiles
  const normalized = trimmed.replace(/^(\.\/|\/)+/, '');
  return normalized || null;
}

/**
 * 带复制按钮的代码块：右上角悬浮一个复制图标，点击后把代码文本写入剪贴板，
 * 短暂显示对勾反馈。pre 本身保持可滚动与键盘聚焦能力。
 */
function CodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);

  async function handleCopy() {
    const text = preRef.current?.innerText ?? '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_400);
    } catch {
      /* 剪贴板不可用时静默 */
    }
  }

  return (
    <div className="code-block">
      <button
        type="button"
        className="code-copy-btn"
        onClick={() => void handleCopy()}
        title={copied ? '已复制' : '复制代码'}
        aria-label={copied ? '已复制' : '复制代码'}
      >
        <Icon name={copied ? 'check' : 'copy'} size={13} />
      </button>
      <pre ref={preRef} tabIndex={0}>
        {children}
      </pre>
    </div>
  );
}

function MarkdownContent({
  content,
  citations,
  onFileLinkClick,
  onPreviewPage,
  onPreviewDiagram,
  onPreviewImage,
  highlight,
}: {
  content: string;
  /** 本条消息的引用来源：用于把模型照抄的相对路径图片解析回可访问的代理地址。 */
  citations?: CitationImageSource[];
  /** 点击正文里的文件路径链接时触发，由父组件打开文件面板并定位。 */
  onFileLinkClick?: (path: string) => void;
  /** 点击页面预览按钮时触发，由父组件打开文件面板的构建预览。 */
  onPreviewPage?: () => void;
  onPreviewDiagram?: (svg: string) => void;
  onPreviewImage: (url: string, filename?: string) => void;
  /** 开启后启用代码语法高亮与 Mermaid 图表渲染；关闭则退化为纯文本代码块。 */
  highlight: boolean;
}) {
  // 检测内容中是否包含预览链接模式（markdown 格式或纯文本）
  const previewLinkRegex = /\[?📺?\s*打开页面预览\]?\(preview:\/\/open\)|📺\s*打开页面预览|打开页面预览|preview_page/;
  const hasPreviewLink = onPreviewPage && previewLinkRegex.test(content);

  // 引用图片索引：basename → 代理地址。模型偶尔会把知识库原文里的相对路径
  // （如 `./000.jpg`）照抄进回答，直接渲染必然 404；这里按文件名映射回资产代理地址。
  const citationImageMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const citation of citations ?? []) {
      for (const image of citation.images ?? []) {
        const base = image.relPath.split('/').pop()?.toLowerCase();
        if (base && !map.has(base)) {
          map.set(base, getKnowledgeAssetContentUrl(citation.kbId, image.assetId));
        }
      }
    }
    return map;
  }, [citations]);

  const markdownComponents = useMemo<Components>(() => {
    const resolveImageSrc = (src: string): string | null => {
      if (src.startsWith('/') || /^(https?:|data:|blob:)/i.test(src)) return src;
      const base = src.split('?')[0]?.split('#')[0].split('/').pop()?.toLowerCase() ?? '';
      return citationImageMap.get(base) ?? null;
    };
    return {
      a: ({ children, href }) => {
        const filePath = fileLinkPath(href);
        if (filePath && onFileLinkClick) {
          return (
            <button
              type="button"
              className="file-link"
              onClick={() => onFileLinkClick(filePath)}
              title={`在文件浏览器中定位：${filePath}`}
            >
              {children}
            </button>
          );
        }
        const safe = safeExternalUrl(href);
        if (!safe) return <span>{children}</span>;
        return (
          <a href={safe} rel="noopener noreferrer" target="_blank">
            {children}
          </a>
        );
      },
      code: ({ className, children }) => {
        if (highlight && className && /language-mermaid/i.test(className)) {
          const chart = String(children).replace(/\n$/, '');
          return <MermaidDiagram chart={chart} onPreview={onPreviewDiagram} />;
        }
        return <code className={className}>{children}</code>;
      },
      pre: ({ children }) => {
        const child = Array.isArray(children) ? children[0] : children;
        const codeProps = isValidElement(child)
          ? (child.props as { className?: string; children?: unknown })
          : null;
        if (
          highlight &&
          codeProps?.className &&
          /language-mermaid/i.test(codeProps.className)
        ) {
          const chart = String(codeProps.children ?? '').replace(/\n$/, '');
          return <MermaidDiagram chart={chart} onPreview={onPreviewDiagram} />;
        }
        return <CodeBlock>{children}</CodeBlock>;
      },
      img: ({ src, alt }) => {
        if (!src) return null;
        const url = typeof src === 'string' ? resolveImageSrc(src) : URL.createObjectURL(src);
        // 相对路径且映射不到任何引用图片：说明文档引用的图片从未上传，渲染占位符，
        // 绝不发 <img> 请求——死链 404 会被 lazy loading 在流式重排时反复重放。
        if (!url) {
          return (
            <span className="markdown-image-missing" title="原文引用的图片未上传到知识库">
              🖼️ {alt || '图片缺失'}
            </span>
          );
        }
        return (
          <img
            src={url}
            alt={alt ?? ''}
            loading="lazy"
            onClick={() => onPreviewImage(url, alt ?? undefined)}
          />
        );
      },
    };
  }, [citationImageMap, highlight, onFileLinkClick, onPreviewDiagram, onPreviewImage]);

  if (hasPreviewLink) {
    // 分割内容：预览链接之前、之后
    const parts = content.split(previewLinkRegex);
    return (
      <>
        {parts[0] && (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={highlight ? [rehypeHighlight] : []}
            components={markdownComponents}
          >
            {parts[0]}
          </ReactMarkdown>
        )}
        <button
          type="button"
          className="preview-page-link"
          onClick={onPreviewPage}
        >
          📺 打开页面预览
        </button>
        {parts[1] && (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={highlight ? [rehypeHighlight] : []}
            components={markdownComponents}
          >
            {parts[1]}
          </ReactMarkdown>
        )}
      </>
    );
  }

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={highlight ? [rehypeHighlight] : []}
      components={markdownComponents}
    >
      {content}
    </ReactMarkdown>
  );
}

function StreamingDots() {
  return (
    <span className="streaming-dots" role="status">
      <span className="sr-only">正在生成</span>
      <i aria-hidden="true" />
      <i aria-hidden="true" />
      <i aria-hidden="true" />
    </span>
  );
}

function ThinkingRow({ children }: { children?: ReactNode }) {
  return (
    <article className="message-row is-assistant thinking-row">
      <div className="avatar">
        <Icon name="shield" size={17} />
      </div>
      <div className="message-body">
        {children ?? <StreamingDots />}
      </div>
    </article>
  );
}

function AgentTodoList({ todos }: { todos: AgentTodo[] }) {
  const [open, setOpen] = useState(false);
  const completed = todos.filter((todo) => todo.status === 'completed').length;
  const active = todos.find((todo) => todo.status === 'in_progress');

  return (
    <section className="task-bar" aria-label="Agent 任务计划">
      <button
        aria-expanded={open}
        className="task-bar-head"
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        <Icon name="list" size={15} />
        <span className="task-bar-count">
          {completed}/{todos.length} 个任务已完成
        </span>
        {active && !open && (
          <span className="task-bar-current">{active.content}</span>
        )}
        <Icon name="chevron" size={13} />
      </button>
      {open && (
        <ol className="task-bar-list">
          {todos.map((todo, index) => (
            <li className={`is-${todo.status}`} key={`${index}-${todo.content}`}>
              <span>
                {todo.status === 'completed' ? (
                  <Icon name="check" size={12} />
                ) : todo.status === 'in_progress' ? (
                  <span className="pulse-dot" />
                ) : (
                  index + 1
                )}
              </span>
              {todo.content}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export {
  AgentTodoList,
  GeneratedInsightCard,
  MarkdownContent,
  Message,
  StreamingDots,
  ThinkingRow,
};
