// SPDX-License-Identifier: MIT | Copyright (c) 2026 Yoni Ryabinski
import fs from 'node:fs';
import { test, expect } from '@playwright/test';

// End-to-end contracts for §4.2 "Recipient-level complaint records".
// Runs against demo mode (deterministic synthetic data, port 8799 by default).

const TABLE = '#recordTable';

async function fetchModel(request) {
  const res = await request.get('/api/report');
  expect(res.ok()).toBeTruthy();
  return res.json();
}

const unbreakable = (n) => 'Xk'.repeat(Math.ceil(n / 2)).slice(0, n);

function parseCsv(text) {
  const rows = [];
  let row = [];
  let cur = '';
  let quoted = false;
  for (const ch of text) {
    if (quoted) {
      if (ch === '"') quoted = false;
      else cur += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') {
      row.push(cur);
      cur = '';
    } else if (ch === '\n') {
      row.push(cur);
      rows.push(row);
      row = [];
      cur = '';
    } else cur += ch;
  }
  if (cur !== '' || row.length) {
    row.push(cur);
    rows.push(row);
  }
  return rows;
}

test.describe('recipient-level complaint records (§4.2)', () => {
  test('fits the content column without horizontal scrolling on desktop', async ({ page }) => {
    for (const width of [1440, 1280, 1024]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      await expect(page.locator(`${TABLE} tbody tr`).first()).toBeVisible();
      const { client, scroll } = await page.evaluate(() => {
        const wrap = document.querySelector('#recordTable .tablewrap');
        return { client: wrap.clientWidth, scroll: wrap.scrollWidth };
      });
      expect(scroll, `record table overflows horizontally at ${width}px`).toBeLessThanOrEqual(client + 1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }
  });

  test('stays usable when it does scroll: pinned address column, edge fade, keyboard scroll', async ({ page, request }) => {
    const model = await fetchModel(request);
    const rows = Array.from({ length: 6 }, (_, i) => ({
      email: `user${i}${unbreakable(40)}@${unbreakable(40)}.example.com`,
      store: unbreakable(52),
      source: unbreakable(40),
      sending_identity: unbreakable(34),
      detail: `Permanent/${unbreakable(70)}`,
      event_type: 'BOUNCE',
      first_seen: '2026-01-02T03:04:05+00:00',
      last_seen: '2026-02-03T04:05:06+00:00',
      hits: i,
      simulator: false,
    }));
    await page.route('**/api/report', (route) => route.fulfill({ json: { ...model, complaint_records: rows } }));
    await page.setViewportSize({ width: 760, height: 900 });
    await page.goto('/');

    const wrap = page.locator(`${TABLE} .tablewrap`);
    const box = page.locator(`${TABLE} .tablebox`);
    await expect(page.locator(`${TABLE} tbody tr`).first()).toBeVisible();

    // Long unbreakable content forces the scroll path this narrow.
    expect(await wrap.evaluate((w) => w.scrollWidth - w.clientWidth)).toBeGreaterThan(50);
    await expect(box).toHaveClass(/more-right/);
    await expect(wrap).toHaveAttribute('tabindex', '0');
    await expect(wrap).toHaveAttribute('aria-label', /^Scrollable table/);

    // Scrolling right keeps the recipient address pinned and shows the boundary.
    const before = await page.locator(`${TABLE} tbody tr td:first-child`).first().boundingBox();
    await wrap.evaluate((w) => { w.scrollLeft = 200; });
    await expect(wrap).toHaveClass(/scrolled-x/);
    const after = await page.locator(`${TABLE} tbody tr td:first-child`).first().boundingBox();
    expect(Math.abs(after.x - before.x)).toBeLessThanOrEqual(2);

    // The edge fade disappears at the far end.
    await wrap.evaluate((w) => { w.scrollLeft = w.scrollWidth; });
    await expect(box).not.toHaveClass(/more-right/);

    // The focused region scrolls with the keyboard.
    await wrap.evaluate((w) => { w.scrollLeft = 0; w.focus(); });
    await page.keyboard.press('ArrowRight');
    await expect.poll(() => wrap.evaluate((w) => w.scrollLeft), { timeout: 2000 }).toBeGreaterThan(0);
  });

  test('renders labelled cards on a phone width with no page overflow', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await expect(page.locator(`${TABLE} tbody tr`).first()).toBeVisible();
    const m = await page.evaluate(() => {
      const wrap = document.querySelector('#recordTable .tablewrap');
      const tr = wrap.querySelector('tbody tr');
      return {
        display: getComputedStyle(wrap.querySelector('table')).display,
        labels: [...tr.querySelectorAll('td')].slice(1).map((td) => td.getAttribute('data-label')),
        docWidth: document.documentElement.scrollWidth,
        wrapScroll: wrap.scrollWidth - wrap.clientWidth,
      };
    });
    expect(m.display).toBe('block');
    expect(m.labels).toEqual(['Store', 'Feedback', 'Sending identity', 'Seen', 'Hits']);
    expect(m.docWidth).toBeLessThanOrEqual(390);
    expect(m.wrapScroll).toBeLessThanOrEqual(1);
  });

  test('sorts from the keyboard and exposes aria-sort', async ({ page, request }) => {
    const model = await fetchModel(request);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/');
    const th = page.locator(`${TABLE} thead th:nth-child(1)`);
    await expect(th).not.toHaveAttribute('aria-sort', /.*/);
    await page.locator(`${TABLE} thead th:nth-child(1) .thbtn`).focus();
    await page.keyboard.press('Enter');
    await expect(th).toHaveAttribute('aria-sort', 'ascending');
    const expected = model.complaint_records.map((r) => r.email).sort((a, b) => a.localeCompare(b))[0];
    await expect(page.locator(`${TABLE} tbody tr td:first-child`).first()).toContainText(expected);
    await page.keyboard.press('Enter');
    await expect(th).toHaveAttribute('aria-sort', 'descending');
  });

  test('keeps sorting available in the stacked phone layout', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await expect(page.locator(`${TABLE} tbody tr`).first()).toBeVisible();
    const sortSel = page.locator(`${TABLE} select.selsort`);
    const dirBtn = page.locator(`${TABLE} button.selsort`);
    await expect(sortSel).toBeVisible();
    await expect(dirBtn).toBeVisible();
    await expect(dirBtn).toBeDisabled();
    const firstStore = page.locator(`${TABLE} tbody tr td[data-label="Store"]`).first();
    await sortSel.selectOption('store');
    await expect(dirBtn).toBeEnabled();
    await expect(firstStore).toContainText('demo-accounts'); // ascending: accounts < email-suppressions
    await dirBtn.click();
    await expect(firstStore).toContainText('demo-email-suppressions');
  });

  test('CSV export carries every record field', async ({ page, request }) => {
    const model = await fetchModel(request);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/');
    await expect(page.locator(`${TABLE} tbody tr`).first()).toBeVisible();
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.locator(`${TABLE} button.ghost.mini:not(.selsort)`).click(),
    ]);
    expect(download.suggestedFilename()).toBe('ses-recipient-records.csv');
    const rows = parseCsv(fs.readFileSync(await download.path(), 'utf8'));
    expect(rows[0]).toEqual([
      'Recipient address', 'Mailbox simulator', 'Store', 'Source', 'Sending identity',
      'Event', 'Feedback detail', 'First seen', 'Last seen', 'Hits',
    ]);
    expect(rows).toHaveLength(model.complaint_records.length + 1);
    for (const row of rows.slice(1)) expect(row).toHaveLength(10);
  });
});
