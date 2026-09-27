import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import * as knowledgeManager from '../app/knowledge/knowledge-manager';

test('accepts supported document filenames', () => {
  const accepts = (knowledgeManager as typeof knowledgeManager & {
    isAcceptedKnowledgeFile?: (name: string) => boolean;
  }).isAcceptedKnowledgeFile;
  assert.equal(typeof accepts, 'function');

  assert.equal(accepts?.('guide.md'), true);
  assert.equal(accepts?.('guide.MARKDOWN'), true);
  assert.equal(accepts?.('notes.txt'), true);
  assert.equal(accepts?.('report.pdf'), true);
  assert.equal(accepts?.('report.docx'), true);
  assert.equal(accepts?.('sheet.xlsx'), true);
  assert.equal(accepts?.('archive.md.zip'), false);
  assert.equal(accepts?.('legacy.doc'), false);
  assert.equal(accepts?.('legacy.xls'), false);
});

test('prevents a second upload from starting for the same knowledge base', () => {
  const reserve = (knowledgeManager as typeof knowledgeManager & {
    reserveKnowledgeUpload?: (active: Set<string>, kbId: string) => boolean;
  }).reserveKnowledgeUpload;
  assert.equal(typeof reserve, 'function');

  const active = new Set<string>();
  assert.equal(reserve?.(active, 'kb-1'), true);
  assert.equal(reserve?.(active, 'kb-1'), false);
  assert.equal(reserve?.(active, 'kb-2'), true);
});

test('knowledge base cards expose counts, update time and management actions', () => {
  const Card = (knowledgeManager as typeof knowledgeManager & {
    KnowledgeBaseCard?: (props: Record<string, unknown>) => React.ReactNode;
  }).KnowledgeBaseCard;
  assert.equal(typeof Card, 'function');

  const html = renderToStaticMarkup(createElement(Card as React.ElementType, {
    base: {
      id: 'kb-1',
      name: '学生成绩知识库',
      description: '考试成绩与销售记录',
      graphEnabled: true,
      documentCount: 1,
      chunkCount: 10,
      updatedAt: '2026-07-01T19:22:00.000Z',
    },
    canWrite: true,
    onOpen: () => undefined,
    onEdit: () => undefined,
    onDelete: () => undefined,
  }));

  assert.match(html, /学生成绩知识库/);
  assert.match(html, /考试成绩与销售记录/);
  assert.match(html, /GraphRAG/);
  assert.match(html, /文档数量/);
  assert.match(html, /切片数量/);
  assert.match(html, /更新于/);
  assert.match(html, /编辑/);
  assert.match(html, /删除/);
});

test('knowledge base cards without write permission hide management actions', () => {
  const Card = (knowledgeManager as typeof knowledgeManager & {
    KnowledgeBaseCard?: (props: Record<string, unknown>) => React.ReactNode;
  }).KnowledgeBaseCard;

  const html = renderToStaticMarkup(createElement(Card as React.ElementType, {
    base: { id: 'kb-ro', name: '只读知识库', graphEnabled: false, documentCount: 0, chunkCount: 0 },
    canWrite: false,
    onOpen: () => undefined,
    onEdit: () => undefined,
    onDelete: () => undefined,
  }));

  assert.match(html, /只读知识库/);
  assert.match(html, /向量检索/);
  assert.doesNotMatch(html, /aria-label="编辑/);
});

test('keeps upload success when refreshing the document list fails', async () => {
  const finishUpload = (knowledgeManager as typeof knowledgeManager & {
    finishKnowledgeUpload?: (
      fileName: string,
      upload: () => Promise<void>,
      refreshDocuments: () => Promise<void>,
    ) => Promise<{ kind: string; message: string }>;
  }).finishKnowledgeUpload;
  assert.equal(typeof finishUpload, 'function');

  const state = await finishUpload?.(
    'guide.md',
    async () => undefined,
    async () => { throw new Error('list unavailable'); },
  );

  assert.equal(state?.kind, 'error');
  assert.match(state?.message ?? '', /guide\.md 已上传/);
  assert.match(state?.message ?? '', /文档列表刷新失败/);
  assert.doesNotMatch(state?.message ?? '', /guide\.md 上传失败/);
});

test('loads each knowledge base document list independently', async () => {
  const loadDocumentLists = (knowledgeManager as typeof knowledgeManager & {
    loadKnowledgeDocumentLists?: (
      bases: Array<{ id: string }>,
      fetchDocuments: (kbId: string) => Promise<Array<{ id: string; name: string }>>,
    ) => Promise<{
      documents: Record<string, Array<{ id: string; name: string }>>;
      errors: Record<string, string>;
    }>;
  }).loadKnowledgeDocumentLists;
  assert.equal(typeof loadDocumentLists, 'function');

  const result = await loadDocumentLists?.(
    [{ id: 'kb-ok' }, { id: 'kb-failed' }],
    async (kbId) => {
      if (kbId === 'kb-failed') throw new Error('list unavailable');
      return [{ id: 'document-1', name: 'guide.md' }];
    },
  );

  assert.deepEqual(result?.documents, {
    'kb-ok': [{ id: 'document-1', name: 'guide.md' }],
  });
  assert.deepEqual(Object.keys(result?.errors ?? {}), ['kb-failed']);
});
