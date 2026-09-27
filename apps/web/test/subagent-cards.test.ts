import assert from 'node:assert/strict';
import test from 'node:test';

import {
  foldSubagentCard,
  subagentCardsFromMessages,
} from '../app/components/resilient-chat/utils';
import type { SubagentEvent } from '../app/components/resilient-chat/types';

type ReviewedEvent = Extract<SubagentEvent, { type: 'subagent.reviewed' }>;

const started = {
  runId: 'run-1',
  timestamp: new Date().toISOString(),
  type: 'subagent.started',
  subagentId: 'sub-1',
  role: '依赖调研员',
  description: '检索依赖的最新版本',
  attempt: 1,
} as const;

const completed = {
  runId: 'run-1',
  timestamp: new Date().toISOString(),
  type: 'subagent.completed',
  subagentId: 'sub-1',
  attempt: 1,
  status: 'completed',
  summary: '已确认版本',
  toolCalls: 3,
  durationMs: 12_345,
} as const;

const reviewFailed = {
  runId: 'run-1',
  timestamp: new Date().toISOString(),
  type: 'subagent.reviewed',
  subagentId: 'sub-1',
  attempt: 1,
  passed: false,
  score: 55,
  feedback: '缺少来源 URL',
  checklist: [{ item: '含来源 URL', met: false }],
} as ReviewedEvent;

const started2 = { ...started, attempt: 2, description: '补齐来源 URL 后重派' } as const;

const completed2 = {
  ...completed,
  attempt: 2,
  summary: '已确认版本并补齐 URL',
  toolCalls: 5,
  durationMs: 8_000,
} as const;

const reviewPassed2 = {
  ...reviewFailed,
  attempt: 2,
  passed: true,
  score: 92,
  feedback: '',
  checklist: [{ item: '含来源 URL', met: true }],
} as ReviewedEvent;

test('started creates a running card and completed folds the result in place', () => {
  const running = foldSubagentCard([], started);
  assert.equal(running.length, 1);
  assert.equal(running[0].status, 'running');
  assert.equal(running[0].role, '依赖调研员');
  assert.equal(running[0].summary, null);

  const done = foldSubagentCard(running, completed);
  assert.equal(done.length, 1);
  assert.equal(done[0].status, 'completed');
  assert.equal(done[0].summary, '已确认版本');
  assert.equal(done[0].toolCalls, 3);
  assert.equal(done[0].durationMs, 12_345);
});

test('orphan completed events are ignored and duplicate starts stay idempotent', () => {
  assert.equal(foldSubagentCard([], completed).length, 0);

  const once = foldSubagentCard([], started);
  const twice = foldSubagentCard(once, { ...started, description: '修正后的任务' });
  assert.equal(twice.length, 1);
  assert.equal(twice[0].description, '修正后的任务');
});

test('cards rebuild from persisted message parts after a page reload', () => {
  const messages = [
    {
      id: 'message-1',
      role: 'assistant',
      parts: [
        { type: 'data-subagent', data: started },
        { type: 'data-subagent', data: completed },
      ],
    },
  ] as never;

  const cards = subagentCardsFromMessages(messages);
  assert.equal(cards.length, 1);
  assert.equal(cards[0].status, 'completed');
  assert.equal(cards[0].subagentId, 'sub-1');
});

test('failed review triggers retry state and keeps the full review trail', () => {
  // 孤立 reviewed 事件（没有 started）忽略。
  assert.equal(foldSubagentCard([], reviewFailed).length, 0);

  let cards = foldSubagentCard([], started);
  cards = foldSubagentCard(cards, completed);
  cards = foldSubagentCard(cards, reviewFailed);
  assert.equal(cards[0].reviews.length, 1);
  assert.equal(cards[0].reviews[0].passed, false);
  assert.equal(cards[0].reviews[0].feedback, '缺少来源 URL');

  // 第 2 轮 started：卡片回到运行中、清空该轮结果，但保留第 1 轮评审。
  cards = foldSubagentCard(cards, started2);
  assert.equal(cards[0].status, 'running');
  assert.equal(cards[0].attempt, 2);
  assert.equal(cards[0].summary, null);
  assert.equal(cards[0].reviews.length, 1);

  // 第 2 轮完成 + 评审通过：两轮评审轨迹都在。
  cards = foldSubagentCard(cards, completed2);
  cards = foldSubagentCard(cards, reviewPassed2);
  assert.equal(cards[0].status, 'completed');
  assert.equal(cards[0].summary, '已确认版本并补齐 URL');
  assert.deepEqual(
    cards[0].reviews.map((review) => [review.attempt, review.passed]),
    [[1, false], [2, true]],
  );
});

test('background flag marks cards and survives retry restarts', () => {
  const bgStarted = { ...started, subagentId: 'bg-1', background: true } as const;
  let cards = foldSubagentCard([], bgStarted);
  assert.equal(cards[0].background, true);

  // 同步 started 缺省 background 字段时按 false 处理（兼容旧事件/刷新重建）。
  const syncCards = foldSubagentCard([], started);
  assert.equal(syncCards[0].background, false);

  // 评审重派时 background 标记随卡片保留。
  cards = foldSubagentCard(cards, { ...completed, subagentId: 'bg-1' });
  cards = foldSubagentCard(cards, { ...reviewFailed, subagentId: 'bg-1' });
  cards = foldSubagentCard(cards, { ...started2, subagentId: 'bg-1' });
  assert.equal(cards[0].background, true);
});

test('review trail rebuilds from persisted parts after reload mid-retry', () => {
  const messages = [
    {
      id: 'message-1',
      role: 'assistant',
      parts: [
        { type: 'data-subagent', data: started },
        { type: 'data-subagent', data: completed },
        { type: 'data-subagent', data: reviewFailed },
        { type: 'data-subagent', data: started2 },
      ],
    },
  ] as never;
  const cards = subagentCardsFromMessages(messages);
  assert.equal(cards.length, 1);
  assert.equal(cards[0].status, 'running');
  assert.equal(cards[0].attempt, 2);
  assert.equal(cards[0].reviews.length, 1);
  assert.equal(cards[0].reviews[0].passed, false);
});
