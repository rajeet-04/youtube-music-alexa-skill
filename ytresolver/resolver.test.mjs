import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createResolver, relay, ResolveError } from './resolver.mjs';

const VIDEO = 'dQw4w9WgXcQ';
const URL_BASE = 'https://rr1---sn-test.googlevideo.com/videoplayback?expire=';

function fakeClient({ status = 'OK', length = 10, calls, url = () => URL_BASE + (Math.floor(Date.now() / 1000) + 3600) }) {
  return async () => {
    calls.sessions++;
    return {
      session: { player: {} },
      async getBasicInfo(video, options) {
        calls.info.push([video, options.client]);
        return {
          playability_status: { status }, basic_info: { duration: 212 },
          chooseFormat: () => ({ itag: 140, has_audio: true, has_video: false, content_length: String(length),
            decipher: async () => url() }),
        };
      },
    };
  };
}

function calls() { return { sessions: 0, info: [], pots: [] }; }

function resolverWith(options = {}) {
  const c = calls();
  const resolver = createResolver({ createClient: fakeClient({ calls: c, ...options }),
    mintPot: async (video, bypass) => { c.pots.push([video, bypass]); return 'POT-' + video; },
    ...options.resolver });
  return { resolver, c };
}

test('resolves with the YTMUSIC client, a video-bound token and coalesces concurrent calls', async () => {
  const { resolver, c } = resolverWith();
  const [a, b] = await Promise.all([resolver.resolve(VIDEO), resolver.resolve(VIDEO)]);
  assert.equal(a, b);
  assert.deepEqual(c.info, [[VIDEO, 'YTMUSIC']]);
  assert.deepEqual(c.pots, [[VIDEO, false]]);
  assert.equal(new URL(a.url).searchParams.get('pot'), 'POT-' + VIDEO);
  assert.equal(a.length, 10);
  assert.equal(a.duration, 212);
  await resolver.resolve(VIDEO);
  assert.equal(c.info.length, 1, 'cached');
  await resolver.resolve(VIDEO, { refresh: true });
  assert.deepEqual(c.pots.at(-1), [VIDEO, true]);
  assert.equal(c.sessions, 1);
});

test('rejects bad ids, unplayable tracks and non-googlevideo urls with stable codes', async () => {
  await assert.rejects(resolverWith().resolver.resolve('../etc/pass'), { code: 'invalid_video' });
  await assert.rejects(resolverWith({ status: 'UNPLAYABLE' }).resolver.resolve(VIDEO), { code: 'track_unavailable' });
  await assert.rejects(resolverWith({ status: 'LOGIN_REQUIRED' }).resolver.resolve(VIDEO), { code: 'authentication_required' });
  await assert.rejects(resolverWith({ url: () => 'https://evil.example.com/x?expire=9999999999' }).resolver.resolve(VIDEO),
    { code: 'invalid_media' });
});

test('a failed session start is retried instead of cached', async () => {
  let attempts = 0;
  const c = calls();
  const good = fakeClient({ calls: c });
  const resolver = createResolver({ createClient: () => (++attempts === 1 ? Promise.reject(new Error('boom')) : good()),
    mintPot: async () => 'P' });
  await assert.rejects(resolver.resolve(VIDEO), { code: 'resolve_failed' });
  assert.equal((await resolver.resolve(VIDEO)).length, 10);
});

test('the session is rebuilt after its maximum age', async () => {
  let clock = 0;
  const c = calls();
  const resolver = createResolver({ createClient: fakeClient({ calls: c }), mintPot: async () => 'P',
    now: () => clock, sessionMaxAgeMs: 1000 });
  await resolver.warm();
  clock = 5000;
  await resolver.warm();
  assert.equal(c.sessions, 2);
});

// --- relay -------------------------------------------------------------------------------

function fakeUpstream(bytes, { failFirst = [], failAt = new Set() } = {}) {
  const seen = [];
  const fetchImpl = async (url, init) => {
    const [, a, b] = /bytes=(\d+)-(\d+)/.exec(init.headers.Range);
    seen.push([url, init.headers.Range]);
    const status = failFirst.length ? failFirst.shift() : failAt.has(Number(a)) ? 403 : 206;
    const slice = bytes.subarray(Number(a), Number(b) + 1);
    return new Response(status === 206 ? slice : null, { status,
      headers: { 'content-range': `bytes ${a}-${b}/${bytes.length}` } });
  };
  return { fetchImpl, seen };
}

async function serve(handler) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { base, close: () => new Promise(resolve => server.close(resolve)) };
}

function mediaResolver(length) {
  let n = 0;
  return { calls: () => n, resolve: async (video, { refresh } = {}) => {
    n++; return { url: 'https://x.googlevideo.com/v?n=' + n + (refresh ? '&r=1' : ''), length, duration: 3 }; } };
}

test('relay writes every range in order with bounded parallelism', async () => {
  const bytes = Buffer.from(Array.from({ length: 23 }, (_, i) => i));
  const up = fakeUpstream(bytes);
  const resolver = mediaResolver(bytes.length);
  let result;
  const s = await serve(async (req, res) => {
    result = await relay(req, res, VIDEO, { resolver, fetchImpl: up.fetchImpl, chunkBytes: 5, parallel: 2 });
  });
  const response = await fetch(s.base);
  const body = Buffer.from(await response.arrayBuffer());
  await s.close();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('content-length'), '23');
  assert.equal(response.headers.get('x-duration-seconds'), '3');
  assert.deepEqual(body, bytes);
  assert.deepEqual(up.seen.map(r => r[1]),
    ['bytes=0-4', 'bytes=5-9', 'bytes=10-14', 'bytes=15-19', 'bytes=20-22']);
  assert.deepEqual(result, { bytes: 23, refreshed: false });
});

test('relay re-resolves once on an upstream 403 before sending audio', async () => {
  const bytes = Buffer.alloc(8, 7);
  const up = fakeUpstream(bytes, { failFirst: [403] });
  const resolver = mediaResolver(bytes.length);
  const s = await serve(async (req, res) => {
    const r = await relay(req, res, VIDEO, { resolver, fetchImpl: up.fetchImpl, chunkBytes: 4 });
    assert.equal(r.refreshed, true);
  });
  const body = Buffer.from(await (await fetch(s.base)).arrayBuffer());
  await s.close();
  assert.deepEqual(body, bytes);
  assert.ok(up.seen[1][0].includes('&r=1'));
});

test('relay surfaces a second 403 before headers and aborts mid-file failures', async () => {
  const resolver = mediaResolver(8);
  await assert.rejects(relay({}, fakeRes(), VIDEO, { resolver,
    fetchImpl: fakeUpstream(Buffer.alloc(8), { failFirst: [403, 403] }).fetchImpl, chunkBytes: 4 }),
    (e) => e instanceof ResolveError && e.upstreamStatus === 403);

  const res = fakeRes();
  await assert.rejects(relay({}, res, VIDEO, { resolver: mediaResolver(8),
    fetchImpl: fakeUpstream(Buffer.alloc(8), { failAt: new Set([4]) }).fetchImpl, chunkBytes: 4 }),
    { code: 'upstream_error' });
  assert.equal(res.headersSent, true, 'headers went out before the mid-file failure');
  assert.equal(res.ended, false, 'a failed relay never ends the body normally');
});

function fakeRes() {
  const listeners = {};
  return { headersSent: false, ended: false,
    on(e, f) { listeners[e] = f; }, off() {}, once() {},
    writeHead() { this.headersSent = true; }, write() { return true; }, end() { this.ended = true; } };
}
