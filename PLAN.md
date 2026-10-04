# JUKES Backend Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` for native execution
> task-by-task. Do not dispatch subagents without explicit authorisation.

**Goal:** Deliver the complete JUKES backend scope in one release: anonymous and
optional personalised music services, shared audio caches, warmup, compatibility
audio, and small authenticated administration without Alexa.

**Architecture:** Keep Flask/Waitress and the existing browser sidecar, but replace
the monolithic player backend with focused music, cache, job and credential
services. SQLite persists lifecycle state; a single process owns the download
coordinator and a bounded worker pool. All audio endpoints share that coordinator.

**Tooling:** Use uv for Python dependency management and Bun for JavaScript/CLI
packages; the user explicitly selected these over pip/npm. System utilities
(Docker/Compose, FFmpeg) remain OS packages.

**Tech Stack:** Python 3.12, Flask, Waitress, SQLite, ytmusicapi, yt-dlp, Deno,
FFmpeg, Chromium/noVNC, Docker Compose and Caddy. Add `cryptography` for authenticated
credential encryption. Keep existing extractor versions initially.

**Spec:** The approved design in this document. External deployment and app
sign-in are verification dependencies, not promises of working behaviour.

**Status:** Final planning draft, 2026-10-04. Feature scope is approved. No backend
code, running services, credentials, or JUKES app code have been modified.

