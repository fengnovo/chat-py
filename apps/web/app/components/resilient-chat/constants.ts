import type { PipelineEvent } from './types';

const initialTrace: PipelineEvent[] = [
  {
    id: 'boot-transport',
    stage: 'transport',
    status: 'success',
    title: '持久化事件流已就绪',
    detail: 'SSE 断流后将从数据库 cursor 续传',
    timestamp: '--:--:--',
  },
  {
    id: 'boot-circuit',
    stage: 'circuit',
    status: 'success',
    title: '共享熔断器已连接',
    detail: 'Redis 在 Worker 之间共享模型健康状态',
    timestamp: '--:--:--',
  },
  {
    id: 'boot-verify',
    stage: 'verify',
    status: 'success',
    title: '租户与工作区隔离已加载',
    detail: 'API 鉴权、队列和独立 workspace 正在保护运行',
    timestamp: '--:--:--',
  },
];

export { initialTrace };
