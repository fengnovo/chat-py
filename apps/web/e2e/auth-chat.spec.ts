import { test, expect, adminUser, mockAuth, mockJson } from './fixtures';

test('登录页展示 OAuth 错误，并能反馈密码登录失败', async ({ page }) => {
  await mockAuth(page, null);
  await mockJson(page, '**/api/auth/oauth/providers', { providers: [] });
  await page.route('**/api/auth/login', async (route) => {
    await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: 'invalid_credentials' }) });
  });

  await page.goto('/login?error=oauth_failed&detail=e2e');
  await expect(page.getByRole('heading', { name: '登录' })).toBeVisible();
  const loginError = page.locator('p.login-error');
  await expect(loginError).toContainText('第三方登录失败');

  await page.getByLabel('用户名').fill('wrong-user');
  await page.getByLabel('密码').fill('wrong-password');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(loginError).toHaveText('用户名或密码错误');
});

test('聊天页可切换历史会话、打开文件面板并预览图片灯箱', async ({ page }) => {
  await mockAuth(page, adminUser);
  await mockJson(page, '**/api/knowledge-bases', { data: [] });
  await mockJson(page, '**/api/agent/sessions?*', {
    data: [{
      id: 'session-1',
      title: '历史对话',
      externalKey: 'chat-1',
      createdAt: '2026-09-22T00:00:00.000Z',
      updatedAt: '2026-09-22T00:00:00.000Z',
    }],
    nextCursor: null,
  });
  await mockJson(page, '**/api/agent/sessions/session-1/history', {
    session: {
      id: 'session-1',
      title: '历史对话',
      externalKey: 'chat-1',
      createdAt: '2026-09-22T00:00:00.000Z',
      updatedAt: '2026-09-22T00:00:00.000Z',
    },
    messages: [{
      id: 'message-1',
      runId: 'run-1',
      role: 'user',
      text: '请看这张图片',
      createdAt: '2026-09-22T00:00:00.000Z',
      attachments: [{
        id: 'image-1',
        filename: 'sample.png',
        contentType: 'image/png',
        sizeBytes: 10,
        kind: 'image',
        url: '/e2e/sample.png',
      }],
    }],
    latestRun: null,
  });
  await mockJson(page, '**/api/agent/sessions/session-1/files', {
    files: [{ path: 'src/index.ts', content: 'export const answer = 42;', operation: 'write_file' }],
  });

  await page.goto('/');
  await expect(page.getByText('历史对话')).toBeVisible();
  await page.getByText('历史对话').click();
  await expect(page.getByText('请看这张图片')).toBeVisible();

  await page.getByRole('button', { name: '显示文件浏览器' }).click();
  const filePanel = page.getByRole('complementary', { name: 'AI 生成的文件' });
  await expect(filePanel).toBeVisible();
  await expect(filePanel).toContainText('index.ts');
  await expect(filePanel).toContainText('export const answer = 42;');

  await page.locator('button[title="查看大图：sample.png"]').click();
  const lightbox = page.getByRole('dialog', { name: '图片预览：sample.png' });
  await expect(lightbox).toBeVisible();
  await page.getByRole('button', { name: '放大' }).click();
  await expect(lightbox.getByText('125%')).toBeVisible();
  await lightbox.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(lightbox).toBeHidden();

  await page.getByRole('button', { name: '关闭文件面板' }).click();
  await expect(filePanel).toBeHidden();
});