**References:** Backend baseline `27bdee94916852bfaa8236a0b96099c28c06d443`;
JUKES beta `8315b553978244583b10da7bcd49b361685c72c8`
(https://github.com/rajeet-04/JUKES/tree/beta).

## Approved design

- Deliver everything together: anonymous access, optional personalisation, warmup,
  shared caches, audio APIs, small admin page, and removal of Alexa/Amazon.
- JUKES owns playlists, likes, listening history and queues. Preserve its existing
  recommendation fetch/filter/dedup/reseed flow. Retire the old web player and Echo
  state without deleting existing database records.
- Anonymous callers need no registration or YouTube login. Server-managed cookies
  may support extraction; never use the operator account to personalise default
  recommendations. Public traffic arrives through Cloudflare.
- Optional personalisation: app submits its session on connect/refresh; backend
  validates and encrypts it under a private installation token. Disconnect deletes
  it. Missing/expired sessions leave anonymous functionality available.
- One shared requested-track pool: **10 GB, LRU, no TTL**. One separate shared warmup
  pool: **1 GB, 2-hour TTL**. All users and request paths reuse public-track audio.
- Warmup accepts video ID or title/artist/optional duration, resolves metadata for
  app verification, and starts downloading immediately without app confirmation.
- Playback/download request promotes a warmup, including an in-flight job, without
  copying bytes or starting a second download. Active transfers are protected.
- Download the complete file on the VM before serving it. Deliver incrementally
  with Content-Length and byte ranges; do not progressively expose partial files.
- Retain `/audio/` compatibility alongside prepare/status APIs.
- Admin supports cookie upload/paste AND interactive YouTube login with persistent
  browser profile, private controls and short-lived browser leases.
- Preserve Gluetun VPN egress for extraction/browser login. Surfshark/India is
  user-reported; provider, protocol, live health and routing must be verified.
- Backend changes are in scope. Required app changes are documented for handoff;
  app implementation and live deployment are separate actions.

## Implementation defaults and lifecycle decisions

These are concrete, configurable engineering defaults, not new product requirements.

- Warmup TTL starts at completion; duplicates do not reset it. Evict expired
  staging files first, then oldest completed unrequested staging files.
- Warmup is a single track per request. The app debounces search and chooses the
  likely match; backend rate limits and dedup bound speculative work.
- A new download begins in a reserved staging area. Use reported size when known;
  otherwise start with a 16 MB reservation and extend in 16 MB increments before
  accepting more bytes. Serialize budget changes and stop extraction if an extension
  cannot be admitted. Never reserve the entire pool for every unknown-size job.
- Requested tracks larger than the 10 GB pool return `track_too_large`; speculative
  tracks larger than 1 GB fail warmup but can be requested directly. No arbitrary
  100 MB/30-minute exclusion. Protected files cause retryable capacity errors.
- Mark LRU use on accepted prepare/playback/download. HEAD, polling, info lookups
  and duplicate warmups do not refresh recency. Promotion counts bytes exactly once.
- Publish finished files atomically after extractor success and media validation;
  use same-filesystem rename and persist recoverable states. Use a process-wide
  coordinator lock for file/metadata transitions and reference-counted read leases.
- Preserve extractor selection/fallbacks and current compatible m4a policy; policy
  version belongs in cache keys. Do not publicly expose private uploads or other
  account-restricted audio using operator credentials.
- Persist active job identity across recovery. Requeue interrupted work once after
  cleaning its partial state; shutdown stops admission, waits a bounded grace
  period, then terminates child process groups and records recoverable states.
- Terminal jobs remain queryable for 24 hours, then are pruned. Cached audio and
  credentials have independent lifecycles; an old job becoming unknown is not
  evidence that its audio was deleted.
- Metadata caches: bounded 5,000 entries, 10-minute TTL; personalise by installation,
  credential generation and locale. Default radio locale/region preserves en-IN/IN.
- Start with four extraction workers, 100 queued jobs, 120-second total extraction
  deadline, configurable fallback budgets within that deadline. Requested work wins
  dispatch priority; do not kill running speculative work to free a worker.
- Start legacy wait at 25 seconds, configurable; validate actual Android playback
  timeouts before calling it compatible. Pending job continues after HTTP timeout.
- Start new-work rates at 30 warmups/minute and 60 requested jobs/minute per trusted
  client IP. Polling has a separate 120/minute budget; issuance 5/minute per IP.
  Duplicate hits still count against cheap HTTP admission, but not new-job quota.
- Automatic installation token issuance is needed only for personalisation. Store
  token digests; token loss creates a fresh installation and requires reconnection.
  Expire token-only installations after 30 idle days; inactive connected installations
  after 180 days, deleting their credentials. Document this explicitly.
- Configure persistent encryption/signing keys. Back up keys separately from encrypted
  DB/profile backups; missing/wrong keys never silently replace encrypted state.
  Admin-only key rotation is an offline maintenance procedure with backup, transactional
  re-encryption, verification and rollback, not an unauthenticated API.
- Cookie/header bundle: accept only YouTube auth fields and account index, never a
  caller-chosen upstream origin/path. Validate account with an uncached request;
  build time-sensitive auth headers server-side. Request bodies capped at 1 MB.
- Invalid installation token is 401. Expired YouTube session is anonymous fallback
  plus reconnect status. Transient upstream outages do not overwrite valid credentials.
- All recommendations and credential/job/admin responses are no-store. Audio edge
  caching is initially disabled to preserve origin LRU access accounting.

## Deployment evidence and verification boundary

- Both existing app/browser services use `network_mode: container:gluetun`.
  Compose does not define Gluetun; Caddy currently targets ytmusic:5000 despite
  that service lacking its own web-network attachment. Verify actual reachable
  targets rather than copying that assumption into the new deployment.
- Existing extraction needs external bgutil-provider:4416, the provider plugin,
  Deno, remote EJS components, FFmpeg and cookie-aware fallback clients. The
  scripts/ensure-bgutil-network.sh systemd wiring attaches the provider to web.
- Current workspace reports 2 ARM cores, 11 GiB RAM and ~44 GB free disk, but Docker
  is absent. These readings do not establish live container limits or VPN health.
- Keep metadata/browser/token-provider traffic reachable through VPN networking;
  check kill-switch behaviour so VPN loss cannot silently cause direct egress.
- Actual Cloudflare hostname, proxy/tunnel, trusted headers and API challenge rules
  require deployment inspection. Do not expose browser/control/CDP ports publicly.

## Global constraints

- Ship optional personalisation in the same release; anonymous use is the default.
- Requested capacity: 10,000,000,000 bytes; warmup: 1,000,000,000 bytes. Expose
  configurable byte limits and display decimal GB consistently.
- Warmup expires 7,200 seconds after completion, never extended by duplicate warmup.
- Public, shared audio only; reject private uploads/account-restricted extraction.
- Fixed audio policy initially preserves the current compatible m4a selection and
  fallbacks. Use a policy version in the internal cache key; one public video ID
  resolves to the configured current policy.
- Reserve 2,000,000,000 free disk bytes beyond cache/temporary reservations.
- Use `/data` for state and secrets, `/tmp/ytm_audio_cache` for audio. Do not delete
  the old database or its records. New tables use a `jukes_` prefix.
- Anonymous requests use an unauthenticated YTMusic client. Admin downloader
  cookies never turn anonymous recommendations into operator-personalised results.
- Personalised clients and metadata caches are scoped by installation and credential
  generation. Audio jobs are shared only for public audio.
- Admin session signing and credential-encryption keys are persistent deployment
  secrets. Fail closed for admin/personalisation when required secrets are absent;
  never overwrite encrypted records with a newly generated key after restart.

## API contract

Input to warmup/prepare is JSON with exactly one selector:
`{"video_id":"abcdefghijk"}` or
`{"title":"Song","artist":"Artist","duration_ms":210000}`.
Duration is optional, positive, and always milliseconds in versioned APIs.
Optional `Authorization: Bearer <installation token>` chooses user context;
absent token is anonymous, invalid supplied token is 401 rather than anonymous.

Return metadata with `video_id`, `title`, `artists` (array of names), `album`
(nullable), `duration_ms` (nullable), and `artwork_url` (nullable). Legacy `artist`
remains a joined string. Versioned response also contains `job_id`, `status`
(`queued`, `downloading`, `ready`, `failed`), `pool` (`warmup` or `requested`),
`audio_url` (only when ready) and `personalization_status`.

- `POST /v1/warmup`: resolve then enqueue immediately; 200 if ready, 202 otherwise.
- `POST /v1/audio/prepare`: resolve, request/promote; 200 if ready, 202 otherwise.
- `GET /v1/jobs/{job_id}`: public audio-job status, video ID and readiness only.
  Return resolved metadata on the original authenticated resolution response; never
  store personalised response metadata in the shared public job. IDs are opaque random values.
- `GET|HEAD /v1/audio/{video_id}`: ready-file delivery. GET promotes/requests a
  missing track but returns 503 pending instead of waiting; HEAD never starts a job.
- `/audio/`: retain `video_id`, accepted YouTube URL, `q`, seconds-valued `duration`,
  `info=1`, and `wait=1`. Info lookup remains side-effect-free. GET always serves
  complete files; HEAD only inspects completed files. Ignore an obsolete supplied
  `key` for public compatibility, without logging it.
- `POST /v1/installations`: issue a cryptographically random bearer token once;
  store its digest only. Registration is automatic, not a user account/login.
- `PUT /v1/me/youtube`: validate supplied browser authentication bundle, encrypt
  and replace for the bearer installation. Preserve valid prior data on failure.
- `GET /v1/me/youtube`: redacted connection status only.
- `DELETE /v1/me/youtube`: delete session, invalidate client/metadata caches.
- `DELETE /v1/me`: revoke token and delete its credential records.
- `POST /v1/recommendations`: accept `video_id` and bounded `limit` (1–100), return
  ordered radio tracks. Use selected user's context or anonymous; on expired
  credentials return anonymous results with `personalization_status=reconnect_required`.
  No queue/filter/reseed rewrite: document how the existing app engine can call it.

Errors use `{"error":{"code":"...","message":"...","retryable":false}}`.
Use 400 invalid input, 401 bad token/admin session, 404 no match/job/file, 413 input
too large, 422 unsupported/rejected match, 429 admission limit, 502 upstream error,
503 pending/unavailable capacity; retryable errors include Retry-After.

## Review focus

1. Restart during a download: recover jobs and reservations without treating partial
   bytes as a completed track (Tasks 1–2).
2. Simultaneous promotion and eviction: protect the same physical file and count it
   once in its new pool (Tasks 1–3).
3. Invalid user credentials or token: no other user's context and no credential
   disclosure in public job status or errors (Tasks 4–5).
4. Forged proxy headers: public clients cannot bypass IP limits or impersonate
   admin sessions; forwarded addresses trusted only from configured proxies (Task 6).
5. Cookie refresh during extraction: old jobs retain stable snapshots; new jobs use
   the validated new version without leaking session data (Task 5).

## File structure

Create `flask-server/jukes/` with `__init__.py`, `config.py`, `models.py`,
`store.py`, `cache.py`, `jobs.py`, `extractor.py`, `music.py`, `identity.py`,
`credentials.py`, `routes.py`, `admin.py`, and `app.py`.
`server.py` becomes the small compatibility entry point exporting `app`.
Retain/adapt `youtube_browser_session.py` and `browser-auth/service.py` for admin
browser login. Create `templates/admin.html` and `templates/admin_login.html`.
Create focused tests listed below; replace obsolete Alexa/player tests rather
than claiming those retired tests establish correctness of the new service.

## Task 1: Persistent two-pool cache

Files (relative to `flask-server/`): create `jukes/{config,models,store,cache}.py`;
test `tests/test_jukes_cache.py`.

Interfaces: `AudioKey(video_id: str, policy: str)`;
`Cache.lookup(key) -> CacheEntry | None`; `Cache.complete(key, path, requested)`;
`Cache.promote(key) -> CacheEntry | None`; `Cache.lease(key)` context manager;
`Cache.prune(now: float) -> PruneResult`. `Store` owns SQLite connections per
operation and transactions; lifecycle fields persist in prefixed tables.

- [ ] Write tests for requested tracks surviving >7,200 seconds, warmup expiry at
  completion+7,200, independent 10 GB/1 GB limits, duplicate warmup preserving expiry,
  promotion without file copy, LRU order, leases and promotion-vs-eviction race.
  Pin the core invariants with named tests:

  ```python
  def test_requested_track_has_no_ttl(cache, clock, completed_audio):
      key, path = completed_audio
      cache.complete(key, path, requested=True)
      clock.advance(7201)
      cache.prune(clock.now())
      assert cache.lookup(key) is not None

  def test_promotion_reuses_file_and_removes_expiry(cache, completed_audio):
      key, path = completed_audio
      cache.complete(key, path, requested=False)
      promoted = cache.promote(key)
      assert promoted.path == path
      assert promoted.pool == "requested"
      assert promoted.expires_at is None
  ```

  Implement injected clock/temporary-file fixtures alongside these tests; no
  live YouTube calls or wall-clock sleeping.
- [ ] Run `python -m pytest flask-server/tests/test_jukes_cache.py -q`; confirm
  failures establish missing behaviour, then implement the interfaces.
- [ ] Test restart reconciliation of missing files/orphan partials and preservation
  of old database tables. Use sparse fixtures/tiny injected limits rather than
  allocate 11 GB in tests.
- [ ] Test atomic publication recovery, oversized pool admission, partial-byte
  reservations and active-reader pinning under low free disk.
- [ ] Run the cache tests to passing; review transactional accounting and commit
  the self-contained cache implementation.

## Task 2: Shared download coordinator and extractor

Files (relative to `flask-server/`): create `jukes/{jobs,extractor}.py`;
test `tests/test_jukes_jobs.py` and `tests/test_jukes_extractor.py`.

Interfaces: `Jobs.submit(key: AudioKey, requested: bool) -> Job`;
`Jobs.get(job_id: str) -> Job | None`; `Jobs.wait(job_id, timeout: float) -> Job`;
`Extractor.download(key, destination, credential_snapshot) -> DownloadResult`.
Jobs consumes Task 1 Cache/Store. Credential provider is injectable until Task 5.

- [ ] Write deterministic fake-extractor tests: 20 simultaneous submissions create
  one job; warmup promotion wins over stale priority; four-worker bound; capacity
  admission; cancellation of an HTTP caller leaves shared work alive.
- [ ] Verify tests fail, implement coordinator, persistent states and reservations.
- [ ] Test interrupted process recovery, timeout, failed cleanup, oversized output,
  public-audio scope, and credential snapshot stability. Reuse current yt-dlp
  fallback command policy without Echo cancellation/state hooks.
- [ ] Test unique cookie snapshot paths, provider unavailable, bounded global cooldown
  and flaky/dead-video backoff, child-process-group shutdown and no secret-bearing
  stderr. Preserve default/android_vr/web/tv cookie-aware fallbacks and bgutil/EJS wiring.
- [ ] Run `python -m pytest flask-server/tests/test_jukes_jobs.py
  flask-server/tests/test_jukes_extractor.py -q` to passing and commit.

## Task 3: Metadata, warmup, prepare and completed audio APIs

Files (relative to `flask-server/`): create `jukes/{music,routes,app}.py`, reduce `server.py` to entry point;
test `tests/test_jukes_api.py`, adapt `tests/test_audio_endpoint.py`.

Interfaces: `Music.resolve(selector: TrackSelector, context: UserContext) -> Track`;
`create_app(config=None, services=None) -> Flask`. Routes consume Task 2 Jobs and
Task 1 cache leases. Context provider defaults anonymous until Task 4.

- [ ] Test both selectors, metadata shape/units, immediate post-resolution warmup,
  deduplication, duration/version rejection, bad inputs and null metadata.
- [ ] Verify failures, implement versioned routes and explicit error contract.
- [ ] Test GET/HEAD full length, open/suffix ranges, invalid-range 416, disconnect
  releasing leases, concurrent promotion/eviction and legacy seconds duration.
  Legacy info must not enqueue; legacy pending returns 503 after injected timeout.
- [ ] Implement completed-file serving with Flask conditional responses and a lease
  held until response close. Never buffer the complete file in RAM.
- [ ] Test If-Range/ETag/304, concurrent stream and segmented downloads, immutable
  publication, artist/title/variant matching and duration tolerance max(8s, 7%).
- [ ] Run `python -m pytest flask-server/tests/test_jukes_api.py
  flask-server/tests/test_audio_endpoint.py -q` plus cache/job tests to passing and commit.

## Task 4: Installation identity and personalised music context

Files (relative to `flask-server/`): create `jukes/{identity,credentials}.py`, extend `music.py`, `routes.py`,
`store.py`, `requirements.txt`; test `tests/test_jukes_personalization.py`.

Interfaces: `Identity.issue() -> str`; `Identity.authenticate(token) -> Installation`;
`Credentials.replace(installation_id, bundle) -> ConnectionStatus`;
`Credentials.delete(installation_id)`;
`Music.context(installation_id: str | None) -> UserContext`;
`Music.radio(video_id: str, limit: int, context: UserContext) -> RadioResult`.

- [ ] Test token digest-only storage, revoked/invalid token 401, encrypted-at-rest
  credentials, wrong/missing encryption key, isolated clients/cache generations,
  reconnect fallback, disconnect and no secret-bearing error/job responses.
- [ ] Verify failures, implement authenticated encryption with persistent configured
  key, validate upstream account context before replacement, bounded token issuance.
- [ ] Implement ordered radio response with anonymous default; preserve app engine's
  responsibility for filtering/queue reseeding. Concurrent users never mutate a
  shared authenticated session. Add public/personalised same-audio dedup test.
- [ ] Test account-index selection, superficially logged-out HTTP 200, wrong-key
  behaviour, installation expiry and header allowlisting. Document token-loss
  reconnection and key backup/rotation.
- [ ] Run `python -m pytest flask-server/tests/test_jukes_personalization.py -q`
  plus cache/job/API tests to passing and commit.

## Task 5: Admin authentication, cookie management and interactive browser

Files (relative to `flask-server/`): create `jukes/admin.py`, admin templates; adapt
`youtube_browser_session.py`, `browser-auth/service.py`, `jukes/credentials.py`;
test `tests/test_jukes_admin.py`, adapt `tests/test_youtube_browser_session.py`.

Interfaces: `ServerCookies.replace(netscape_text: str) -> CookieStatus`;
`ServerCookies.snapshot() -> CredentialSnapshot`; admin routes live under `/admin/`.
Browser export uses the same validated cookie replacement path.

- [ ] Write tests for admin login/logout/CSRF, bounded malformed uploads, invalid
  replacement retaining old cookies, persistence, redaction and protected leases.
- [ ] Verify failures, implement hashed password verification, signed HttpOnly
  sessions, private encrypted server-cookie storage and stable per-job materialised
  snapshots with restrictive permissions and cleanup.
- [ ] Adapt browser login to admin identity; add export of Netscape download cookies
  from the operator profile, validate before activation. Do not expose CDP/control
  ports. Test old-job/new-cookie concurrency and lease expiry/public rejection.
- [ ] Test admin session revocation and invalidation of legacy owner sessions,
  cookie probe bypassing caches, unique snapshot cleanup, exported download jar
  rather than only Music headers, and bounded saved-profile refresh/reconnect.
- [ ] Implement small status view: two pool sizes, job counts, cookie connection
  status and redacted errors. Run admin/browser/service tests to passing and commit.

## Task 6: Alexa removal and deployable anonymous service

Files: modify `requirements.txt`, Dockerfiles, `docker-compose.yml`, `Caddyfile`,
README and SETUP-DOCKER; remove obsolete Alexa runtime/assets/routes and skill
directories after reference audit. Test `flask-server/tests/test_jukes_deployment.py`.

- [ ] Test clean application import without alexapy, absent Echo/player routes,
  public audio with protected admin, trusted-proxy address handling and limits.
- [ ] Verify failures, remove Alexa imports/dependency/session volume and player UI.
  Preserve old DB and unrelated volumes; no destructive volume migration commands.
- [ ] Replace read-only host cookie mount with persistent private storage. Keep the
  existing outbound VPN choice configurable; do not silently change VM networking.
- [ ] Configure no-store for admin/browser/user/job APIs and disable audio edge
  caching initially. Preserve Gluetun and external bgutil-provider connectivity;
  define backend/browser proxy targets from verified network topology.
- [ ] Test trusted-proxy handling against forged forwarded headers, VPN/provider
  unavailability and restart recovery. Add cheap liveness/readiness endpoints that
  distinguish process health from extraction dependencies without upstream probing
  on every request. Separate serving capacity from extraction worker limits.
- [ ] Run deployment tests and `docker compose config --quiet` with test secrets;
  verify secrets aren't printed. Build/import smoke test when dependencies/network
  are available, report actual external blockers, then commit.

## Task 7: Full integration and JUKES handoff

Files: create `flask-server/tests/test_jukes_integration.py`, `docs/JUKES_API.md`,
`scripts/benchmark_jukes.py`; update PLAN progress and README.

- [ ] Test warmup→prepare→ready→range playback, concurrent anonymous/personalised
  public-track reuse, two-pool limits, restart and invalid credential fallback.
- [ ] Document exact app requests, polling backoff, metadata verification, installation
  tokens, connect/refresh/disconnect, retry and existing provider fallback.
- [ ] Run the retained/new backend suite, browser sidecar tests, whitespace checks
  and dependency/reference audit. Do not run obsolete Alexa feature assertions.
- [ ] Document current JUKES key gate (URL and key must both be nonblank), ignored
  compatibility key, 10-second lookup/60-second download timeout, ApiClient timeout
  matrix, ExoPlayer verification and m4a-under-mp3-suffix health checks. New app
  polling/warmup/session calls and credential log redaction are explicit handoff items.
- [ ] Benchmark cold download, warmup-hit startup, parallel clients and job saturation
  against the VM and through the configured Cloudflare hostname when available.
  Do not claim app playback or Google capture works without actual verification.
- [ ] Review final changes against every requirement, record external verification
  results/limitations and commit the finished integration. Deployment changes to
  a live service remain a separate explicit action.

## External verification dependencies

- Current Cloudflare hostname/proxy or tunnel configuration and trusted-proxy path.
- Gluetun deployment, Surfshark provider/protocol and actual exit country/health;
  current runtime lacks Docker CLI. Do not read or print VPN credential contents.
- Actual JUKES Android build/device for end-to-end playback and optional session
  capture. Backend support can be implemented here; modifying the app is separate.
- Operator interactive Google sign-in/challenges and valid credentials for live
  download/personalisation probes. Never substitute another user's credentials.

## Self-review and execution handoff

- Scope coverage: cache Tasks 1–2; APIs/warmup Task 3; optional personalisation
  Task 4; admin/browser Task 5; Alexa removal/VPN/Cloudflare Task 6; app contract
  and integration Task 7. All are one release.
- Race, restart, proxy-header and credential-isolation review cases map to tests
  in their owning task. Public job status intentionally excludes user metadata.
- Durations are milliseconds in v1 and seconds in legacy query parameters;
  capacities use decimal bytes. Temporary files count toward disk admission.
- Use deterministic fakes for network/extraction tests; live tests report actual
  credentials/VPN/Cloudflare/device prerequisites separately.
- No claims of automatic Google login capture or live deployment success without
  verification. Native task execution is recommended; execution method remains
  for the user to select. No subagents were used in planning.
- Before implementation, review this final document and choose native execution
  or explicitly authorised subagent execution. Production deployment is separate.
