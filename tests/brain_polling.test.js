// Run: node --test tests/brain_polling.test.js
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../services/chat/static/brain.html'), 'utf8');
const code = html.slice(html.indexOf('async function loadJobs()'), html.indexOf('// ── Graph'));
const escapeCode = html.slice(html.indexOf('function esc('), html.indexOf('function domainColor('));

function setup(fetch, active = true) {
  const timers = new Map();
  let next = 0;
  const tbody = { innerHTML: '' };
  const context = vm.createContext({
    fetch, state: { jobPollTimer: null },
    document: { getElementById: id => id === 'panel-upload'
      ? { classList: { contains: () => active } } : tbody },
    setTimeout: (fn, delay) => { const id = ++next; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(escapeCode + code, context);
  return { context, timers, tbody };
}

for (const [label, fetch] of [
  ['empty queue', async () => ({ ok: true, json: async () => ({ jobs: [] }) })],
  ['HTTP error', async () => ({ ok: false })],
  ['network failure', async () => { throw new Error('offline'); }],
]) {
  test(`polling retries after ${label}`, async () => {
    const { context, timers } = setup(fetch);
    await context.loadJobs();
    assert.equal(timers.size, 1);
    assert.equal([...timers.values()][0].delay, 3000);
    await [...timers.values()][0].fn();
    assert.equal(timers.size, 1, 'no duplicate polling timers');
  });
}

test('completed and skipped queues still discover new uploads', async () => {
  const { context, timers, tbody } = setup(async () => ({ ok: true,
    json: async () => ({ jobs: [{ status: 'completed' }, { status: 'skipped' }] }) }));
  await context.loadJobs();
  assert.equal(timers.size, 1);
  assert.match(tbody.innerHTML, /completed/);
  assert.match(tbody.innerHTML, /skipped/);
});

test('inactive upload tab does not schedule more polling', async () => {
  const { context, timers } = setup(async () => ({ ok: false }), false);
  await context.loadJobs();
  assert.equal(timers.size, 0);
});

test('job metadata is escaped, failed status uses actual DB status', async () => {
  const { context, tbody } = setup(async () => ({ ok: true,
    json: async () => ({ jobs: [{ source_path: '<img src=x onerror=alert(1)>',
      domain: '<b>domain</b>', status: 'failed', created_notes: ['A'] }] }) }));
  await context.loadJobs();
  assert.doesNotMatch(tbody.innerHTML, /<img|<b>/);
  assert.match(tbody.innerHTML, /&lt;img/);
  assert.match(tbody.innerHTML, /status-badge failed/);
});
