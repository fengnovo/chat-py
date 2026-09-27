import { z } from 'zod';

export const runStatusSchema = z.enum([
  'queued',
  'running',
  'waiting_approval',
  'waiting_question',
  'completed',
  'failed',
  'cancelled',
]);

export type RunStatus = z.infer<typeof runStatusSchema>;

export const todoSchema = z.object({
  content: z.string(),
  status: z.enum(['pending', 'in_progress', 'completed']),
});

export const approvalActionSchema = z.object({
  name: z.string(),
  args: z.record(z.string(), z.unknown()),
  summary: z.string(),
});

export const questionOptionSchema = z.object({
  label: z.string(),
  description: z.string().optional(),
});

export const userQuestionSchema = z.object({
  question: z.string(),
  options: z.array(questionOptionSchema).min(2).max(9),
  multiple: z.boolean(),
  allowCustom: z.boolean(),
});

export const knowledgeBaseIdsSchema = z
  .array(z.uuid())
  .max(10)
  .refine((ids) => new Set(ids).size === ids.length, {
    message: 'knowledge base ids must be unique',
  });

export const citationImageSchema = z.object({
  assetId: z.uuid(),
  name: z.string().trim().min(1).max(255),
  mime: z.string().trim().min(1).max(127),
  alt: z.string().trim().max(500),
  relPath: z.string().trim().min(1).max(1024),
});

export const citationSchema = z.object({
  chunkId: z.uuid(),
  kbId: z.uuid(),
  documentId: z.uuid(),
  documentName: z.string().trim().min(1).max(255),
  ordinal: z.number().int().nonnegative(),
  heading: z.string().trim().max(500).optional(),
  score: z.number().finite().min(-1).max(1),
  via: z.enum(['vector', 'graph', 'both']),
  images: z.array(citationImageSchema).max(20).optional(),
});

export const relationCitationSchema = z.object({
  source: z.string().trim().min(1).max(255),
  relation: z.string().trim().min(1).max(255),
  target: z.string().trim().min(1).max(255),
  chunkIds: z.array(z.uuid()).max(10),
});

export const retrievalStatsSchema = z.object({
  vectorHits: z.number().int().nonnegative().max(10_000),
  graphHops: z.number().int().nonnegative().max(100),
  searchedKbs: z.number().int().nonnegative().max(10),
  durationMs: z.number().int().nonnegative().max(300_000),
  truncated: z.boolean(),
});

export const knowledgeRunTokenClaimsSchema = z.object({
  tenantId: z.uuid(),
  userId: z.uuid(),
  sessionId: z.uuid(),
  runId: z.uuid(),
  kbIds: knowledgeBaseIdsSchema,
  jti: z.uuid(),
  exp: z.number().int().positive(),
  aud: z.literal('knowledge-service'),
});

export type KnowledgeRunTokenClaims = z.infer<
  typeof knowledgeRunTokenClaimsSchema
>;

const eventBase = {
  runId: z.uuid(),
  timestamp: z.string().datetime(),
};

/**
 * run.started 携带的运行时能力快照：本次运行可调用的 MCP 工具与 skills、
 * 上下文压缩阈值、沙箱模式与知识库关联状态，供前端观测面板展示。
 */
export const runCapabilitiesSchema = z.object({
  tools: z.array(z.string().min(1).max(64)).max(120),
  skills: z.array(z.string().min(1).max(128)).max(50),
  backendMode: z.enum(['docker', 'e2b']),
  /** 上下文摘要压缩触发阈值（tokens），0 表示未启用摘要压缩。 */
  contextTriggerTokens: z.number().int().nonnegative(),
  knowledgeEnabled: z.boolean(),
});

export type RunCapabilities = z.infer<typeof runCapabilitiesSchema>;

