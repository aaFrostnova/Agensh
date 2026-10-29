import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MattermostListener } from '../dist/listeners/mattermost/listener.js';
import { groupMessagePayloads } from '../dist/group_messages.js';
import { Dispatcher } from '../dist/dispatcher.js';
import { renderSummary } from '../dist/uem_render.js';
const event = (id, channel = 'team', sender = 'peer', type = 'O') => ({
  event: 'posted', seq: 1,
  data: { post: JSON.stringify({ id, create_at: 1, edit_at: 0, user_id: sender,
      channel_id: channel, message: 'ordinary message without mention\n' + 'full text '.repeat(40) }),
    channel_type: type, sender_name: sender }, broadcast: { channel_id: channel },
});
test('ordinary team posts reach recipients and self echoes are excluded', async () => {
  const delivered = [];
  const listener = new MattermostListener({ agent: 'a', state: { ingest: async row => { delivered.push(row); return 1; } },
    baseUrl: 'http://localhost', token: 'test', channelIds: ['team'] });
  listener.selfId = 'a-uuid';
  await listener.ingestEvent(event('ordinary'));
  await listener.ingestEvent(event('self-name', 'team', 'a'));
  await listener.ingestEvent(event('self-id', 'team', 'a-uuid'));
  await listener.ingestEvent(event('outside', 'unrelated'));
  await listener.ingestEvent(event('kickoff', 'private-dm', 'controller', 'D'));
  assert.equal(delivered.length, 2);
  assert.equal(delivered[0].event_id, 'mattermost:post:ordinary:0');
  assert.equal(delivered[1].kind, 'dm');
  process.env.TROY_PRESERVE_MESSAGE_TEXT = '1';
  try { assert.ok(renderSummary(delivered[0]).includes('full text '.repeat(40))); }
  finally { delete process.env.TROY_PRESERVE_MESSAGE_TEXT; }
});
test('backlog stores on host but advertises only worker paths', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'v070-router-'));
  try {
    const rows = Array.from({ length: 35 }, (_, i) => ({ id: i + 1, payload: {
      event_id: `e${i}`, source: 'mattermost', target_agent: 'a', kind: 'post', observed_at: i,
      summary: `m${i}`, raw: { data: { post: JSON.stringify({ message: `full-${i}` }) } },
    } }));
    const payloads = await groupMessagePayloads(rows, dir, 'a', '/opt/data/home/group-messages');
    assert.equal(payloads.length, 33);
    assert.ok(payloads[0].raw.path.startsWith('/opt/data/home/group-messages/'));
    assert.ok(!JSON.stringify(payloads).includes(dir));
    const files = await readdir(dir);
    assert.equal(files.length, 1);
    const saved = (await readFile(join(dir, files[0]), 'utf8')).trim().split('\n').map(JSON.parse);
    assert.equal(saved.length, 35);
    assert.equal(saved[0].message, 'full-0');
  } finally { await rm(dir, { recursive: true, force: true }); }
});
test('pause drains an active turn without dispatching new queued turns', async () => {
  let queries = 0, finished = 0, release, entered;
  const started = new Promise(resolve => { entered = resolve; });
  const pending = new Promise(resolve => { release = resolve; });
  const row = { id: 1, payload: { event_id: 'e', source: 'mattermost', kind: 'dm' } };
  const dispatcher = new Dispatcher({ agent: 'a', groupMessageDirectory: '/unused',
    state: { nextPendingBatch: async () => { queries++; return [row]; },
      claimShown: async () => new Set(), markBatchSending: async () => 1,
      markBatchSent: async () => { finished++; } },
    channel: { prompt: async () => { entered(); await pending; return { stopReason: 'end_turn' }; } },
    board: { enabled: false }, onLog: () => {},
  });
  const current = dispatcher.tick(); await started; dispatcher.pause(); release(); await current;
  assert.equal(finished, 1);
  assert.deepEqual(await dispatcher.tick(), { dispatched: 0 });
  await dispatcher.dispatchSpecific([row]);
  assert.equal(queries, 1); assert.equal(finished, 1);
});
