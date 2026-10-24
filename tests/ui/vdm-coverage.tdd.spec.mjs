// SPDX-License-Identifier: MIT | Copyright (c) 2026 Yoni Ryabinski
// Acceptance scenarios from tdd/vdm-coverage.tdd.yaml (VDM-equivalent coverage, VDM off).
// Runs against demo mode; scenario IDs stay in the test names so QA maps results back.
import { test, expect } from '@playwright/test';

async function fetchModel(request) {
  const res = await request.get('/api/report');
  expect(res.ok()).toBeTruthy();
  return res.json();
}

test.describe('REQ-003 — Mailbox providers table, Range-scoped, with the 1,000-delivery floor', () => {
  test('SC-003-e Mailbox providers section renders with rates and the floor note', async ({ page }) => {
    await page.goto('/');
    const rows = page.locator('#providerTable tbody tr');
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThanOrEqual(3);
    const head = await page.locator('#providerTable thead').innerText();
    for (const col of ['DELIVERIES', 'COMPLAINTS', 'COMPLAINT RATE', 'BOUNCES', 'BOUNCE RATE']) expect(head.toUpperCase()).toContain(col);
    await expect(page.locator('#providerTable')).toContainText('too few deliveries');
    const firstDeliveries = async () => (await page.locator('#providerTable tbody tr:first-child td:nth-child(2)').innerText()).trim();
    const all = await firstDeliveries();
    await page.click('[data-range="30"]');
    await expect.poll(firstDeliveries).not.toBe(all);
  });
});

test.describe('REQ-010 — Every Fix button copies an agent-ready Markdown brief', () => {
  test('SC-010-d Clicking Fix copies the Markdown brief and names the resource', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto('/');
    const btn = page.locator('.attn-item [data-fix]').first();
    await expect(btn).toBeVisible();
    await btn.click();
    const clip = await page.evaluate(() => navigator.clipboard.readText());
    const resource = (await page.locator('#toast').innerText()).replace(/^Fix brief for /, '').replace(/ copied as Markdown$/, '');
    expect(clip.startsWith('# ')).toBeTruthy();
    expect(clip).toContain('## Commands');
    expect(resource.length).toBeGreaterThan(0);
    expect(clip).toContain(resource);
  });
});

test.describe('REQ-011 — Open and click rates with the Apple MPP caveat', () => {
  test('SC-011-c Engagement columns and MPP caveat are visible', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#providerTable tbody tr').first()).toBeVisible();
    const head = (await page.locator('#providerTable thead').innerText()).toUpperCase();
    expect(head).toContain('OPEN RATE');
    expect(head).toContain('CLICK RATE');
    await expect(page.locator('#providersSection')).toContainText('Opens inflated by Apple Mail Privacy Protection');
  });
});

test.describe('REQ-013 — Missing data is explained', () => {
  test('SC-013-d No delivery data yet is stated with a fix brief', async ({ page, request }) => {
    const model = await fetchModel(request);
    for (const w of Object.values(model.breakdowns.ranges)) {
      w.state = 'no_delivery_data';
      for (const k of ['providers', 'identities', 'config_sets'])
        w[k] = w[k].map((r) => ({ ...r, deliveries: 0, opens: 0, clicks: 0, complaint_rate: null, bounce_rate: null, open_rate: null, click_rate: null, rate_note: 'no delivery data yet' }));
    }
    model.breakdowns.state = 'no_delivery_data';
    await page.route('**/api/report', (r) => r.fulfill({ json: model }));
    await page.goto('/');
    await expect(page.locator('#providersSection')).toContainText('No delivery data yet');
    await expect(page.locator('#providerTable')).toContainText('no delivery data yet');
    expect(await page.locator('#providersSection [data-fix]').count()).toBeGreaterThanOrEqual(1);
  });
});