export const agentEventSchema = z.discriminatedUnion('type', [
  z.object({
    ...eventBase,
    type: z.literal('run.started'),
    capabilities: runCapabilitiesSchema.optional(),
  }),
  z.object({ ...eventBase, type: z.literal('assistant.delta'), text: z.string() }),
  /**
   * 模型的思考过程（reasoning_content）。推理模型在输出正式回复前会先输出
   * 一段内部思考，这里把它实时流式出来，避免用户在思考阶段看不到任何反馈。
   * 前端在可折叠区展示，不属于最终答复正文。
   */
  z.object({ ...eventBase, type: z.literal('assistant.reasoning'), text: z.string() }),
  /**
   * 模型在「带工具调用的轮次」里输出的过程旁白。
   * 这类文本属于执行过程而非最终答复，前端只在可折叠的过程区展示，
   * 不能混进最终消息正文。
   */
  z.object({ ...eventBase, type: z.literal('assistant.narration'), text: z.string() }),
  /** 单次模型调用的真实用量增量；前端按 run 累加即为本轮总消耗。 */
  z.object({
    ...eventBase,
    type: z.literal('usage.updated'),
    inputTokens: z.number().int().nonnegative(),
    outputTokens: z.number().int().nonnegative(),
    totalTokens: z.number().int().nonnegative(),
  }),
  z.object({
    ...eventBase,
    type: z.literal('model.retry'),
    model: z.string(),
    attempt: z.number().int().positive(),
    delayMs: z.number().int().nonnegative(),
    reason: z.string(),
  }),
  z.object({
    ...eventBase,
    type: z.literal('model.fallback'),
    from: z.string(),
    to: z.string(),
    reason: z.string(),
  }),
  z.object({
    ...eventBase,
    type: z.literal('tool.started'),
    invocationId: z.string(),
    tool: z.string(),
    input: z.unknown(),
  }),
  z.object({
    ...eventBase,
    type: z.literal('tool.completed'),
    invocationId: z.string(),
    tool: z.string(),
    output: z.unknown(),
  }),
  z.object({
    ...eventBase,
    type: z.literal('retrieval.completed'),
    retrievalId: z.uuid(),
    toolCallId: z.string().min(1).max(255),
    knowledgeBaseIds: knowledgeBaseIdsSchema,
    query: z.string().trim().min(1).max(10_000),
    citations: z.array(citationSchema).max(20),
    relations: z.array(relationCitationSchema).max(20),
    stats: retrievalStatsSchema,
  }),
  z.object({ ...eventBase, type: z.literal('todo.updated'), todos: z.array(todoSchema) }),
  /**
   * 子 Agent 编排：主 Agent 通过 spawn_subagent 工具派发的子任务生命周期。
   * started 在每轮尝试派发时发出，completed 在该轮子 Agent 结束（含失败/超时）时发出，
   * reviewed 是评分器对 completed 产出的评审结论；不达标时工具会带 feedback 重派，
   * 同 subagentId 会出现 attempt=2、3 的新一轮 started/completed/reviewed。
   * 子 Agent 自身的工具/模型事件不逐条上抛，只在 completed 里聚合计数。
   */
  z.object({
    ...eventBase,
    type: z.literal('subagent.started'),
    subagentId: z.string().min(1).max(64),
    /** 角色名：主 Agent 现场撰写的 role_prompt 首行（截断），用于卡片标题。 */
    role: z.string().min(1).max(200),
    /** 任务简述：task 文本截断，供卡片展开前的一句话说明。 */
    description: z.string().max(2_000),
    /** 第几轮尝试：首轮为 1，评审不达标重派时递增（上限 3）。 */
    attempt: z.number().int().positive(),
    /** 是否为后台异步任务（spawn_subagent 的 background=true）：工具立即返回，
     *  主 Agent 先给阶段性回复，任务在同 run 内继续执行、完成后自动续轮汇总。 */
    background: z.boolean().optional(),
  }),
  z.object({
    ...eventBase,
    type: z.literal('subagent.completed'),
    subagentId: z.string().min(1).max(64),
    /** 该完成事件对应第几轮尝试。 */
    attempt: z.number().int().positive(),
    status: z.enum(['completed', 'failed', 'timeout']),
    /** 回传给主 Agent 的摘要文本（已 clamp 到 2000 字）。 */
    summary: z.string().max(2_000),
    toolCalls: z.number().int().nonnegative(),
    durationMs: z.number().int().nonnegative(),
  }),
  z.object({
    ...eventBase,
    type: z.literal('subagent.reviewed'),
    subagentId: z.string().min(1).max(64),
    /** 被评审的是第几轮尝试的产出。 */
    attempt: z.number().int().positive(),
    /** 是否达到验收标准；false 时工具会带 feedback 重派（未达上限的话）。 */
    passed: z.boolean(),
    /** 0-100 质量分（仅参考，通过线由评分器按验收标准判断）。 */
    score: z.number().min(0).max(100),
    /** 不达标时的具体整改意见；通过时为空串。 */
    feedback: z.string().max(1_000),
    /** 逐条验收项的达成情况，供卡片展示与下轮整改对照。 */
    checklist: z
      .array(
        z.object({
          item: z.string().min(1).max(300),
          met: z.boolean(),
        }),
      )
      .max(20),
  }),
  z.object({
    ...eventBase,
    type: z.literal('approval.required'),
    interruptId: z.string(),
    actions: z.array(approvalActionSchema).min(1),
  }),
  z.object({
    ...eventBase,
    type: z.literal('question.required'),
    interruptId: z.string(),
    question: userQuestionSchema,
  }),
  z.object({
    ...eventBase,
    type: z.literal('artifact.created'),
    artifactId: z.uuid(),
    name: z.string(),
    contentType: z.string(),
  }),
  z.object({ ...eventBase, type: z.literal('run.completed') }),
  z.object({ ...eventBase, type: z.literal('run.cancelled') }),
  z.object({
    ...eventBase,
    type: z.literal('run.failed'),
    code: z.string(),
    message: z.string(),
  }),
  /**
   * 上下文压缩进行中。deepagents 的 summarization middleware 在对话超长时
   * 会调用模型生成摘要，该摘要只供后续模型调用使用，不应作为回复展示给用户。
   * 前端收到此事件后显示「正在压缩上下文」指示器，忽略后续摘要文本。
   */
  z.object({ ...eventBase, type: z.literal('context.compressing') }),
]);

