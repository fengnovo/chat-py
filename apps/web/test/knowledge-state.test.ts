import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { KnowledgeBaseMenu, toggleKnowledgeBase, knowledgeBaseIdsForChat, persistKnowledgeBaseIds } from '../app/components/resilient-chat/knowledge-base-picker';
import { messagesFromHistory } from '../app/components/resilient-chat/utils';

test('knowledge picker toggles one id without dropping other selections', () => {
  assert.deepEqual(toggleKnowledgeBase(['a', 'b'], 'a'), ['b']);
  assert.deepEqual(toggleKnowledgeBase(['a', 'b'], 'c'), ['a', 'b', 'c']);
});

test('knowledge picker distinguishes never-set (null) from explicitly empty', () => {
  const storage = new Map<string, string>();
  assert.equal(knowledgeBaseIdsForChat('chat-1', storage), null);
  storage.set('knowledge-bases:chat-1', JSON.stringify(['kb-1']));
  assert.deepEqual(knowledgeBaseIdsForChat('chat-1', storage), ['kb-1']);
  assert.equal(knowledgeBaseIdsForChat('chat-2', storage), null);
  persistKnowledgeBaseIds('chat-2', [], storage);
  assert.deepEqual(knowledgeBaseIdsForChat('chat-2', storage), []);
});

test('knowledge menu links to the knowledge management page', () => {
  const html = renderToStaticMarkup(createElement(KnowledgeBaseMenu, {
    bases: [{ id: 'kb-1', name: 'keen-test' }],
    value: ['kb-1'],
    onToggle: () => undefined,
    onChangeAll: () => undefined,
  }));

  assert.match(html, /href="\/knowledge"/);
  assert.match(html, /管理知识库/);
});

test('history citation part is available to message rendering', () => {
  const [message] = messagesFromHistory([{
    id: 'message-1', runId: 'run-1', role: 'assistant', text: 'answer', createdAt: new Date().toISOString(),
    citations: [{ chunkId: 'c', kbId: 'kb-1', documentId: 'd', documentName: 'doc.md', ordinal: 1, score: 0.8, via: 'graph' }],
  }]);
  assert.deepEqual(message.parts.at(-1), { type: 'data-citations', data: { citations: [{ chunkId: 'c', kbId: 'kb-1', documentId: 'd', documentName: 'doc.md', ordinal: 1, score: 0.8, via: 'graph' }] } });
});
