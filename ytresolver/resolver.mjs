// Warm YouTube Music audio resolver and ordered ranged relay.
//
// One youtubei.js session stays initialized, so a cold song costs one /player call
// (~250 ms) instead of a yt-dlp start, player fetch and challenge solve (~3 s).
// Anonymous googlevideo URLs stop after ~1 MiB unless they carry a PO token bound to
// the video id, so each resolve mints one from bgutil in parallel. Requests without a
// Range header are throttled to ~32 KiB/s, so the file is always fetched as ranges.

export const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
export const ITAG = 140; // AAC/MP4: the format the cache and progressive copy path expect

export class ResolveError extends Error {
  constructor(code, upstreamStatus) {
    super(code);
    this.code = code;
    this.upstreamStatus = upstreamStatus;
  }
}

export const HTTP_STATUS = {
  invalid_video: 400, authentication_required: 403, track_unavailable: 404,
  unsupported_format: 422, busy: 503,
};

export function createResolver({ createClient, mintPot, now = Date.now,
  sessionMaxAgeMs = 6 * 3600_000, capacity = 256 } = {}) {
  let session, sessionAt = 0, cache = new Map(), pending = new Map(), lastPlayerReset = -Infinity;

  function reset() { session = undefined; cache = new Map(); pending = new Map(); }

  function client() {
    if (!session || now() - sessionAt > sessionMaxAgeMs) {
      // Cached URLs survive a rotation: neither the signature nor the video-bound token
      // depends on the session that produced them.
      sessionAt = now();
      const current = session = Promise.resolve().then(createClient);
      // A failed start must be retryable: never keep a rejected session.
      current.catch(() => { if (session === current) session = undefined; });
    }
    return session;
  }

  async function lookup(video, refresh) {
    const yt = await client();
    const [info, pot] = await Promise.all([
      yt.getBasicInfo(video, { client: 'YTMUSIC' }), mintPot(video, refresh)]);
    const status = info.playability_status?.status;
    if (status === 'LOGIN_REQUIRED') throw new ResolveError('authentication_required');
    if (status === 'UNPLAYABLE' || status === 'ERROR') throw new ResolveError('track_unavailable');
    let format;
    try { format = info.chooseFormat({ itag: ITAG, type: 'audio', format: 'mp4' }); }
    catch { throw new ResolveError('unsupported_format'); }
    const length = Number(format.content_length);
    if (format.itag !== ITAG || format.has_video || !format.has_audio ||
        format.drm_families?.length || !(length > 0))
      throw new ResolveError('unsupported_format');
    const url = new URL(await format.decipher(yt.session.player));
    if (url.protocol !== 'https:' || !url.hostname.endsWith('.googlevideo.com') ||
        (url.port && url.port !== '443') || url.username || url.password)
      throw new ResolveError('invalid_media');
    url.searchParams.set('pot', pot);
    const expiry = Number(url.searchParams.get('expire')) * 1000;
    const expiresAt = Math.min(now() + 300_000, expiry ? expiry - 60_000 : now() + 60_000);
    if (expiresAt <= now()) throw new ResolveError('expired');
    const seconds = Number(info.basic_info?.duration);
    return { url: url.toString(), length, expiresAt,
      duration: Number.isFinite(seconds) && seconds > 0 ? seconds : null };
  }

  async function resolve(video, { refresh = false } = {}) {
    if (!VIDEO_ID.test(video || '')) throw new ResolveError('invalid_video');
    if (refresh) cache.delete(video);
    const hit = cache.get(video);
    if (hit && hit.expiresAt > now()) return hit;
    if (!refresh && pending.has(video)) return pending.get(video);
    const currentCache = cache, currentPending = pending;
    const task = lookup(video, refresh);
    currentPending.set(video, task);
    try {
      const media = await task;
      // A session rotation while this awaited must not publish into the new cache.
      currentCache.set(video, media);
      while (currentCache.size > capacity) currentCache.delete(currentCache.keys().next().value);
      return media;
    } catch (error) {
      if (error instanceof ResolveError) throw error;
      const status = error.info?.status;
      if (status === 'ERROR' || status === 'UNPLAYABLE') throw new ResolveError('track_unavailable');
      if (status === 'LOGIN_REQUIRED') throw new ResolveError('authentication_required');
      // YouTube can ship a new player while this process lives. Rebuild the session on
      // the next request, but not repeatedly for network errors or dead videos.
      if (/decipher|signature|nsig|player/i.test(error.message || '') && now() - lastPlayerReset > 30_000) {
        lastPlayerReset = now();
        if (currentCache === cache) reset();
      }
      throw new ResolveError('resolve_failed');
    } finally {
      if (currentPending.get(video) === task) currentPending.delete(video);
    }
  }

  return { resolve, warm: () => client(), reset };
}

