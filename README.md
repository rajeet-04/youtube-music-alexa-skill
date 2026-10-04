# JUKES backend

Music backend for the JUKES Android app: anonymous search/audio, optional
personalised recommendations, shared audio caches with warmup, and a small
authenticated admin. (This repository previously hosted an Alexa skill; that
runtime has been removed. Git history keeps it.)

## What it does

- **Anonymous by default.** No registration or YouTube login for audio or radio.
- **Two shared audio pools.** *Requested*: 10 GB, LRU, no expiry. *Warmup*:
  1 GB, 2-hour TTL. One physical file per track; a request promotes a warmup
  (even an in-flight one) without copying bytes or downloading twice.
- **Whole files, ranges.** The VM downloads the complete file first, then serves
  it with `Content-Length` and byte ranges. `/audio/` keeps the legacy contract.
- **Optional personalisation.** The app can submit its YouTube Music session;
  it is validated, encrypted under a private installation token, and used only
  for that installation's recommendations. Disconnect deletes it.
- **Admin** (`/admin/`): download-cookie upload/paste, interactive YouTube
  sign-in in a private browser, pool/job status.

API reference for the app: [`docs/JUKES_API.md`](docs/JUKES_API.md).
Deployment: [`SETUP-DOCKER.md`](SETUP-DOCKER.md). Plan and decisions: [`PLAN.md`](PLAN.md).

## Layout

| Path | Purpose |
|---|---|
| `flask-server/jukes/` | cache, jobs, extractor, music, identity, credentials, admin, routes |
| `flask-server/server.py` | `waitress-serve server:app` entry point |
| `browser-auth/` | private Chromium + noVNC sidecar for interactive sign-in |
| `docker-compose.yml` | app + browser + Caddy; `docker-compose.vpn.yml` adds Gluetun |
| `vpn/` | **placeholders** for your Surfshark (India) profile |
| `scripts/` | secrets setup, VPN verify, bgutil network helper, benchmark |

## Quick start (development)

```bash
uv venv && uv pip install -r flask-server/requirements.txt pytest
uv run python -m pytest flask-server/tests browser-auth/tests -q
```

Python dependencies are managed with **uv**; JavaScript tooling (Cloudflare
Wrangler) with **Bun** (`bun install`, then `bun x wrangler login` when you are ready).

## Secrets

```bash
cp .env.example .env
uv run --no-project --with werkzeug --with cryptography scripts/setup-secrets.py
```

Keys must stay stable; back them up separately from the database. See
[`SETUP-DOCKER.md`](SETUP-DOCKER.md#what-you-still-need-to-provide) for the full checklist.