export type AgentEvent = z.infer<typeof agentEventSchema>;

export const persistedAgentEventSchema = agentEventSchema.and(
  z.object({
    seq: z.number().int().positive(),
  }),
);

export type PersistedAgentEvent = z.infer<typeof persistedAgentEventSchema>;

export const createSessionSchema = z.object({
  title: z.string().trim().min(1).max(120).default('新会话'),
  projectId: z.uuid().optional(),
  externalKey: z.string().trim().min(1).max(200).optional(),
});

export const updateSessionSchema = z.object({
  title: z.string().trim().min(1).max(120),
});

export const gitProjectSourceSchema = z.object({
  type: z.literal('git'),
  url: z
    .url()
    .refine((value) => new URL(value).protocol === 'https:', {
      message: 'Only HTTPS Git repositories are supported',
    })
    .refine((value) => {
      const url = new URL(value);
      return !url.username && !url.password;
    }, 'Credentials must not be embedded in the Git URL'),
  ref: z.string().trim().min(1).max(200).optional(),
});

export const workspaceSourceSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('empty') }),
  gitProjectSourceSchema,
  z.object({
    type: z.literal('upload'),
    objectKey: z.string().min(1).max(1_000),
  }),
]);

export type WorkspaceSource = z.infer<typeof workspaceSourceSchema>;

export const createProjectSchema = z.object({
  name: z.string().trim().min(1).max(120),
  source: z.discriminatedUnion('type', [
    z.object({ type: z.literal('empty') }),
    gitProjectSourceSchema,
  ]),
});

export const uploadProjectSchema = z.object({
  name: z.string().trim().min(1).max(120),
  files: z
    .array(
      z.object({
        path: z.string().trim().min(1).max(1_000),
        contentBase64: z.string().min(1),
      }),
    )
    .min(1)
    .max(1_000),
});

export const createRunSchema = z.object({
  message: z.string().trim().min(1).max(100_000),
  knowledgeBaseIds: knowledgeBaseIdsSchema.default([]),
});

export const approvalDecisionSchema = z.object({
  decision: z.enum(['approve', 'reject']),
  scope: z.enum(['once', 'session']).default('once'),
  message: z.string().trim().max(2_000).optional(),
});

export const questionAnswerSchema = z.object({
  selections: z.array(
    z.object({
      index: z.number().int().nonnegative(),
      label: z.string(),
    }),
  ),
  customText: z.string().trim().max(4_000).optional(),
});

export const createArtifactUploadSchema = z.object({
  name: z.string().trim().min(1).max(255),
  contentType: z.string().trim().min(1).max(200),
  sizeBytes: z.number().int().positive(),
  sha256: z.string().regex(/^[a-f0-9]{64}$/i),
});

/**
 * 聊天附件的处理类型。
 * - image：多模态视觉输入（data URL 在 worker 侧从对象存储下载后生成）；
 * - text：小体积文本，worker 下载后内联进消息正文；
 * - file：其他二进制，run 启动前投进沙箱工作区，由 agent 工具读取。
 */
export const runAttachmentKindSchema = z.enum(['image', 'text', 'file']);
export type RunAttachmentKind = z.infer<typeof runAttachmentKindSchema>;

/**
 * 随 run 派发任务持久化的附件**引用**（对象存储键），不再内联 base64：
 * worker 按引用下载并按 kind 决定喂给模型 / 内联正文 / 投进沙箱。
 */
