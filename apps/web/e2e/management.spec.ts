import { test, expect, adminUser, mockAuth, mockJson } from './fixtures';

const knowledgeBase = {
  id: 'kb-1',
  name: 'E2E 知识库',
  description: '用于浏览器回归验证',
  visibility: 'private',
  status: 'ready',
  owner_user_id: adminUser.id,
  document_count: 1,
  chunk_count: 1,
  graph_enabled: true,
  chunk_size: 800,
  chunk_overlap: 80,
  top_k: 10,
  created_at: '2026-09-22T00:00:00.000Z',
  updated_at: '2026-09-22T00:00:00.000Z',
};

test('知识库可以进入文档、切片和检索页面', async ({ page }) => {
  await mockAuth(page, adminUser);
  await mockJson(page, '**/api/knowledge-bases', { data: [knowledgeBase] });
  await mockJson(page, '**/api/knowledge-bases/kb-1/documents', {
    data: [{
      id: 'doc-1', kb_id: 'kb-1', name: 'guide.md', mime: 'text/markdown', status: 'ready',
      size_bytes: 128, chunk_count: 1, error_message: null, indexed_at: '2026-09-22T00:00:00.000Z',
      directory: '', created_at: '2026-09-22T00:00:00.000Z', updated_at: '2026-09-22T00:00:00.000Z',
    }],
  });
  await mockJson(page, '**/api/knowledge-bases/kb-1/documents/doc-1/chunks*', {
    data: [{
      id: 'chunk-1', document_id: 'doc-1', ordinal: 1, text: 'E2E chunk content', token_count: 3,
      heading: null, metadata: {}, created_at: '2026-09-22T00:00:00.000Z', document_name: 'guide.md',
    }],
    total: 1,
  });
  await mockJson(page, '**/api/knowledge-bases/kb-1/assets*', { data: [] });
  await page.route('**/api/knowledge-bases/kb-1/retrieval', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        retrievalId: 'retrieval-1',
        citations: [{
          chunkId: 'chunk-1', documentId: 'doc-1', documentName: 'guide.md', ordinal: 0,
          score: 0.92, via: 'vector', passage: 'E2E retrieval result',
        }],
        relations: [],
        stats: { vectorHits: 0, graphHops: 0, durationMs: 2 },
      }),
    });
  });

  await page.goto('/knowledge');
  await expect(page.getByText('知识库平台', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'E2E 知识库' })).toBeVisible();
  await page.getByRole('heading', { name: 'E2E 知识库' }).click();

  await expect(page.getByRole('heading', { name: '文档管理' })).toBeVisible();
  await page.getByRole('button', { name: '切片管理' }).click();
  await expect(page.getByRole('heading', { name: '切片管理' })).toBeVisible();
  await expect(page.getByText('E2E chunk content')).toBeVisible();

  await page.getByRole('button', { name: '知识检索' }).click();
  await expect(page.getByRole('heading', { name: '知识检索' })).toBeVisible();
  await page.getByLabel('知识检索').fill('E2E query');
  await page.getByLabel('知识检索').press('Enter');
  await expect(page.getByText('2 ms')).toBeVisible();
});

test('管理员用户页和长期记忆页加载数据', async ({ page }) => {
  await mockAuth(page, adminUser);
  await mockJson(page, '**/api/admin/users', {
    data: [{ id: 'user-1', username: 'e2e-user', displayName: 'E2E 用户', role: 'member', tenantId: 'e2e-tenant' }],
  });
  await mockJson(page, '**/api/knowledge-bases', { data: [] });
  await mockJson(page, '**/api/agent/memories?*', {
    data: [{ id: 'memory-1', kind: 'preference', scope: 'global', content: '喜欢简洁答案', importance: 0.8, confidence: 0.9, updatedAt: '2026-09-22T00:00:00.000Z' }],
  });

  await page.goto('/admin/users');
  await expect(page.getByRole('heading', { name: '用户管理' })).toBeVisible();
  await expect(page.getByText('E2E 用户')).toBeVisible();

  await page.goto('/memory');
  await expect(page.getByRole('heading', { name: '长期记忆' })).toBeVisible();
  await expect(page.getByText('喜欢简洁答案')).toBeVisible();
});