export function bgutilMinter(baseUrl, fetchImpl = fetch) {
  return async (video, bypass) => {
    let body;
    try {
      const response = await fetchImpl(new URL('/get_pot', baseUrl), {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ content_binding: video, bypass_cache: Boolean(bypass) }),
        signal: AbortSignal.timeout(5000) });
      body = response.ok ? await response.json() : null;
    } catch { body = null; }
    if (!body?.poToken) throw new ResolveError('pot_unavailable');
    return body.poToken;
  };
}

// Writes the whole file to `res` in order while up to `parallel` ranges are in flight.
// Before any byte is sent an upstream 403/410 re-resolves once; after that, a failure
// destroys the response so the caller sees a short body instead of spliced audio.
export async function relay(req, res, video, { resolver, fetchImpl = fetch, chunkBytes = 1 << 20,
  parallel = 4, chunkTimeoutMs = 15_000, onFirstByte = () => {} }) {
  const abort = new AbortController();
  let clientClosed = false;
  const stop = () => { clientClosed = !res.writableFinished; abort.abort(); };
  res.on('close', stop);
  const opened = [];
  try {
    let media = await resolver.resolve(video);
    let refreshed = false;
    let first;
    for (;;) {
      try {
        first = await openRange(media, 0);
        break;
      } catch (error) {
        if (refreshed || ![403, 410].includes(error.upstreamStatus)) throw error;
        refreshed = true;
        media = await resolver.resolve(video, { refresh: true });
      }
    }
    const count = Math.ceil(media.length / chunkBytes);
    opened[0] = Promise.resolve(first);
    for (let i = 1; i < Math.min(parallel, count); i++) opened[i] = guard(openRange(media, i));
    res.writeHead(200, { 'Content-Type': 'audio/mp4', 'Content-Length': String(media.length),
      'Cache-Control': 'no-store', 'X-Itag': String(ITAG),
      ...(media.duration ? { 'X-Duration-Seconds': String(media.duration) } : {}) });
    let sent = 0;
    for (let i = 0; i < count; i++) {
      const response = await opened[i];
      if (i + parallel < count) opened[i + parallel] = guard(openRange(media, i + parallel));
      let got = 0;
      for await (const block of response.body) {
        if (sent === 0) onFirstByte();
        got += block.length;
        sent += block.length;
        if (!res.write(block)) await new Promise(resolve => res.once('drain', resolve));
      }
      if (got !== rangeEnd(media, i) - i * chunkBytes + 1) throw new ResolveError('short_range');
    }
    res.end();
    return { bytes: sent, refreshed };
  } catch (error) {
    abort.abort();
    throw clientClosed ? new ResolveError('client_closed') : error;
  } finally {
    res.off('close', stop);
  }

  function rangeEnd(media, i) { return Math.min(media.length, (i + 1) * chunkBytes) - 1; }

  async function openRange(media, i) {
    const start = i * chunkBytes, end = rangeEnd(media, i);
    const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(chunkTimeoutMs)]);
    let response;
    try {
      response = await fetchImpl(media.url, { headers: { Range: `bytes=${start}-${end}` }, signal });
    } catch { throw new ResolveError(abort.signal.aborted ? 'client_closed' : 'upstream_unavailable'); }
    if (response.status !== 206 || response.headers.get('content-range') !== `bytes ${start}-${end}/${media.length}`) {
      response.body?.cancel().catch(() => {});
      const code = response.status === 429 ? 'upstream_rate_limited' : 'upstream_error';
      throw new ResolveError(code, response.status);
    }
    return response;
  }

  // A range that fails after an earlier one already failed must not crash the process.
  function guard(promise) { promise.catch(() => {}); return promise; }
}
