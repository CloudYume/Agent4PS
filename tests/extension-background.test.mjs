import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

test('code write requires the active tab, live run command, and visible matching problem', async () => {
  const source = fs.readFileSync(path.resolve('extension/background.js'), 'utf8');
  let listener;
  let activeId = 8;
  let commandLive = true;
  let authorizations = 0;
  const writes = [];
  const model = {
    getLanguageId: () => 'python3',
    getValue: () => writes.at(-1) || '',
    setValue: (code) => writes.push(code),
  };
  const document = { visibilityState: 'visible' };
  const location = { pathname: '/problems/two-sum/' };
  const chrome = {
    runtime: { onMessage: { addListener(callback) { listener = callback; } } },
    storage: { local: { get: async () => ({ bridgeToken: 'token', bridgePort: 8765 }) } },
    tabs: { query: async () => [{ id: activeId }] },
    scripting: { executeScript: async ({ func, args }) => [{ result: func(...args) }] },
  };
  const fetch = async (url, options) => {
    assert.match(url, /\/v1\/command-active$/);
    assert.equal(JSON.parse(options.body).tab_id, 7);
    authorizations++;
    return { status: 200, json: async () => ({ active: commandLive }) };
  };
  const window = { monaco: { editor: { getModels: () => [model] } } };
  vm.runInNewContext(source, { chrome, fetch, window, document, location });
  const sender = { tab: { id: 7, url: 'https://leetcode.cn/problems/two-sum/' } };
  const send = (message) => new Promise((resolve) => listener(message, sender, resolve));
  const message = {
    type: 'WRITE_CODE', code: 'class Solution: pass', command_id: 'a'.repeat(24),
    slug: 'two-sum', document_id: 'document-1',
  };

  const inactive = await send(message);
  assert.equal(inactive.ok, false);
  assert.match(inactive.error, /another Edge tab/);
  assert.equal(authorizations, 0);
  assert.equal(writes.length, 0);

  activeId = 7;
  commandLive = false;
  const expired = await send(message);
  assert.equal(expired.ok, false);
  assert.match(expired.error, /expired/);
  assert.equal(writes.length, 0);

  commandLive = true;
  document.visibilityState = 'hidden';
  const hidden = await send(message);
  assert.equal(hidden.ok, false);
  assert.match(hidden.error, /document hidden/);
  assert.equal(writes.length, 0);

  document.visibilityState = 'visible';
  location.pathname = '/problems/add-two-numbers/';
  const moved = await send(message);
  assert.equal(moved.ok, false);
  assert.match(moved.error, /left the expected problem/);
  assert.equal(writes.length, 0);

  location.pathname = '/problems/two-sum/';
  const active = await send(message);
  assert.equal(active.ok, true);
  assert.deepEqual(writes, ['class Solution: pass\n']);
});

test('background restores a missing or stale token without a pairing code', async () => {
  const source = fs.readFileSync(path.resolve('extension/background.js'), 'utf8');
  let listener;
  const stored = { bridgePort: 8765 };
  const requests = [];
  const chrome = {
    runtime: { onMessage: { addListener(callback) { listener = callback; } } },
    storage: { local: {
      async get(keys) {
        return Object.fromEntries(keys.map((key) => [key, stored[key]]));
      },
      async set(values) { Object.assign(stored, values); },
      async remove(key) { delete stored[key]; },
    } },
  };
  const fetch = async (url, options) => {
    requests.push({ url, authorization: options.headers.Authorization });
    if (url.endsWith('/v1/reconnect')) {
      return { ok: true, status: 200, json: async () => ({ token: 'restored-token' }) };
    }
    assert.match(url, /\/v1\/status$/);
    return options.headers.Authorization === 'Bearer restored-token'
      ? { status: 200, json: async () => ({ connected: false }) }
      : { status: 401, json: async () => ({ error: 'not paired' }) };
  };
  vm.runInNewContext(source, { chrome, fetch });
  const send = (message) => new Promise((resolve) => listener(message, {}, resolve));

  assert.equal((await send({ type: 'STATUS' })).paired, true);
  assert.equal(stored.bridgeToken, 'restored-token');
  stored.bridgeToken = 'stale-token';
  assert.equal((await send({ type: 'STATUS' })).paired, true);
  assert.equal(stored.bridgeToken, 'restored-token');
  assert.deepEqual(requests.map((request) => request.url.split('/').at(-1)), [
    'reconnect', 'status', 'status', 'reconnect', 'status',
  ]);
});
