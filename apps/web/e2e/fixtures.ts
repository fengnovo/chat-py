import { test as base, expect, type Page } from '@playwright/test';

export type E2EUser = {
  id: string;
  displayName: string;
  role: 'admin' | 'owner' | 'member';
  tenantId: string;
};

export const adminUser: E2EUser = {
  id: 'e2e-admin',
  displayName: 'E2E 管理员',
  role: 'admin',
  tenantId: 'e2e-tenant',
};

export const memberUser: E2EUser = {
  id: 'e2e-member',
  displayName: 'E2E 成员',
  role: 'member',
  tenantId: 'e2e-tenant',
};

export async function mockJson(
  page: Page,
  path: string | RegExp,
  body: unknown,
  status = 200,
) {
  await page.route(path, async (route) => {
    await route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
}

export async function mockAuth(page: Page, user: E2EUser | null = adminUser) {
  await mockJson(page, '**/api/auth/me', user ? { user } : {}, user ? 200 : 401);
}

export async function mockCommonUserApis(page: Page, user: E2EUser = adminUser) {
  await mockAuth(page, user);
  await mockJson(page, '**/api/knowledge-bases', { data: [] });
  await mockJson(page, '**/api/agent/sessions?*', { data: [], nextCursor: null });
  await mockJson(page, '**/api/knowledge-bases/*/documents', { data: [] });
  await mockJson(page, '**/api/agent/memories?*', { data: [] });
}

export const test = base;
export { expect };
