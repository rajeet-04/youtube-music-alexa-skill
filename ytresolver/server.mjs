// Loopback-only HTTP front for the warm resolver. It shares the VPN container's network
// namespace with ytmusic and bgutil, so 127.0.0.1 is reachable only from those three.
import http from 'node:http';
import vm from 'node:vm';
import { Innertube, Platform, Log } from 'youtubei.js';
import { createResolver, bgutilMinter, relay, HTTP_STATUS, ResolveError } from './resolver.mjs';

Log.setLevel(); // never log signed URLs or tokens
Platform.shim.eval = async data => vm.runInNewContext(
  '(function(){' + data.output + '})()', {}, { timeout: 2000 });

const HOST = process.env.HOST || '127.0.0.1';
const PORT = Number(process.env.PORT || 4417);
const MAX_ACTIVE = Number(process.env.YTRESOLVER_MAX_ACTIVE || 6);
const WARM_ID = 'dQw4w9WgXcQ';

const resolver = createResolver({
  createClient: () => Innertube.create({
    fetch: (url, init) => fetch(url, { ...init, signal: init?.signal ?? AbortSignal.timeout(10_000) }) }),
  mintPot: bgutilMinter(process.env.BGUTIL_URL || 'http://127.0.0.1:4416'),
});

const started = Date.now();
const recent = [];
const totals = { requests: 0, ok: 0, failed: 0, refreshed: 0, busy: 0, codes: {} };
let active = 0, ready = false;

function remember(entry) {
  recent.push(entry);
  if (recent.length > 2000) recent.shift();
  console.log(JSON.stringify(entry));
}

function percentiles(values) {
  const sorted = values.filter(v => v != null).sort((a, b) => a - b);
  const at = p => sorted.length ? sorted[Math.max(0, Math.ceil(sorted.length * p) - 1)] : null;
  return { n: sorted.length, p50: at(0.5), p95: at(0.95), max: sorted.at(-1) ?? null };
}

function stats() {
  const ok = recent.filter(r => r.ok);
  return { uptime_s: Math.round((Date.now() - started) / 1000), ready, active, totals,
    recent: { window: recent.length,
      resolve_ms: percentiles(ok.map(r => r.resolve_ms)),
      first_byte_ms: percentiles(ok.map(r => r.first_byte_ms)),
      total_ms: percentiles(ok.map(r => r.total_ms)) } };
}

function json(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}

async function audio(req, res, video) {
  totals.requests++;
  if (active >= MAX_ACTIVE) { totals.busy++; return json(res, 503, { error: 'busy' }); }
  active++;
  const t0 = performance.now();
  const entry = { at: new Date().toISOString(), video, ok: false };
  try {
    // Resolve separately first so the log separates /player time from transfer time.
    await resolver.resolve(video);
    entry.resolve_ms = Math.round(performance.now() - t0);
    const result = await relay(req, res, video, { resolver,
      onFirstByte: () => { entry.first_byte_ms = Math.round(performance.now() - t0); } });
    Object.assign(entry, { ok: true, bytes: result.bytes, refreshed: result.refreshed });
    totals.ok++;
    if (result.refreshed) totals.refreshed++;
  } catch (error) {
    const code = error instanceof ResolveError ? error.code : 'relay_failed';
    entry.code = code;
    if (code === 'relay_failed') // scrub anything URL-shaped: signed URLs carry the token
      entry.detail = `${error.name}: ${error.message}`.replace(/https?:\/\/\S+/g, '<url>').slice(0, 200);
    totals.failed++;
    totals.codes[code] = (totals.codes[code] || 0) + 1;
    if (!res.headersSent) json(res, HTTP_STATUS[code] || 502, { error: code });
    else res.destroy(); // a short body tells the caller the file is incomplete
  } finally {
    active--;
    entry.total_ms = Math.round(performance.now() - t0);
    remember(entry);
  }
}

const server = http.createServer((req, res) => {
  const { pathname } = new URL(req.url, 'http://local');
  const match = /^\/audio\/([^/]+)$/.exec(pathname);
  if (req.method === 'GET' && match) return void audio(req, res, match[1]);
  if (req.method === 'GET' && pathname === '/health') return json(res, ready ? 200 : 503, { ready });
  if (req.method === 'GET' && pathname === '/stats') return json(res, 200, stats());
  json(res, 404, { error: 'not_found' });
});
server.requestTimeout = 0; // a full song transfer is bounded by per-range timeouts instead
server.listen(PORT, HOST, () => console.log(JSON.stringify({ at: new Date().toISOString(), listening: `${HOST}:${PORT}` })));

// Build the session and player now, not on the first listener's request; retry until it works.
(async function warm() {
  for (let delay = 2000; !ready; delay = Math.min(delay * 2, 60_000)) {
    try { await resolver.warm(); ready = true; }
    catch { await new Promise(resolve => setTimeout(resolve, delay)); }
  }
  // Also exercises bgutil and the player once; a single unavailable video must not block readiness.
  await resolver.resolve(WARM_ID).catch(() => {});
  console.log(JSON.stringify({ at: new Date().toISOString(), ready, warm_ms: Date.now() - started }));
})();
// The session rotates every six hours; rebuild it in the background so no listener waits.
setInterval(() => resolver.warm().catch(() => {}), 10 * 60_000).unref();

for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close(() => process.exit(0)));
