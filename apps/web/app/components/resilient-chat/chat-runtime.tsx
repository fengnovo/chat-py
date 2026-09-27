import { useChat } from '@ai-sdk/react';
import { WorkflowChatTransport } from '@ai-sdk/workflow';
import type { RunCapabilities } from '@/lib/contracts';
import type { FileUIPart } from 'ai';
import Link from 'next/link';
import {
  type CSSProperties,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';

import {
  clearPersistedRun,
  readPersistedRun,
  readSessionCache,
  writePersistedRun,
  writeSessionCache,
} from '@/app/lib/persistence';
import { ResilientSession } from '@/app/lib/session';
import { scheduleMicrotask } from '@/app/lib/schedule-microtask';

import { AgentStatusPanel } from './agent-status';
import {
  fetchKnowledgeBases,
  fetchSessionFiles,
  fetchSessionPage,
  responseError,
} from './api';
import { Composer } from './composer';
import { Lightbox, type LightboxImage } from './lightbox';
import { initialTrace } from './constants';
import { agentEventToTrace, createTrackedFetch, localEvent } from './events';
import { TaskFailureNotice, friendlyError } from './failure-notice';
import { DEFAULT_FILES_WIDTH, FilePanel } from './file-panel';
import type { TouchedFile } from './file-panel';
import { Icon } from './icon';
import { Message, ThinkingRow } from './message';
import {
  knowledgeBaseIdsForChat,
  persistKnowledgeBaseIds,
  toggleKnowledgeBase,
} from './knowledge-base-picker';
import { UserMenu } from '../auth/user-menu';
import { AiServiceWidget } from '../ai-service-widget';
import { useAuth } from '../auth/auth-context';
import { apiFetch } from './api';
import { PendingInteraction } from './pending-interaction';
import { SessionActionDialog } from './session-dialog';
import { Sidebar } from './sidebar';
import { TracePanel } from './trace-panel';
import type {
  AgentActivityEntry,
  AgentActivityState,
  AgentStatus,
  AgentTodo,
  ConversationSeed,
  PendingInterrupt,
  PipelineEvent,
  QuestionAnswer,
  ResilientMessage,
  RunSummary,
  SessionDialog,
  SessionHistory,
  SubagentCard,
  TaskFailure,
  WebSessionSummary,
} from './types';
import {
  deriveAgentActivity,
  failureFromRun,
  foldSubagentCard,
  isPendingStatus,
  messageText,
  messagesFromHistory,
  subagentCardsFromMessages,
} from './utils';

const emptyActivity: AgentActivityState = {
  entries: [],
  startedAt: null,
  lastEventAt: null,
};

/**
 * 「继续对话」续跑时，前端仍需通过 useChat 发出一条 user 消息来驱动请求，
 * 但它只是占位：界面不渲染、结束后从 store 移除，真正发给模型的是服务端
 * 合成的内部续跑指令（见 API 的 CONTINUATION_INSTRUCTION）。
 */
const CONTINUATION_PLACEHOLDER = '继续';

function AppSkeleton() {
  return (
    <main className='app-shell is-loading' aria-label='正在加载可靠聊天'>
      <aside className='sidebar skeleton-panel' />
      <section className='chat-column'>
        <div className='topbar skeleton-line' />
        <div className='skeleton-center'>
          <div className='skeleton-orb' />
          <div className='skeleton-copy' />
          <div className='skeleton-copy short' />
        </div>
      </section>
    </main>
  );
}

function ChatRuntime() {
  // ChatRuntime 只在 AuthGate 内渲染，user 一定存在。所有本地缓存都按该
  // 用户 ID 隔离，避免同一浏览器切换账号后看到上一个用户的会话与消息。
  const { user } = useAuth();
  const userId = user!.id;
  const [conversation, setConversation] = useState<ConversationSeed>(() => {
    const persisted = readPersistedRun(userId);
    return {
      chatId: persisted?.chatId ?? crypto.randomUUID(),
      messages: (persisted?.messages ?? []) as ResilientMessage[],
      // 先不触发 resume：等后端确认这次运行仍在进行中，再挂上事件流
      resumeRun: null,
    };
  });
  const [input, setInput] = useState('');
  const [trace, setTrace] = useState<PipelineEvent[]>(initialTrace);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [dismissedCards, setDismissedCards] = useState<Set<string>>(
    () => new Set(),
  );
  const [copiedMessage, setCopiedMessage] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sidebarHidden, setSidebarHidden] = useState(false);
  const [traceOpen, setTraceOpen] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const [filesWidth, setFilesWidth] = useState(DEFAULT_FILES_WIDTH);
  const [filesWide, setFilesWide] = useState(false);
  /** 文件面板中当前选中的文件路径；从聊天消息的文件链接跳转时由外部设置。 */
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  /** 外部触发 FilePanel 打开构建预览的计数器，每次 +1 触发 useEffect。 */
  const [openBuildPreviewCount, setOpenBuildPreviewCount] = useState(0);
  const [pendingInterrupt, setPendingInterrupt] =
    useState<PendingInterrupt | null>(null);
  const [agentTodos, setAgentTodos] = useState<AgentTodo[]>([]);
  // 子 Agent 卡片：从持久化消息的 data-subagent parts 恢复（历史接口不回传，见方案偏差）。
  const [subagentCards, setSubagentCards] = useState<SubagentCard[]>(() =>
    subagentCardsFromMessages(conversation.messages),
  );
  const [activity, setActivity] = useState<AgentActivityState>(emptyActivity);
  const [historyFiles, setHistoryFiles] = useState<TouchedFile[]>([]);
  const [generatedTokens, setGeneratedTokens] = useState(0);
  /** 本次运行累计输入 tokens（近似当前上下文占用），随 usage.updated 累加。 */
  const [contextInputTokens, setContextInputTokens] = useState(0);
  /** run.started 携带的运行时能力快照：可调用 MCP 工具、skills 与上下文配置。 */
  const [runCapabilities, setRunCapabilities] =
    useState<RunCapabilities | null>(null);
  /** 模型思考过程，按 runId 独立存储，避免被 AI SDK 流式 text-delta 更新覆盖。 */
  const [reasoningByRunId, setReasoningByRunId] = useState<Map<string, string>>(
    new Map(),
  );
  const [activityClock, setActivityClock] = useState(() => Date.now());
  const [interactionBusy, setInteractionBusy] = useState(false);
  const [interactionError, setInteractionError] = useState<string | null>(null);
  const [runFailure, setRunFailure] = useState<TaskFailure | null>(null);
  /** 上下文压缩进行中：deepagents 摘要阶段显示流光指示器，不展示摘要正文。 */
  const [contextCompressing, setContextCompressing] = useState(false);
  /** 续跑占位 user 消息的 id 集合：运行期间隐藏，run 结束后从 store 中移除。 */
  const [hiddenContinuationIds, setHiddenContinuationIds] = useState<
    ReadonlySet<string>
  >(new Set());
  const [lightboxImage, setLightboxImage] = useState<LightboxImage | null>(
    null,
  );
  const [restoredSessions] = useState(() =>
    readSessionCache<WebSessionSummary>(userId),
  );
  const [sessions, setSessions] = useState<WebSessionSummary[]>(
    () => restoredSessions?.data ?? [],
  );
  const [sessionsLoaded, setSessionsLoaded] = useState(
    () => restoredSessions !== null,
  );
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [sessionsNextCursor, setSessionsNextCursor] = useState<string | null>(
    () => restoredSessions?.nextCursor ?? null,
  );
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const [switchingSessionId, setSwitchingSessionId] = useState<string | null>(
    null,
  );
  const [creatingSession, setCreatingSession] = useState(false);
  const [sessionDialog, setSessionDialog] = useState<SessionDialog | null>(
    null,
  );
  const [sessionDialogBusy, setSessionDialogBusy] = useState(false);
  const [sessionDialogError, setSessionDialogError] = useState<string | null>(
    null,
  );
  const [sessionMenuId, setSessionMenuId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [aiServiceOpen, setAiServiceOpen] = useState(false);
  // Markdown 渲染增强开关：语法高亮 + Mermaid 图表，默认开启；持久化到 localStorage。
  const [highlightMarkdown, setHighlightMarkdown] = useState<boolean>(() => {
    if (typeof window === 'undefined') return true;
    try {
      const stored = window.localStorage.getItem('chat:highlight-markdown');
      return stored === null ? true : stored !== 'false';
    } catch {
      return true;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(
        'chat:highlight-markdown',
        highlightMarkdown ? 'true' : 'false',
      );
    } catch {
      /* localStorage 不可用时静默，仅本会话生效 */
    }
  }, [highlightMarkdown]);
  const [knowledgeBases, setKnowledgeBases] = useState<
    Array<{ id: string; name: string; status?: string }>
  >([]);
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState<string[]>(() =>
    typeof window === 'undefined'
      ? []
      : (knowledgeBaseIdsForChat(conversation.chatId) ?? []),
  );
  const sessionRef = useRef(new ResilientSession());
  const conversationRef = useRef<HTMLDivElement>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  // 用户是否停在会话底部附近：在底部时新内容自动跟随；主动上滑查看时不打断。
  const stickToBottomRef = useRef(true);
  const mobileMenuButtonRef = useRef<HTMLButtonElement>(null);
  const sidebarWasOpenRef = useRef(false);
  const dialogReturnFocusRef = useRef<HTMLElement | null>(null);

  // 浏览器会把切换路由/切到其他应用时被丢弃的请求以 TypeError
  // 抛出，AI SDK 会把它当成连接错误并停在 error 状态。这里只在页面
  // 重新可见且当前运行确实还在进行时，才决定是否恢复；否则切个会话
  // 或切到别的页面再回来，任务状态就被误判为“已终止”。
  const isPageVisible = useSyncExternalStore(
    (notify) => {
      const handler = () => notify();
      document.addEventListener('visibilitychange', handler);
      window.addEventListener('focus', handler);
      window.addEventListener('pageshow', handler);
      return () => {
        document.removeEventListener('visibilitychange', handler);
        window.removeEventListener('focus', handler);
        window.removeEventListener('pageshow', handler);
      };
    },
    () => document.visibilityState === 'visible',
    () => true,
  );

  const refreshSessions = useCallback(async () => {
    try {
      const page = await fetchSessionPage();
      setSessions(page.data);
      setSessionsNextCursor(page.nextCursor);
      setSessionsError(null);
      writeSessionCache(
        { data: page.data, nextCursor: page.nextCursor },
        userId,
      );
    } catch {
      setSessionsError('历史记录加载失败');
    } finally {
      setSessionsLoaded(true);
    }
  }, [userId]);
  useEffect(() => {
    void fetchKnowledgeBases()
      .then(setKnowledgeBases)
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    scheduleMicrotask(() => {
      setKnowledgeBaseIds(knowledgeBaseIdsForChat(conversation.chatId) ?? []);
    });
  }, [conversation.chatId]);
  // 知识库列表首次加载后，如果该会话从未做过选择，则默认全选并记住；
  // 用户之后主动清空（保存为 []）不会再被覆盖。
  useEffect(() => {
    if (knowledgeBases.length === 0) return;
    if (knowledgeBaseIdsForChat(conversation.chatId) !== null) return;
    const allIds = knowledgeBases.map((base) => base.id);
    persistKnowledgeBaseIds(conversation.chatId, allIds);
    scheduleMicrotask(() => setKnowledgeBaseIds(allIds));
  }, [knowledgeBases, conversation.chatId]);

  const handleToggleKnowledgeBase = useCallback(
    (id: string) => {
      setKnowledgeBaseIds((current) => {
        const next = toggleKnowledgeBase(current, id);
        persistKnowledgeBaseIds(conversation.chatId, next);
        return next;
      });
    },
    [conversation.chatId],
  );

  const handleChangeKnowledgeBases = useCallback(
    (ids: string[]) => {
      persistKnowledgeBaseIds(conversation.chatId, ids);
      setKnowledgeBaseIds(ids);
    },
    [conversation.chatId],
  );

  // 当前会话的历史文件记录。运行结束后会重新拉取（refreshHistoryFiles），
  // 否则新一轮 run 一开始清空实时工具记录时，上一轮生成的文件会从面板里消失。
  const sessionsRef = useRef(sessions);
  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);
  const refreshHistoryFiles = useCallback(async () => {
    const match = sessionsRef.current.find(
      (session) => session.externalKey === conversation.chatId,
    );
    if (!match) return;
    try {
      const files = await fetchSessionFiles(match.id);
      setHistoryFiles(
        files.map((file) => ({
          path: file.path,
          content: file.content,
          operation: file.operation as TouchedFile['operation'],
        })),
      );
    } catch {
      // 历史文件拉取失败不影响聊天主流程
    }
  }, [conversation.chatId]);

  // 首次加载（或切换会话）时拉取一次历史文件。
  const historyFilesChatIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!sessionsLoaded) return;
    if (historyFilesChatIdRef.current === conversation.chatId) return;
    const match = sessions.find(
      (session) => session.externalKey === conversation.chatId,
    );
    if (!match) return;
    historyFilesChatIdRef.current = conversation.chatId;
    void refreshHistoryFiles();
  }, [sessionsLoaded, sessions, conversation.chatId, refreshHistoryFiles]);

  const transport = useMemo(() => {
    return new WorkflowChatTransport<ResilientMessage>({
      api: '/api/chat',
      fetch: createTrackedFetch(userId),
      maxConsecutiveErrors: 3,
      initialStartIndex: 0,
      prepareSendMessagesRequest: ({ id, messages, trigger, body }) => ({
        body: {
          messages,
          chat_id: id,
          trigger,
          ...(body?.continuation === true ? { continuation: true } : {}),
          ...(Array.isArray(body?.attachment_ids) &&
          body.attachment_ids.length > 0
            ? { attachment_ids: body.attachment_ids }
            : {}),
          ...(knowledgeBaseIds.length
            ? { knowledge_base_ids: knowledgeBaseIds }
            : {}),
        },
        headers: { 'Content-Type': 'application/json' },
      }),
      prepareReconnectToStreamRequest: ({ api }) => {
        const persistedRun = readPersistedRun(userId);
        const pendingRun =
          persistedRun?.pending && persistedRun.chatId === conversation.chatId
            ? persistedRun
            : conversation.resumeRun?.pending
              ? conversation.resumeRun
              : null;
        if (!pendingRun) return { api };
        return {
          api: `/api/chat/${encodeURIComponent(pendingRun.runId)}/stream`,
          headers: { 'x-page-resume': '1' }, // 固定让服务器从0开始全量重放
        };
      },
      onChatSendMessage: (response, options) => {
        const runId = response.headers.get('x-workflow-run-id');
        if (!runId) return;
        writePersistedRun(
          {
            chatId: options.chatId,
            runId,
            chunkIndex: 0,
            messages: options.messages,
            pending: true,
          },
          userId,
        );
        void refreshSessions();
      },
      onChatEnd: ({ chunkIndex }) => {
        const current = readPersistedRun(userId);
        if (!current) return;
        writePersistedRun({ ...current, chunkIndex }, userId);
      },
    });
  }, [
    userId,
    conversation.chatId,
    conversation.resumeRun,
    refreshSessions,
    knowledgeBaseIds,
  ]);

  const {
    clearError,
    error,
    messages,
    regenerate: reload,
    resumeStream,
    sendMessage,
    setMessages,
    status,
    stop,
  } = useChat<ResilientMessage>({
    id: conversation.chatId,
    messages: conversation.messages,
    resume: Boolean(
      conversation.resumeRun?.pending && conversation.resumeRun.runId,
    ),
    throttle: 24,
    transport,
    onData: (part) => {
      if (part.type === 'data-subagent') {
        // 子 Agent 事件：折叠进执行面板卡片（started 显示运行中，completed 落定结果）。
        setSubagentCards((current) => foldSubagentCard(current, part.data));
      }
      if (part.type === 'data-agent') {
        const event = part.data;
        const mapped = agentEventToTrace(event);
        if (mapped) {
          setTrace((current) =>
            current.some((item) => item.id === mapped.id)
              ? current
              : [...current.slice(-11), mapped],
          );
        }
        if (event.type === 'run.started') {
          setActivity({
            entries: [],
            startedAt: Date.parse(event.timestamp) || Date.now(),
            lastEventAt: Date.now(),
          });
          setGeneratedTokens(0);
          setContextInputTokens(0);
          setRunCapabilities(event.capabilities ?? null);
          setContextCompressing(false);
        }
        if (event.type === 'context.compressing') {
          // 摘要阶段：只展示流光指示器，不渲染摘要正文。
          setContextCompressing(true);
        }
        if (event.type === 'assistant.reasoning') {
          // 推理模型的思考过程：按 runId 独立存储，不写入消息 parts，
          // 避免被 AI SDK 流式 text-delta 更新覆盖。run 结束后仍保留。
          setContextCompressing(false);
          setReasoningByRunId((current) => {
            const next = new Map(current);
            next.set(event.runId, (next.get(event.runId) ?? '') + event.text);
            return next;
          });
        }
        if (event.type === 'usage.updated') {
          // 真实用量：每次模型调用报一条增量，按 run 累加即为本轮生成量。
          setGeneratedTokens((current) => current + event.outputTokens);
          // 输入 tokens 累计近似当前上下文占用，供观测面板展示。
          setContextInputTokens((current) => current + event.inputTokens);
        }
        if (event.type === 'assistant.narration') {
          // 过程旁白只进过程区，不进消息正文。
          const at = Date.parse(event.timestamp) || Date.now();
          setActivity((current) => ({
            entries: [
              ...current.entries.slice(-19),
              {
                kind: 'narration',
                id: `narration-${event.timestamp}-${current.entries.length}`,
                text: event.text,
                at,
              },
            ],
            startedAt: current.startedAt ?? at,
            lastEventAt: Date.now(),
          }));
        }
        if (event.type === 'tool.started' || event.type === 'tool.completed') {
          const phase = event.type === 'tool.started' ? 'start' : 'end';
          setContextCompressing(false);
          const at = Date.parse(event.timestamp) || Date.now();
          const input = event.type === 'tool.started' ? event.input : null;
          const output = event.type === 'tool.completed' ? event.output : null;
          setActivity((current) => ({
            entries: [
              ...current.entries.slice(-19),
              {
                kind: 'tool',
                id: `${event.type}-${event.invocationId}-${event.timestamp}`,
                invocationId: event.invocationId,
                tool: event.tool,
                phase,
                at,
                input,
                output,
              },
            ],
            startedAt: current.startedAt ?? at,
            lastEventAt: Date.now(),
          }));
        }
        if (event.type === 'todo.updated') setAgentTodos(event.todos);
        if (
          event.type === 'approval.required' ||
          event.type === 'question.required'
        ) {
          setPendingInterrupt(event);
          setInteractionError(null);
        }
        if (
          event.type === 'run.completed' ||
          event.type === 'run.cancelled' ||
          event.type === 'run.failed'
        ) {
          setPendingInterrupt(null);
          setContextCompressing(false);
          // 本轮结束：把这一轮写过的文件并入历史文件记录，
          // 后续新一轮运行清空实时记录时文件面板仍然完整。
          void refreshHistoryFiles();
        }
        if (event.type === 'run.failed') {
          setRunFailure({ code: event.code, message: event.message });
        }
        if (event.type === 'run.completed' || event.type === 'run.cancelled') {
          setRunFailure(null);
        }
      }
      if (part.type === 'data-pipeline') {
        setTrace((current) => [...current.slice(-9), part.data]);
      }
      if (part.type === 'data-suggestions') {
        setSuggestions(part.data);
      }
    },
    onError: (caught) => {
      if (caught.message.includes('404')) {
        clearPersistedRun(userId);
        setConversation((current) => ({ ...current, resumeRun: null }));
      }
      const rolledBack = sessionRef.current.rollbackAssistant();
      setTrace((current) => [
        ...current.slice(-9),
        localEvent(
          'request',
          'error',
          '本轮请求已安全回退',
          rolledBack
            ? 'Conversationalist undo() 已移除半截 assistant 状态'
            : '请求在写入 assistant 历史前失败，可直接重新生成',
        ),
      ]);
      console.warn('[Recoverable Chat Error]', caught.message);
    },
    onFinish: ({
      finishReason,
      message,
      messages: finishedMessages,
      isAbort,
      isError,
    }) => {
      const text = messageText(message);
      const taskFailed = finishReason === 'error';
      if (!isError && !taskFailed && text) {
        sessionRef.current.commitAssistant(text);
      }
      if (isError) return;

      // 运行正常收尾却没有任何文本：提示用户而不是留下一个永远转圈的空气泡
      if (!isAbort && !taskFailed && !text) {
        setRunFailure({
          code: 'empty_completion',
          message: 'Agent 本次没有返回任何内容',
        });
        setTrace((current) => [
          ...current.slice(-9),
          localEvent(
            'verify',
            'warning',
            '本轮没有产生任何回复',
            '运行已结束但未收到文本内容，可以重新发送这条消息',
          ),
        ]);
      }

      const persisted = readPersistedRun(userId);
      const runId = message.metadata?.runId ?? persisted?.runId ?? '';
      if (runId) {
        writePersistedRun(
          {
            chatId: conversation.chatId,
            runId,
            chunkIndex: persisted?.chunkIndex ?? 0,
            messages: finishedMessages,
            pending: false,
          },
          userId,
        );
      }
      void refreshSessions();
      setConversation((current) =>
        current.chatId === conversation.chatId
          ? { ...current, resumeRun: null }
          : current,
      );
      if (isAbort) {
        setTrace((current) => [
          ...current.slice(-9),
          localEvent(
            'transport',
            'warning',
            '生成已由用户停止',
            '已保留当前可见内容',
          ),
        ]);
      }
    },
  });

  const isBusy = status === 'submitted' || status === 'streaming';
  const hasConversation = messages.length > 0;
  const lastMessage = messages.at(-1);
  const hasAssistantPlaceholder =
    lastMessage?.role === 'assistant' && !messageText(lastMessage);
  // 执行日志面板要插在最后一条 assistant 消息之前（本轮回复的上方）。
  const lastAssistantIndex = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]?.role === 'assistant') return index;
    }
    return -1;
  }, [messages]);
  const lastAssistant = [...messages]
    .reverse()
    .find((message) => message.role === 'assistant');

  // 页面重新可见时，如果当前运行确实还在进行（后端仍有 pending run），
  // 就自动把流接回来。切走再切回时浏览器丢弃了旧连接，这里用服务端的
  // run 状态而不是前端 error 作为是否恢复的依据，避免任务被误判为结束。
  useEffect(() => {
    if (!isPageVisible) return;
    const persisted = readPersistedRun(userId);
    const hasActiveRun =
      status === 'submitted' ||
      status === 'streaming' ||
      (persisted?.pending && persisted.chatId === conversation.chatId);
    if (!hasActiveRun || !persisted) return;

    const controller = new AbortController();
    apiFetch(`/api/agent/runs/${encodeURIComponent(persisted.runId)}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 404) {
          clearPersistedRun(userId);
          return;
        }
        if (!response.ok) return;
        const run = (await response.json()) as RunSummary;
        const stillPending = isPendingStatus(run.status);
        writePersistedRun({ ...persisted, pending: stillPending }, userId);
        if (stillPending && status === 'error') {
          // 任务还在后台运行，只是页面失去焦点的这段时间连接断了。
          await resumeStream();
          setRunFailure(failureFromRun(run));
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [isPageVisible, status, conversation.chatId, resumeStream, userId]);

  useEffect(() => {
    if (status !== 'streaming' || !lastAssistant) return;
    sessionRef.current.stageAssistant(messageText(lastAssistant));
  }, [lastAssistant, status]);

  useEffect(() => {
    if (!isBusy) return;
    const timer = window.setInterval(() => setActivityClock(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [isBusy]);

  // 流式期间节流持久化消息：页面卡死/崩溃时，已收到的 AI 回复不至于在刷新后丢失。
  // 必须用节流（每 300ms 最多写一次）而非防抖——流式期间 messages 每 ~24ms 就
  // 更新一次，防抖会因持续重置定时器而永不触发，导致崩溃时什么都没存下。
  const lastPersistRef = useRef(0);
  useEffect(() => {
    if (!isBusy) return;
    const now = Date.now();
    if (now - lastPersistRef.current < 300) return;
    lastPersistRef.current = now;
    const current = readPersistedRun(userId);
    if (current && current.chatId === conversation.chatId) {
      writePersistedRun({ ...current, messages }, userId);
    }
  }, [messages, isBusy, userId, conversation.chatId]);

  // 运行结束时 flush 最后一帧：onFinish 也会写，但提前在 isBusy 落 false 时
  // 补一次，确保流刚结束、用户立刻刷新的场景下也有最新内容。
  useEffect(() => {
    if (isBusy) return;
    const current = readPersistedRun(userId);
    if (current && current.chatId === conversation.chatId) {
      writePersistedRun({ ...current, messages }, userId);
    }
  }, [isBusy, messages, userId, conversation.chatId]);

  const agentActivity: AgentStatus = useMemo(() => {
    // 用量来自模型返回的真实 usage，运行结束后保留最终值
    return {
      ...deriveAgentActivity(activity, isBusy, activityClock),
      tokens: activity.startedAt !== null || isBusy ? generatedTokens : 0,
    };
  }, [activity, activityClock, generatedTokens, isBusy]);

  // 与 AgentStatusPanel 的显示条件保持一致：有过程记录或子 Agent 卡片才显示。
  const processPanelVisible =
    agentActivity.entries.length > 0 || subagentCards.length > 0;

  useEffect(() => {
    // 已经有缓存就先直接渲染，切回页面时不再重复拉取会话列表
    if (restoredSessions) return;
    const controller = new AbortController();
    let active = true;
    fetchSessionPage(undefined, controller.signal)
      .then((page) => {
        if (!active) return;
        setSessions(page.data);
        setSessionsNextCursor(page.nextCursor);
        setSessionsError(null);
        writeSessionCache(
          { data: page.data, nextCursor: page.nextCursor },
          userId,
        );
      })
      .catch((caught: unknown) => {
        if (
          !active ||
          (caught instanceof DOMException && caught.name === 'AbortError')
        ) {
          return;
        }
        setSessionsError('历史记录加载失败');
      })
      .finally(() => {
        if (active) setSessionsLoaded(true);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [restoredSessions, userId]);

  useEffect(() => {
    const persisted = readPersistedRun(userId);
    if (!persisted) return;
    const controller = new AbortController();
    apiFetch(`/api/agent/runs/${encodeURIComponent(persisted.runId)}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 404) {
          clearPersistedRun(userId);
          return;
        }
        if (!response.ok) return;
        const run = (await response.json()) as RunSummary;
        // 竞态保护：若会话加载（loadSession）已把持久化 run 更新成本会话更新的 run
        // （例如用户已成功续跑），这里拿到的旧 run 状态已过期，不能再据此设置失败横幅，
        // 否则会覆盖 loadSession 里 setRunFailure(null) 的正确结果，导致刷新后横幅常驻。
        if (readPersistedRun(userId)?.runId !== run.id) return;
        setRunFailure(failureFromRun(run));
        const pending = isPendingStatus(run.status);
        writePersistedRun({ ...persisted, pending }, userId);
        if (pending) {
          setConversation((current) =>
            current.chatId === persisted.chatId
              ? { ...current, resumeRun: persisted }
              : current,
          );
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
    // 仅在挂载时校准一次上一次运行的持久化状态
  }, [userId]);

  // 刷新页面后从 API 重拉当前会话完整历史。
  // localStorage 的 persisted messages 只用于崩溃恢复，不能作为完整历史的数据源——
  // 最后一次 run 若被取消/失败没产出文本，onFinish 不会写完整 messages，缓存里
  // 可能只剩用户消息。sessions 加载完后匹配当前会话，拉一次 history 覆盖。
  const historyLoadedRef = useRef(false);
  useEffect(() => {
    if (!sessionsLoaded || historyLoadedRef.current) return;
    historyLoadedRef.current = true;
    const persisted = readPersistedRun(userId);
    if (!persisted) return;
    const match = sessions.find((s) => s.externalKey === persisted.chatId);
    if (!match) return;
    const controller = new AbortController();
    apiFetch(`/api/agent/sessions/${encodeURIComponent(match.id)}/history`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) return;
        const history = (await response.json()) as SessionHistory;
        const restoredMessages = messagesFromHistory(history.messages);
        setReasoningByRunId(() => {
          const map = new Map<string, string>();
          for (const msg of history.messages) {
            if (msg.reasoning) map.set(msg.runId, msg.reasoning);
          }
          return map;
        });
        const latestRun = history.latestRun;
        const pending = latestRun ? isPendingStatus(latestRun.status) : false;
        // 非 pending：用 API 完整历史覆盖本地缓存（修复取消/失败后丢回复的问题）。
        // pending：不覆盖——让流重放恢复当前 run 的流式输出，避免历史快照（不含
        // 当前 run 的流式增量）覆盖掉 localStorage 里已有的部分内容。
        if (!pending) {
          writePersistedRun(
            { ...persisted, messages: restoredMessages, pending },
            userId,
          );
          setConversation((current) =>
            current.chatId === persisted.chatId
              ? { ...current, messages: restoredMessages }
              : current,
          );
          setRunFailure(failureFromRun(latestRun));
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [sessions, sessionsLoaded, userId]);

  useEffect(() => {
    if (!notice) return;
    const timeout = window.setTimeout(() => setNotice(null), 2400);
    return () => window.clearTimeout(timeout);
  }, [notice]);

  useEffect(() => {
    if (sidebarWasOpenRef.current && !sidebarOpen) {
      mobileMenuButtonRef.current?.focus();
    }
    sidebarWasOpenRef.current = sidebarOpen;
  }, [sidebarOpen]);

  useEffect(() => {
    if (sessionDialog || !dialogReturnFocusRef.current) {
      return;
    }
    const returnTarget = dialogReturnFocusRef.current;
    dialogReturnFocusRef.current = null;
    window.requestAnimationFrame(() => returnTarget.focus());
  }, [sessionDialog]);

  useEffect(() => {
    if (!sessionDialog && !sessionMenuId && !sidebarOpen) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (sessionDialogBusy) return;
      setSessionDialog(null);
      setSessionMenuId(null);
      setSidebarOpen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [sessionDialog, sessionDialogBusy, sessionMenuId, sidebarOpen]);

  // 会话右键菜单（重命名/删除）打开时，点击菜单和"…"按钮以外的区域立即关闭。
  useEffect(() => {
    if (!sessionMenuId) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (target.closest('.session-menu') || target.closest('.session-more')) {
        return;
      }
      setSessionMenuId(null);
    };
    document.addEventListener('pointerdown', handlePointerDown, true);
    return () =>
      document.removeEventListener('pointerdown', handlePointerDown, true);
  }, [sessionMenuId]);

  // 只滚动会话容器自己：scrollIntoView 会连带滚动所有祖先（包括窗口），
  // 处理中高频触发时会把整个页面滚走，露出底部大片空白。
  // 依赖里纳入过程记录与任务列表，「正在执行」容器每次更新都会跟随到底。
  useEffect(() => {
    const container = conversationRef.current;
    if (!container || !stickToBottomRef.current) return;
    container.scrollTop = container.scrollHeight;
  }, [
    messages,
    agentActivity.entries,
    agentTodos,
    generatedTokens,
    pendingInterrupt,
    error,
    runFailure,
  ]);

  function handleConversationScroll() {
    const container = conversationRef.current;
    if (!container) return;
    const distance =
      container.scrollHeight - container.scrollTop - container.clientHeight;
    stickToBottomRef.current = distance < 96;
  }

  // 任意容器尺寸变化（展开「正在执行」面板、工具日志追加、任务条变高、
  // 输入区撑高等）只要用户还停在底部，就立刻跟随到底。
  useEffect(() => {
    const container = conversationRef.current;
    const list = messageListRef.current;
    if (!container) return;
    const follow = () => {
      if (stickToBottomRef.current) {
        container.scrollTop = container.scrollHeight;
      }
    };
    const observer = new ResizeObserver(follow);
    if (list) observer.observe(list);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // 助手还没输出正文时，气泡里实时显示它正在做什么：优先用模型的过程旁白，
  // 没有旁白时回退到当前正在执行的工具名，避免只剩三个点、用户不知道进展。
  const latestNarrationText = useMemo(() => {
    const latestNarration = [...agentActivity.entries]
      .reverse()
      .find(
        (entry): entry is Extract<AgentActivityEntry, { kind: 'narration' }> =>
          entry.kind === 'narration',
      );
    return latestNarration?.text ?? null;
  }, [agentActivity.entries]);

  const liveActivityLabel = useMemo(() => {
    if (latestNarrationText) return latestNarrationText;
    if (agentActivity.runningTool) {
      return `正在执行 ${agentActivity.runningTool}…`;
    }
    return null;
  }, [latestNarrationText, agentActivity.runningTool]);

  // 执行面板：嵌入本轮 assistant 消息体顶部，与正文同列、顶边不高于头像。
  const processPanel = (
    <AgentStatusPanel
      busy={isBusy}
      status={agentActivity}
      subagents={subagentCards}
    />
  );

  // 从工具调用记录里提取 AI 操作过的文件，按路径去重，保留最后一次操作的内容。
  // 合并历史会话的文件记录（从后端加载）和当前 run 的实时工具调用。
  const touchedFiles = useMemo<TouchedFile[]>(() => {
    const fileOps = new Map<string, TouchedFile>();
    for (const file of historyFiles) {
      fileOps.set(file.path, file);
    }
    for (const entry of agentActivity.entries) {
      if (entry.kind !== 'tool') continue;
      const tool = entry.tool;
      if (
        tool !== 'write_file' &&
        tool !== 'edit_file' &&
        tool !== 'read_file' &&
        tool !== 'delete'
      ) {
        continue;
      }
      const args =
        entry.input && typeof entry.input === 'object'
          ? (entry.input as Record<string, unknown>)
          : {};
      const filePath = String(args.file_path ?? args.path ?? '').trim();
      if (!filePath) continue;

      let content: string | null = fileOps.get(filePath)?.content ?? null;
      if (tool === 'write_file' || tool === 'edit_file') {
        const raw = args.content;
        if (typeof raw === 'string') content = raw;
      } else if (tool === 'read_file' && entry.output != null) {
        const out = entry.output;
        if (typeof out === 'string') content = out;
        else if (out && typeof out === 'object') {
          const record = out as Record<string, unknown>;
          const candidate = record.content ?? record.text ?? record.output;
          if (typeof candidate === 'string') content = candidate;
        }
      }

      fileOps.set(filePath, {
        path: filePath,
        content,
        operation: tool,
      });
    }
    return [...fileOps.values()].sort((a, b) => a.path.localeCompare(b.path));
  }, [agentActivity.entries, historyFiles]);

  /**
   * 点击聊天正文中的文件路径链接：在 touchedFiles 中定位对应文件，
   * 打开文件面板并选中它。支持精确匹配、后缀匹配与文件名匹配，
   * 兼容 AI 回复里写相对路径或省略目录前缀的情况。
   */
  function handleFileLinkClick(linkPath: string) {
    const normalized = linkPath.replace(/^(\.\/|\/)+/, '');
    const match =
      touchedFiles.find((file) => file.path === normalized) ??
      touchedFiles.find((file) => file.path.endsWith(`/${normalized}`)) ??
      touchedFiles.find(
        (file) => file.path.split('/').pop() === normalized.split('/').pop(),
      );
    if (match) {
      setSelectedFilePath(match.path);
    } else {
      // 没找到也打开面板，提示用户该文件不在已记录的操作列表中。
      setSelectedFilePath(null);
    }
    setFilesOpen(true);
  }

  /** 用户点击聊天消息中的页面预览按钮：打开文件面板、展开至 70%、隐藏左侧导航并启用构建预览。 */
  function handlePreviewPage() {
    setSelectedFilePath(null);
    setFilesOpen(true);
    setFilesWide(true);
    setSidebarHidden(true);
    setFilesWidth(Math.round(window.innerWidth * 0.7));
    setOpenBuildPreviewCount((c) => c + 1);
  }

  async function selectSession(session: WebSessionSummary) {
    if (session.externalKey === conversation.chatId) return;
    stickToBottomRef.current = true;
    setSwitchingSessionId(session.id);
    setSessionsError(null);
    try {
      // 切走前先停止当前任务：如果它在后台运行，就发取消信号并断开流。
      await stopCurrentConversation();
      const response = await apiFetch(
        `/api/agent/sessions/${encodeURIComponent(session.id)}/history`,
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const history = (await response.json()) as SessionHistory;
      const restoredMessages = messagesFromHistory(history.messages);
      // 从历史消息中恢复思考过程到 Map，刷新后仍可见。
      setReasoningByRunId(() => {
        const map = new Map<string, string>();
        for (const msg of history.messages) {
          if (msg.reasoning) map.set(msg.runId, msg.reasoning);
        }
        return map;
      });
      const latestRun = history.latestRun;
      const persistedRun = latestRun
        ? {
            chatId: session.externalKey,
            runId: latestRun.id,
            chunkIndex: 0,
            messages: restoredMessages,
            pending: isPendingStatus(latestRun.status),
          }
        : null;

      // 并行加载该会话历史里 AI 操作过的文件，供文件面板展示。
      const files = await fetchSessionFiles(session.id).catch(() => []);
      setHistoryFiles(
        files.map((file) => ({
          path: file.path,
          content: file.content,
          operation: file.operation as TouchedFile['operation'],
        })),
      );

      await stop();
      clearError();
      if (persistedRun) writePersistedRun(persistedRun, userId);
      else clearPersistedRun(userId);
      setConversation({
        chatId: session.externalKey,
        messages: restoredMessages,
        resumeRun: persistedRun?.pending ? persistedRun : null,
      });
      setInput('');
      setSuggestions([]);
      setTrace(initialTrace);
      setDismissedCards(new Set());
      setPendingInterrupt(null);
      setAgentTodos([]);
      setActivity(emptyActivity);
      setSubagentCards(subagentCardsFromMessages(restoredMessages));
      setGeneratedTokens(0);
      setInteractionError(null);
      setRunFailure(failureFromRun(latestRun));
      setHiddenContinuationIds(new Set());
      sessionRef.current = new ResilientSession();
    } catch {
      setSessionsError('无法打开这条历史记录');
    } finally {
      setSwitchingSessionId(null);
    }
  }

  async function submitText(
    value: string,
    files: FileUIPart[] = [],
    attachmentIds: string[] = [],
  ) {
    const trimmed = value.trim();
    if ((!trimmed && files.length === 0) || isBusy || error) return;
    stickToBottomRef.current = true;
    setInput('');
    setSuggestions([]);
    setRunFailure(null);
    // 新消息开始时清掉上一轮残留的过程记录/任务计划，避免"你好"也先冒出上轮的工具执行。
    setActivity(emptyActivity);
    setAgentTodos([]);
    setSubagentCards([]);
    setGeneratedTokens(0);
    sessionRef.current.addUserMessage(trimmed);
    setTrace([
      localEvent(
        'request',
        'running',
        '正在提交新消息',
        'useChat 已锁定输入并创建请求',
      ),
    ]);
    await sendMessage(
      files.length > 0 ? { text: trimmed, files } : { text: trimmed },
      attachmentIds.length > 0
        ? { body: { attachment_ids: attachmentIds } }
        : undefined,
    );
  }

  // 续跑 run 落定后（成功或再次失败），把仅用于驱动请求的占位 user 消息
  // 从 useChat store 移除；它从未进入会话持久化，历史接口也不会返回它。
  useEffect(() => {
    if (status !== 'ready' && status !== 'error') return;
    if (hiddenContinuationIds.size === 0) return;
    scheduleMicrotask(() => {
      setMessages(
        messages.filter((message) => !hiddenContinuationIds.has(message.id)),
      );
      setHiddenContinuationIds(new Set());
    });
  }, [status, messages, hiddenContinuationIds, setMessages]);

  // 「继续对话」：等价于替用户发起一次"继续"，但不留下任何用户消息记录。
  // 真正发给模型的内容由服务端合成（continuation 标记），前端这条占位
  // user 消息全程隐藏，并在 run 结束后从本地 store 删除。
  async function continueAfterFailure() {
    if (isBusy || error) return;
    const continuationId = crypto.randomUUID();
    setHiddenContinuationIds((current) => {
      const next = new Set(current);
      next.add(continuationId);
      return next;
    });
    stickToBottomRef.current = true;
    // 发起即消失：若新 run 再次失败，run.failed 事件会重新拉起横幅。
    setRunFailure(null);
    setActivity(emptyActivity);
    setAgentTodos([]);
    setSubagentCards([]);
    setGeneratedTokens(0);
    setTrace([
      localEvent(
        'request',
        'running',
        '正在续跑上一轮任务',
        '基于会话已有进度创建续跑请求',
      ),
    ]);
    // AI SDK 的 sendMessage 有两条路径：
    // 1) 传 messageId → 语义是"替换已存在的消息"，找不到就抛 "message with id X not found"；
    // 2) 不传 messageId、直接传 { id, role, parts } → 用指定 id 新建消息。
    // 这里走路径 2，避免先 setMessages 再 sendMessage 的时序竞态。
    await sendMessage(
      {
        id: continuationId,
        role: 'user',
        parts: [{ type: 'text', text: CONTINUATION_PLACEHOLDER }],
      },
      { body: { continuation: true } },
    );
  }

  function handleSubmit(
    event: FormEvent<HTMLFormElement>,
    files: FileUIPart[],
    attachmentIds: string[],
  ) {
    event.preventDefault();
    void submitText(input, files, attachmentIds);
  }

  async function handleConnectionRecovery() {
    const persistedRun = readPersistedRun(userId);
    const canResume =
      persistedRun?.pending && persistedRun.chatId === conversation.chatId;
    setTrace((current) => [
      ...current.slice(-9),
      localEvent(
        'retry',
        'running',
        canResume ? '正在重新连接' : '正在重新提交',
        canResume
          ? '将从已持久化的事件 cursor 继续接收，不会创建重复任务'
          : '原运行记录已失效，将重新提交上一条消息',
      ),
    ]);
    clearError();
    if (canResume) {
      await resumeStream();
      return;
    }
    await reload();
  }

  async function stopCurrentConversation() {
    const currentRun = readPersistedRun(userId);
    if (currentRun?.pending) {
      await apiFetch(
        `/api/agent/runs/${encodeURIComponent(currentRun.runId)}/cancel`,
        { method: 'POST' },
      ).catch(() => null);
    }
    await stop();
  }

  function resetConversation(chatId: string) {
    stickToBottomRef.current = true;
    clearError();
    clearPersistedRun(userId);
    setConversation({
      chatId,
      messages: [],
      resumeRun: null,
    });
    setInput('');
    setSuggestions([]);
    setTrace(initialTrace);
    setDismissedCards(new Set());
    setPendingInterrupt(null);
    setAgentTodos([]);
    setActivity(emptyActivity);
    setSubagentCards([]);
    setHistoryFiles([]);
    setGeneratedTokens(0);
    setInteractionError(null);
    setRunFailure(null);
    setHiddenContinuationIds(new Set());
    sessionRef.current = new ResilientSession();
  }

  async function handleNewChat() {
    if (creatingSession) return;
    setSessionMenuId(null);
    setCreatingSession(true);
    setSessionsError(null);
    const externalKey = crypto.randomUUID();
    try {
      const response = await apiFetch('/api/agent/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          externalKey,
          title: '新会话',
        }),
      });
      if (!response.ok) {
        throw new Error(await responseError(response, '新建会话失败'));
      }
      await stopCurrentConversation();
      resetConversation(externalKey);
      await refreshSessions();
    } catch (caught) {
      setSessionsError(
        caught instanceof Error ? caught.message : '新建会话失败',
      );
    } finally {
      setCreatingSession(false);
    }
  }

  async function loadMoreSessions() {
    if (!sessionsNextCursor || loadingMoreSessions) return;
    setLoadingMoreSessions(true);
    try {
      const page = await fetchSessionPage(sessionsNextCursor);
      setSessions((current) => {
        const known = new Set(current.map((session) => session.id));
        return [
          ...current,
          ...page.data.filter((session) => !known.has(session.id)),
        ];
      });
      setSessionsNextCursor(page.nextCursor);
      setSessionsError(null);
    } catch {
      setSessionsError('更多历史记录加载失败');
    } finally {
      setLoadingMoreSessions(false);
    }
  }

  async function renameSession(title: string) {
    if (!sessionDialog || sessionDialog.kind !== 'rename') return;
    setSessionDialogBusy(true);
    setSessionDialogError(null);
    try {
      const response = await apiFetch(
        `/api/agent/sessions/${encodeURIComponent(sessionDialog.session.id)}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title }),
        },
      );
      if (!response.ok) {
        throw new Error(await responseError(response, '重命名失败'));
      }
      const updated = (await response.json()) as WebSessionSummary;
      setSessions((current) =>
        current.map((session) =>
          session.id === updated.id ? updated : session,
        ),
      );
      setSessionDialog(null);
      setNotice('会话名称已更新');
    } catch (caught) {
      setSessionDialogError(
        caught instanceof Error ? caught.message : '重命名失败',
      );
    } finally {
      setSessionDialogBusy(false);
    }
  }

  async function deleteSession() {
    if (!sessionDialog || sessionDialog.kind !== 'delete') return;
    setSessionDialogBusy(true);
    setSessionDialogError(null);
    try {
      const response = await apiFetch(
        `/api/agent/sessions/${encodeURIComponent(sessionDialog.session.id)}`,
        { method: 'DELETE' },
      );
      if (!response.ok) {
        throw new Error(
          await responseError(
            response,
            response.status === 409 ? '运行中的会话不能删除' : '删除失败',
          ),
        );
      }
      const deleted = sessionDialog.session;
      const wasActive = deleted.externalKey === conversation.chatId;
      // 列表按最近更新排序，过滤后的第一个就是左侧第一项。
      const remaining = sessions.filter((session) => session.id !== deleted.id);
      setSessions(remaining);
      setSessionDialog(null);
      if (wasActive) {
        // 删掉的正是当前会话：自动切到剩余的第一个；一个都不剩就落到新建对话。
        if (remaining[0]) {
          await selectSession(remaining[0]);
        } else {
          await stopCurrentConversation();
          resetConversation(crypto.randomUUID());
        }
      }
      setNotice('会话已删除');
    } catch (caught) {
      setSessionDialogError(
        caught instanceof Error ? caught.message : '删除失败',
      );
    } finally {
      setSessionDialogBusy(false);
    }
  }

  async function respondToInterrupt(
    interrupt: PendingInterrupt,
    body:
      | {
          decision: 'approve' | 'reject';
          scope?: 'once' | 'session';
          message?: string;
        }
      | QuestionAnswer,
  ) {
    setInteractionBusy(true);
    setInteractionError(null);
    const segment =
      interrupt.type === 'approval.required' ? 'approvals' : 'questions';
    try {
      const response = await apiFetch(
        `/api/agent/runs/${encodeURIComponent(interrupt.runId)}/${segment}/${encodeURIComponent(interrupt.interruptId)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
      );
      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as {
          error?: string;
        } | null;
        const code = payload?.error ?? `HTTP ${response.status}`;
        // 孤儿审批：run 已不在等待状态（如 Worker 重启导致审批悬挂，或
        // 刷新后重放了已解决的 approval 事件），卡片永远无法成功提交，
        // 自动移除避免卡死交互。提示不说"任务已结束"——任务可能仍在运行。
        if (
          code === 'run_not_waiting_for_approval' ||
          code === 'run_not_found'
        ) {
          setPendingInterrupt(null);
          setNotice('该审批已处理，卡片已自动移除');
          return;
        }
        throw new Error(code);
      }
      setPendingInterrupt(null);
      const sessionApprovalGranted =
        'decision' in body &&
        body.decision === 'approve' &&
        body.scope === 'session';
      if (sessionApprovalGranted) {
        setNotice('本会话后续操作将自动执行');
      }
      setTrace((current) => [
        ...current.slice(-11),
        localEvent(
          'request',
          'running',
          sessionApprovalGranted ? '已开启本会话自动批准' : '已提交人工响应',
          sessionApprovalGranted
            ? '当前会话后续工具操作将自动执行'
            : '任务已重新进入 Worker 队列',
        ),
      ]);
    } catch (caught) {
      setInteractionError(
        caught instanceof Error ? caught.message : '提交失败，请稍后重试',
      );
    } finally {
      setInteractionBusy(false);
    }
  }

  async function handleStop() {
    const currentRun = readPersistedRun(userId);
    if (!currentRun?.runId) {
      await stop();
      return;
    }
    try {
      const response = await apiFetch(
        `/api/agent/runs/${encodeURIComponent(currentRun.runId)}/cancel`,
        { method: 'POST' },
      );
      if (!response.ok && response.status !== 404) {
        throw new Error(`HTTP ${response.status}`);
      }
      setTrace((current) => [
        ...current.slice(-11),
        localEvent(
          'transport',
          'warning',
          '正在取消运行',
          '取消信号已发送至 Worker',
        ),
      ]);
      if (response.status === 404) await stop();
    } catch {
      await stop();
    }
  }

  async function copyMessage(id: string, text: string) {
    await navigator.clipboard.writeText(text);
    setCopiedMessage(id);
    window.setTimeout(() => setCopiedMessage(null), 1400);
  }

  const persistedForRecovery = error ? readPersistedRun(userId) : null;
  const canResumeConnection = Boolean(
    persistedForRecovery?.pending &&
    persistedForRecovery.chatId === conversation.chatId,
  );

  return (
    <main
      className={`app-shell ${traceOpen ? 'is-trace-open' : ''} ${filesOpen ? 'is-files-open' : ''} ${sidebarOpen ? 'is-sidebar-open' : ''} ${sidebarCollapsed ? 'is-sidebar-collapsed' : ''} ${sidebarHidden ? 'is-sidebar-hidden' : ''}`}
      style={
        {
          '--files-width': `${filesWidth}px`,
        } as CSSProperties
      }
    >
      {!sidebarHidden && (
        <Sidebar
          activeChatId={conversation.chatId}
          busy={isBusy || creatingSession}
          collapsed={sidebarCollapsed}
          creating={creatingSession}
          error={sessionsError}
          loaded={sessionsLoaded}
          hasMore={Boolean(sessionsNextCursor)}
          loadingMore={loadingMoreSessions}
          menuSessionId={sessionMenuId}
          inactive={Boolean(sessionDialog)}
          onToggleCollapse={() => setSidebarCollapsed((current) => !current)}
          onDelete={(session) => {
            dialogReturnFocusRef.current =
              document.activeElement
                ?.closest('.session-item')
                ?.querySelector<HTMLElement>('.session-more') ?? null;
            setSessionMenuId(null);
            setSessionDialogError(null);
            setSessionDialog({ kind: 'delete', session });
          }}
          onLoadMore={() => void loadMoreSessions()}
          onMenu={setSessionMenuId}
          onClose={() => setSidebarOpen(false)}
          onNewChat={() => {
            setSidebarOpen(false);
            void handleNewChat();
          }}
          onRename={(session) => {
            dialogReturnFocusRef.current =
              document.activeElement
                ?.closest('.session-item')
                ?.querySelector<HTMLElement>('.session-more') ?? null;
            setSessionMenuId(null);
            setSessionDialogError(null);
            setSessionDialog({ kind: 'rename', session });
          }}
          onRefresh={() => void refreshSessions()}
          onSelect={(session) => {
            setSidebarOpen(false);
            void selectSession(session);
          }}
          sessions={sessions}
          open={sidebarOpen}
          switchingSessionId={switchingSessionId}
        />
      )}

      {sidebarHidden && (
        <button
          aria-label='展开侧边栏'
          className='sidebar-restore-fab'
          type='button'
          onClick={() => setSidebarHidden(false)}
        >
          <Icon name='menu' size={18} />
        </button>
      )}

      {sidebarOpen && (
        <button
          aria-label='关闭历史对话'
          className='sidebar-scrim'
          type='button'
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <section
        className='chat-column'
        inert={sidebarOpen || Boolean(sessionDialog) ? true : undefined}
      >
        <header className='topbar'>
          <div className='topbar-title'>
            <button
              className='mobile-icon-button'
              ref={mobileMenuButtonRef}
              type='button'
              aria-label='打开历史对话'
              aria-expanded={sidebarOpen}
              onClick={() => setSidebarOpen(true)}
            >
              <Icon name='menu' />
            </button>
          </div>
          <div className='topbar-actions'>
            <button
              className='icon-button'
              type='button'
              aria-label={filesOpen ? '隐藏文件浏览器' : '显示文件浏览器'}
              aria-expanded={filesOpen}
              title={filesOpen ? '隐藏文件浏览器' : '文件浏览器'}
              onClick={() => setFilesOpen((current) => !current)}
            >
              <Icon name='folder' size={16} />
            </button>
            {/* <button
              className="icon-button"
              type="button"
              aria-label={traceOpen ? '隐藏 Agent 运行轨迹' : '显示 Agent 运行轨迹'}
              aria-expanded={traceOpen}
              onClick={() => setTraceOpen((current) => !current)}
            >
              <Icon name="panel" size={16} />
            </button> */}
            <UserMenu
              onAiServiceClick={() => setAiServiceOpen((open) => !open)}
            />
          </div>
        </header>

        <div
          className='conversation'
          aria-live='polite'
          ref={conversationRef}
          onScroll={handleConversationScroll}
        >
          {hasConversation && (
            <div className='message-list' ref={messageListRef}>
              {messages.map((message, index) => {
                if (hiddenContinuationIds.has(message.id)) return null;
                const isStreaming =
                  isBusy &&
                  message.id === lastMessage?.id &&
                  message.role === 'assistant';
                return (
                  <Message
                    copied={copiedMessage === message.id}
                    dismissedCards={dismissedCards}
                    header={index === lastAssistantIndex ? processPanel : null}
                    highlight={highlightMarkdown && !isStreaming}
                    key={message.id}
                    message={message}
                    onBoundaryError={() => {
                      setTrace((current) => [
                        ...current.slice(-9),
                        localEvent(
                          'verify',
                          'warning',
                          'AIBoundary 已隔离组件崩溃',
                          '聊天主体保持可用，错误载荷已进入脱敏流程',
                        ),
                      ]);
                    }}
                    onCopy={copyMessage}
                    onFileLinkClick={handleFileLinkClick}
                    onPreviewPage={handlePreviewPage}
                    onPreviewDiagram={(svg) =>
                      setLightboxImage({ svg, filename: '图表预览' })
                    }
                    onPreviewImage={(url, filename) =>
                      setLightboxImage({ url, filename })
                    }
                    onDismissCard={(messageId) => {
                      setDismissedCards((current) =>
                        new Set(current).add(messageId),
                      );
                    }}
                    runTokens={
                      !isBusy &&
                      message.role === 'assistant' &&
                      index === lastAssistantIndex
                        ? agentActivity.tokens
                        : 0
                    }
                    showWaitingDots={
                      !(index === lastAssistantIndex && processPanelVisible)
                    }
                    streaming={isStreaming}
                    liveLabel={isStreaming ? liveActivityLabel : null}
                    reasoning={
                      reasoningByRunId.get(message.metadata?.runId ?? '') ?? ''
                    }
                  />
                );
              })}
              {status === 'submitted' && !hasAssistantPlaceholder && (
                <ThinkingRow>
                  {processPanelVisible ? processPanel : null}
                </ThinkingRow>
              )}
              {pendingInterrupt && (
                <PendingInteraction
                  key={pendingInterrupt.interruptId}
                  busy={interactionBusy}
                  error={interactionError}
                  interrupt={pendingInterrupt}
                  onApproval={(decision, scope) =>
                    respondToInterrupt(pendingInterrupt, { decision, scope })
                  }
                  onQuestion={(answer) =>
                    respondToInterrupt(pendingInterrupt, answer)
                  }
                />
              )}
              {runFailure && (
                <TaskFailureNotice
                  failure={runFailure}
                  onContinue={() => void continueAfterFailure()}
                />
              )}
              {contextCompressing && (
                <div
                  className='context-compressing'
                  role='status'
                  aria-live='polite'
                >
                  <span className='context-compressing-shimmer' />
                  <span className='context-compressing-text'>
                    正在压缩上下文…
                  </span>
                </div>
              )}
              {error && (
                <div className='error-banner' role='alert'>
                  <div className='error-icon'>
                    <Icon name='triangle' size={19} />
                  </div>
                  <div>
                    <strong>与 Agent 的连接暂时中断</strong>
                    <p>{friendlyError(error)}</p>
                  </div>
                  <button
                    type='button'
                    onClick={() => void handleConnectionRecovery()}
                  >
                    <Icon name='refresh' size={16} />
                    {canResumeConnection ? '重新连接' : '重新提交'}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        <Composer
          activity={
            interactionBusy
              ? '正在提交审批结果…'
              : pendingInterrupt?.type === 'approval.required'
                ? '等待你的审批后继续'
                : pendingInterrupt?.type === 'question.required'
                  ? '等待你的回答后继续'
                  : // 过程区可见时不再重复提示；其余时间保留反馈，避免纯思考阶段毫无动静。
                    processPanelVisible
                    ? null
                    : status === 'submitted'
                      ? '思考中'
                      : status === 'streaming'
                        ? '思考中'
                        : null
          }
          disabled={Boolean(error) || Boolean(pendingInterrupt)}
          disabledPlaceholder={
            pendingInterrupt ? '请先处理上方待办' : '请先恢复与 Agent 的连接'
          }
          input={input}
          isBusy={isBusy}
          knowledgeBases={knowledgeBases}
          knowledgeBaseIds={knowledgeBaseIds}
          onChange={setInput}
          onChangeKnowledgeBases={handleChangeKnowledgeBases}
          onPreviewImage={(url, filename) =>
            setLightboxImage({ url, filename })
          }
          onStop={() => void handleStop()}
          onSubmit={handleSubmit}
          onSuggestion={submitText}
          onToggleKnowledgeBase={handleToggleKnowledgeBase}
          suggestions={suggestions}
          todos={agentTodos}
        />
      </section>

      <TracePanel
        capabilities={runCapabilities}
        contextInputTokens={contextInputTokens}
        generatedTokens={generatedTokens}
        compressing={contextCompressing}
        inactive={sidebarOpen || Boolean(sessionDialog)}
        open={traceOpen}
        onClose={() => setTraceOpen(false)}
        trace={trace}
      />

      {filesOpen && (
        <FilePanel
          files={touchedFiles}
          onClose={() => {
            setFilesOpen(false);
            setSidebarHidden(false);
          }}
          onResize={setFilesWidth}
          onResetWidth={() => {
            setFilesWide(false);
            setSidebarHidden(false);
            setFilesWidth(DEFAULT_FILES_WIDTH);
          }}
          onExpand={() => {
            setFilesWide(true);
            setSidebarHidden(true);
            setFilesWidth(Math.round(window.innerWidth * 0.7));
          }}
          isWide={filesWide}
          onToggleWide={() => {
            const next = !filesWide;
            setFilesWide(next);
            if (next) {
              setSidebarHidden(true);
              setFilesWidth(Math.round(window.innerWidth * 0.7));
            } else {
              setSidebarHidden(false);
              setFilesWidth(DEFAULT_FILES_WIDTH);
            }
          }}
          selectedPath={selectedFilePath}
          onSelectPath={setSelectedFilePath}
          onHideSidebar={() => setSidebarHidden(true)}
          sessionId={
            sessions.find((s) => s.externalKey === conversation.chatId)
              ?.externalKey ?? null
          }
          openBuildPreview={openBuildPreviewCount}
        />
      )}

      {sessionDialog && (
        <SessionActionDialog
          busy={sessionDialogBusy}
          dialog={sessionDialog}
          error={sessionDialogError}
          onClose={() => {
            if (!sessionDialogBusy) setSessionDialog(null);
          }}
          onDelete={() => void deleteSession()}
          onRename={(title) => void renameSession(title)}
        />
      )}

      {notice && (
        <div className='toast' role='status'>
          <Icon name='check' size={16} />
          {notice}
        </div>
      )}

      <Lightbox image={lightboxImage} onClose={() => setLightboxImage(null)} />

      <AiServiceWidget
        open={aiServiceOpen}
        onOpen={() => setAiServiceOpen(true)}
        onClose={() => setAiServiceOpen(false)}
      />
    </main>
  );
}

export { AppSkeleton, ChatRuntime };
