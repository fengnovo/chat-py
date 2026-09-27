import type { AgentEvent } from '@/lib/contracts';

import { updatePersistedCursor } from '@/app/lib/persistence';

import type { PipelineEvent } from './types';

function localEvent(
  stage: PipelineEvent['stage'],
  status: PipelineEvent['status'],
  title: string,
  detail: string,
): PipelineEvent {
  return {
    id: `local-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    stage,
    status,
    title,
    detail,
    timestamp: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
  };
}

function agentEventToTrace(event: AgentEvent): PipelineEvent | null {
  const base = {
    id: `${event.runId}-${event.timestamp}-${event.type}`,
    timestamp: new Date(event.timestamp).toLocaleTimeString('zh-CN', {
      hour12: false,
    }),
  };

  switch (event.type) {
    case 'assistant.delta':
      return null;
    case 'assistant.narration':
      return null;
    case 'assistant.reasoning':
      return null;
    case 'usage.updated':
      return null;
    case 'context.compressing':
      return null;
    case 'retrieval.completed':
      return { ...base, stage: 'verify', status: 'success', title: '已完成知识检索', detail: `${event.citations.length} 条引用 · ${event.stats.durationMs}ms` };
    case 'run.started':
      return {
        ...base,
        stage: 'request',
        status: 'running',
        title: 'Worker 已接管运行',
        detail: '队列任务已启动，正在隔离工作区中执行',
      };
    case 'model.retry':
      return {
        ...base,
        stage: 'retry',
        status: 'warning',
        title: `${event.model} 正在重试`,
        detail: `第 ${event.attempt} 次尝试将在 ${event.delayMs}ms 后执行 · ${event.reason}`,
      };
    case 'model.fallback':
      return {
        ...base,
        stage: 'fallback',
        status: 'warning',
        title: `模型已切换至 ${event.to}`,
        detail: `${event.from} 暂不可用 · ${event.reason}`,
      };
    case 'tool.started':
      return {
        ...base,
        stage: 'request',
        status: 'running',
        title: `正在调用 ${event.tool}`,
        detail: `tool invocation ${event.invocationId.slice(0, 8)}`,
      };
    case 'tool.completed':
      return {
        ...base,
        stage: 'verify',
        status: 'success',
        title: `${event.tool} 已完成`,
        detail: `tool invocation ${event.invocationId.slice(0, 8)}`,
      };
    case 'todo.updated': {
      const completed = event.todos.filter((todo) => todo.status === 'completed').length;
      return {
        ...base,
        stage: 'request',
        status: 'running',
        title: '任务计划已更新',
        detail: `${completed}/${event.todos.length} 项已完成`,
      };
    }
    case 'subagent.started':
      return {
        ...base,
        stage: 'request',
        status: 'running',
        title: event.background
          ? event.attempt > 1
            ? `后台子 Agent 整改重派 · 第 ${event.attempt} 轮 · ${event.role}`
            : `后台子 Agent 已启动 · ${event.role}`
          : event.attempt > 1
            ? `子 Agent 整改重派 · 第 ${event.attempt} 轮 · ${event.role}`
            : `子 Agent 已启动 · ${event.role}`,
        detail: event.description,
      };
    case 'subagent.completed': {
      // 摘要可能长达 2000 字，过程流里只显示简短结论；完整摘要在子 Agent 卡片里。
      const brief = event.summary.length > 160
        ? `${event.summary.slice(0, 159)}…`
        : event.summary;
      return {
        ...base,
        stage: 'verify',
        status: event.status === 'completed' ? 'success' : 'error',
        title:
          event.status === 'completed'
            ? event.attempt > 1
              ? `子 Agent 第 ${event.attempt} 轮已完成，等待评审`
              : '子 Agent 已完成，等待评审'
            : event.status === 'timeout'
              ? '子 Agent 已超时'
              : '子 Agent 执行失败',
        detail: brief,
      };
    }
    case 'subagent.reviewed': {
      const missed = event.checklist.filter((item) => !item.met);
      return {
        ...base,
        stage: 'verify',
        status: event.passed ? 'success' : 'warning',
        title: event.passed
          ? `第 ${event.attempt} 轮评审通过（${event.score} 分）`
          : `第 ${event.attempt} 轮评审未达标（${event.score} 分）`,
        detail: event.passed
          ? '子任务验收通过'
          : event.feedback ||
            (missed.length > 0
              ? `未满足：${missed.map((item) => item.item).join('；')}`.slice(0, 160)
              : '未达标，准备整改重派'),
      };
    }
    case 'approval.required':
      return {
        ...base,
        stage: 'verify',
        status: 'warning',
        title: '等待人工审批',
        detail: `${event.actions.length} 个高风险操作需要确认`,
      };
    case 'question.required':
      return {
        ...base,
        stage: 'verify',
        status: 'warning',
        title: 'Agent 正在等待你的选择',
        detail: event.question.question,
      };
    case 'artifact.created':
      return {
        ...base,
        stage: 'verify',
        status: 'success',
        title: `已保存产物 ${event.name}`,
        detail: event.contentType,
      };
    case 'run.completed':
      return {
        ...base,
        stage: 'done',
        status: 'success',
        title: '运行已完成',
        detail: '事件、检查点和最终状态已持久化',
      };
    case 'run.cancelled':
      return {
        ...base,
        stage: 'done',
        status: 'warning',
        title: '运行已取消',
        detail: 'Worker 已收到取消信号并释放资源',
      };
    case 'run.failed':
      return {
        ...base,
        stage: 'done',
        status: 'error',
        title: `运行失败 · ${event.code}`,
        detail: event.message,
      };
  }
}

function countSseFrames(text: string) {
  return text
    .split('\n\n')
    .slice(0, -1)
    .filter((frame) => frame.trimStart().startsWith('data:')).length;
}

function createTrackedFetch(userId: string): typeof fetch {
  return async (input, init) => {
    const response = await fetch(input, init);
    // 与 apiFetch 保持一致：会话过期时跳转登录页（/api/chat 也受鉴权保护）。
    if (
      response.status === 401 &&
      !window.location.pathname.startsWith('/login')
    ) {
      window.location.assign('/login');
    }
    if (!response.ok || !response.body) return response;

    const runId = response.headers.get('x-workflow-run-id');
    if (!runId) return response;

    const requestUrl =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(requestUrl, window.location.origin);
    const headers = new Headers(init?.headers);
    const isPageResume = headers.get('x-page-resume') === '1';
    let cursor = isPageResume
      ? 0
      : Number(url.searchParams.get('startIndex') ?? '0');
    if (!Number.isFinite(cursor) || cursor < 0) cursor = 0;

    const decoder = new TextDecoder();
    let pending = '';
    const observed = response.body.pipeThrough(
      new TransformStream<Uint8Array, Uint8Array>({
        transform(chunk, controller) {
          // 第一优先级：必须把原始字节透传给下游，追踪逻辑绝不允许阻断主数据流。
          controller.enqueue(chunk);
          // 旁路追踪：任何异常都吞掉，保证上面的 enqueue 已经执行。
          try {
            pending += decoder.decode(chunk, { stream: true });
            const boundary = pending.lastIndexOf('\n\n');
            if (boundary === -1) return;
            const complete = pending.slice(0, boundary + 2);
            pending = pending.slice(boundary + 2);
            cursor += countSseFrames(complete);
            updatePersistedCursor(userId, runId, cursor);
          } catch {
            // 追踪逻辑出错不影响主流程
          }
        },
      }),
    );

    return new Response(observed, {
      headers: response.headers,
      status: response.status,
      statusText: response.statusText,
    });
  };
}

export { agentEventToTrace, countSseFrames, createTrackedFetch, localEvent };
