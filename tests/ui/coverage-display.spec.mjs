// SPDX-License-Identifier: MIT | Copyright (c) 2026 Yoni Ryabinski
import { test, expect } from '@playwright/test';

// A configuration set whose only senders already route BOUNCE/COMPLAINT
// through identity-level notifications is a note, not a red gap. The matrix,
// the section badge and the Markdown export must agree with the risk model.

async function fetchModel(request) {
  const res = await request.get('/api/report');
  expect(res.ok()).toBeTruthy();
  return res.json();
}

const TOPIC = 'arn:aws:sns:us-east-1:000000000000:feedback-topic';

function twoSets(model) {
  const m = structuredClone(model);
  m.config_sets = [
    {
      name: 'covered-set',
      sending_enabled: true,
      suppressed_reasons: null,
      reputation_metrics: true,
      event_types: ['DELIVERY', 'OPEN'],
      engagement_types: ['OPEN'],
      destinations: [{ name: 'opens', enabled: true, event_types: ['DELIVERY', 'OPEN'], type: 'sns', sns: TOPIC }],
      feedback_topics: [],
      delivery_topics: [TOPIC],
      forwards_feedback: false,
    },
    {
      name: 'open-set',
      sending_enabled: true,
      suppressed_reasons: null,
      reputation_metrics: true,
      event_types: ['DELIVERY'],
      engagement_types: [],
      destinations: [{ name: 'delivery', enabled: true, event_types: ['DELIVERY'], type: 'sns', sns: TOPIC }],
      feedback_topics: [],
      delivery_topics: [TOPIC],
      forwards_feedback: false,
    },
  ];
  m.suppression_coverage = [
    {
      config_set: 'covered-set',
      sending_enabled: true,
      routes_feedback: false,
      reaches_ledger: false,
      reaches_handler: false,
      delivery_to_ledger: true,
      engagement_types: ['OPEN'],
      feedback_topics: [],
      event_types: ['DELIVERY', 'OPEN'],
      destinations: 1,
      sends: 10,
      sends_recent: 10,
      last_send: '2026-10-24',
      usage: 'active',
      complaints: null,
      covered_by: ['cover.example'],
      risk: 'info',
    },
    {
      config_set: 'open-set',
      sending_enabled: true,
      routes_feedback: false,
      reaches_ledger: false,
      reaches_handler: false,
      delivery_to_ledger: true,
      engagement_types: [],
      feedback_topics: [],
      event_types: ['DELIVERY'],
      destinations: 1,
      sends: 10,
      sends_recent: 10,
      last_send: '2026-10-24',
      usage: 'active',
      complaints: null,
      covered_by: [],
      risk: 'medium',
    },
  ];
  m.identity_feedback = [{
    identity: 'cover.example',
    bounce_topic: TOPIC,
    complaint_topic: TOPIC,
    delivery_topic: null,
    forwarding_enabled: false,
  }];
  m.advisor = { ...(m.advisor || {}), findings: [] };
  return m;
}

test('covered BOUNCE/COMPLAINT gaps are notes, not red gaps', async ({ page, request }) => {
  const model = twoSets(await fetchModel(request));
  await page.route('**/api/report', (r) => r.fulfill({ json: model }));
  await page.goto('/');

  const section = page.locator('section').filter({ has: page.locator('#coverage') });
  const coveredRow = section.locator('table.matrix tbody tr', { hasText: 'covered-set' });
  const openRow = section.locator('table.matrix tbody tr', { hasText: 'open-set' });

  await expect(coveredRow).toBeVisible();
  await expect(openRow).toBeVisible();

  const coveredBounce = coveredRow.locator('td[data-label="BOUNCE"] .mx');
  await expect(coveredBounce).toHaveClass(/mx c/);
  await expect(coveredBounce).toHaveAttribute('title', /covered by identity notifications on cover\.example/);
  await expect(coveredRow.locator('td[data-label="BOUNCE"] .mx.n')).toHaveCount(0);
  await expect(openRow.locator('td[data-label="BOUNCE"] .mx.n')).toHaveCount(1);

  await expect(section).toContainText('1 gap');
  await expect(section).toContainText('1 set covered by identity');
  await expect(section).not.toContainText('2 gaps');

  // The covered row is a note: no Fix button. The real gap keeps its button.
  await expect(coveredRow.locator('button')).toHaveCount(0);
  await expect(openRow.locator('button')).toHaveCount(1);

  const md = await page.evaluate(() => buildMarkdown());
  expect(md).toContain('- `open-set` — risk medium');
  expect(md).not.toContain('- `covered-set` — risk');
  expect(md).toContain('- `covered-set` — covered by cover.example');
});