export const runAttachmentRefSchema = z.object({
  id: z.uuid(),
  kind: runAttachmentKindSchema,
  objectKey: z.string().min(1).max(600),
  filename: z.string().trim().min(1).max(255),
  contentType: z.string().trim().min(1).max(200),
  sizeBytes: z.number().int().nonnegative(),
  /** 对象存储内容编码（gzip 时 worker 下载后需先解压还原原文）。 */
  contentEncoding: z.enum(['gzip']).optional(),
});
export type RunAttachmentRef = z.infer<typeof runAttachmentRefSchema>;

/**
 * 前端初始化附件上传的请求体。服务端按原始文件信息判定 kind/限额并做秒传，
 * 再按实际存储字节（可能经 gzip 压缩）换发整文件或分片预签名 URL。
 */
export const initChatAttachmentSchema = z.object({
  filename: z.string().trim().min(1).max(255),
  contentType: z.string().trim().min(1).max(200),
  /** 原始文件大小。 */
  sizeBytes: z.number().int().positive(),
  /** 原始内容哈希，秒传/去重依据。 */
  contentSha256: z.string().regex(/^[a-f0-9]{64}$/i),
  /** 实际上传字节哈希（无压缩时与内容哈希相同）。 */
  storedSha256: z.string().regex(/^[a-f0-9]{64}$/i),
  /** 实际上传字节数。 */
  storedSizeBytes: z.number().int().positive(),
  contentEncoding: z.enum(['gzip']).optional(),
});
export type InitChatAttachmentInput = z.infer<typeof initChatAttachmentSchema>;

/** 分片上传完成时回传的分片清单；整文件上传时为空。 */
export const completeChatAttachmentSchema = z.object({
  parts: z
    .array(
      z.object({
        number: z.number().int().min(1).max(10_000),
        etag: z.string().trim().min(1).max(255),
      }),
    )
    .optional(),
});
export type CompleteChatAttachmentInput = z.infer<
  typeof completeChatAttachmentSchema
>;

/**
 * 随 Outbox/BullMQ payload 持久化的最小观测上下文。
 * 只携带 W3C Trace Context 和内部 request ID；不传播 baggage，
 * 不包含 prompt、用户 token 或请求体，旧 payload 缺省该字段仍可消费。
 */
export const observabilityContextSchema = z.object({
  traceparent: z.string().max(256).optional(),
  tracestate: z.string().max(512).optional(),
  requestId: z.string().max(128).optional(),
});

export const runJobSchema = z.discriminatedUnion('kind', [
  z.object({
    kind: z.literal('start'),
    tenantId: z.uuid(),
    userId: z.uuid(),
    sessionId: z.uuid(),
    runId: z.uuid(),
    message: z.string(),
    workspacePath: z.string(),
    workspaceSource: workspaceSourceSchema.optional(),
    approvalMode: z.enum(['manual', 'session']).optional(),
    knowledgeBaseIds: knowledgeBaseIdsSchema,
    attachments: z.array(runAttachmentRefSchema).max(5).default([]),
    observability: observabilityContextSchema.optional(),
  }),
  z.object({
    kind: z.literal('resume-approval'),
    tenantId: z.uuid(),
    userId: z.uuid(),
    sessionId: z.uuid(),
    runId: z.uuid(),
    workspacePath: z.string(),
    decision: approvalDecisionSchema,
    approvalMode: z.enum(['manual', 'session']).optional(),
    knowledgeBaseIds: knowledgeBaseIdsSchema,
    observability: observabilityContextSchema.optional(),
  }),
  z.object({
    kind: z.literal('resume-question'),
    tenantId: z.uuid(),
    userId: z.uuid(),
    sessionId: z.uuid(),
    runId: z.uuid(),
    workspacePath: z.string(),
    answer: questionAnswerSchema,
    approvalMode: z.enum(['manual', 'session']).optional(),
    knowledgeBaseIds: knowledgeBaseIdsSchema,
    observability: observabilityContextSchema.optional(),
  }),
]);

export type RunJob = z.infer<typeof runJobSchema>;
export type ObservabilityContextPayload = z.infer<typeof observabilityContextSchema>;

export interface AuthContext {
  userId: string;
  tenantId: string;
  roles: string[];
}

export const RUN_QUEUE_NAME = 'agent-runs';
export const MEMORY_QUEUE_NAME = 'agent-memory';
export const MEMORY_INDEX_QUEUE_NAME = 'agent-memory-index';
export const runEventsChannel = (runId: string) => `agent:run:${runId}:events`;
export const runCancellationChannel = (runId: string) => `agent:run:${runId}:cancel`;
