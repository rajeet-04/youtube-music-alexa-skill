# Deploy the JUKES backend with Docker

This guide deploys the anonymous music API, optional personalisation, the small
owner admin flow, and its private YouTube browser. Alexa is no longer part of
this backend.

## Prerequisites

- Docker Engine and the Compose plugin on the host.
- The existing Gluetun VPN container and its kill-switch, managed outside this
  Compose project. Keep the backend and browser on that VPN network namespace;
  do not use a direct-egress fallback when the VPN is down.
- The existing Docker network named `web`, also managed outside this project.
  Gluetun must be attached to it with a stable DNS alias (the default is
  `gluetun`). The external `bgutil-provider` container must also be attached to
  `web` and resolve as `bgutil-provider` from Gluetun's network namespace.
- The same Compose project name and existing named volumes used by the current
  deployment. Volume names are project-scoped. Confirm the project name before
  applying an update so the app continues using the existing data, audio-cache,
  and Chromium-profile volumes.

The repository does not establish live VPN health, kill-switch behavior,
network attachment, DNS, Cloudflare ingress, or the deployed project/volume
names. Verify those on the host before starting the new containers. Keep the
current Cloudflare ingress path and disable edge caching for audio and API
responses during rollout.

Forwarded client IPs are untrusted by default. The example leaves
`CADDY_TRUSTED_PROXY_CONFIG` and `JUKES_TRUSTED_PROXY_CIDRS` blank, so Caddy
uses the direct peer address and the app rate-limits by its direct Caddy peer.
This is safe when all public requests arrive through Caddy, though it groups
callers behind that peer. After verifying Cloudflare's actual source ranges,
origin protection, and the exact Caddy-side Docker subnet, set the Caddy global
options block to the verified Cloudflare ranges and set
`JUKES_TRUSTED_PROXY_CIDRS` only to the verified Caddy source/subnet seen by
Flask. For example, replace both placeholders with observed values; the sample
contains no usable trust ranges:

```dotenv
CADDY_TRUSTED_PROXY_CONFIG='{
  servers {
    trusted_proxies static <verified-cloudflare-cidr-1> <verified-cloudflare-cidr-2>
    trusted_proxies_strict
  }
}'
JUKES_TRUSTED_PROXY_CIDRS=<verified-caddy-source-cidr>
```

Caddy overwrites upstream `X-Forwarded-For` and `X-Real-IP` with its computed
client address and removes `CF-Connecting-IP` and `True-Client-IP`. Only enable
Cloudflare trust after confirming that origin traffic cannot bypass the
verified edge policy and that the edge replaces untrusted forwarded values.

## Configure the private environment

Copy the tracked template and edit the private file:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set `SITE_ADDRESS` and `PUBLIC_BASE_URL` to the hostname users reach over HTTPS.
Set `VPN_CONTAINER_NAME` to the existing VPN container name and `GLUETUN_ALIAS`
to the alias Gluetun actually has on the `web` network. Keep
`YT_BROWSER_SERVICE_URL=http://127.0.0.1:8765`; the Flask app and browser share
the VPN container's network namespace.

Generate the browser control token once and put the same value in the private
`.env` used by both services:

```bash
openssl rand -hex 32
```

Generate the admin session key and the Fernet credential-encryption key once;
back them up securely and keep them stable across restarts:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

The second value is a Fernet-compatible key for
`JUKES_CREDENTIAL_ENCRYPTION_KEY`. Create the admin password hash with the
application's Werkzeug dependency after building the app image. The command
prompts for the password and prints only its hash:

```bash
docker compose build ytmusic
docker compose run --rm --no-deps --entrypoint python ytmusic -c \
  'from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass("Admin password: ")))'
```

Store that output as `JUKES_ADMIN_PASSWORD_HASH`. Do not use the legacy
`API_KEY`, `SECRET_KEY`, `REMOTE_PASSWORD`, or other old login value as the new
admin password or signing/encryption key. Admin and personalisation features
fail closed when their keys are absent or invalid; anonymous music access is
independent of those credentials.

Keep `YTDLP_BGUTIL_BASE_URL=http://bgutil-provider:4416` only if provider DNS
and connectivity have been verified from the shared Gluetun namespace. Keep
`YTDLP_JS_RUNTIME=deno`; Deno is still the yt-dlp runtime. `Bun` is not used to
replace it.

## Import existing YouTube cookies safely

The new admin flow accepts a cookie upload/paste or interactive YouTube sign-in
and stores accepted credentials encrypted in the JUKES database on `ytmusic_data`.
It does not automatically read `cookies.txt`, `/data/headers_auth.json`, or
`YTMUSIC_AUTH_FILE`.

If a legacy host `cookies.txt` is still needed during migration, the Compose
file includes a commented read-only bind-mount example for temporary use. Enable
it only after choosing the private source path, point the extractor's supported
cookie-file setting at `/run/secrets/legacy-ytdlp-cookies.txt`, and remove the
mount after the admin import and an extraction check succeed. Keep the source
file outside version control, do not put cookie contents in `.env` or logs, and
do not copy it blindly into `/data`. Leave `/data/headers_auth.json` untouched;
it is not imported or used for anonymous recommendations.

The persistent encryption key is required to recover saved credentials. Back it
up separately from the encrypted database and browser profile. Never generate a
replacement key after credentials have been stored unless performing the
approved offline key-rotation procedure.

## Preserve existing state and network topology

Both `ytmusic` and `youtube-browser` use
`network_mode: container:${VPN_CONTAINER_NAME:-gluetun}`. The browser controller
is reached by the app at `127.0.0.1:8765`; Caddy reaches Flask and noVNC through
the verified `GLUETUN_ALIAS` on the external `web` network. Caddy publishes only
ports 80 and 443. Compose does not publish the browser UI, controller, CDP, or
VNC ports.

The app keeps the existing `ytmusic_data:/data` and
`ytmusic_cache:/tmp/ytm_audio_cache` mounts. Its new database defaults to
`/data/jukes.sqlite3`, while the previous `/data/data.db` and
`/data/headers_auth.json` remain untouched on the same volume. The existing
`ytmusic_chromium_profile:/profile` volume remains attached to the browser. The
Alexa cookie volume is no longer mounted or managed by this Compose file; leave
its existing Docker volume and contents alone. Do not use `docker compose down
-v`, Docker volume pruning, or manual volume removal for this migration.

The `web` network is declared external, so it must already exist. Gluetun needs
the configured alias on that network, and bgutil-provider must join the same
network. The helper can retry that provider attachment after Docker starts:

```bash
sudo cp scripts/ensure-bgutil-network.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ensure-bgutil-network.service
sudo systemctl status ensure-bgutil-network.service
```

The checked-in unit expects this repository at
`/home/ubuntu/youtube-music-alexa-skill`; edit its `ExecStart` path if your
checkout is elsewhere. The script changes no VPN/network policy and only
attaches the external provider to the existing `web` network.

## Start and verify

Before first start, back up the existing data volume and browser profile. Then
render the Compose model without printing the expanded environment:

```bash
docker compose config --quiet
```

Start the services only after verifying Gluetun is healthy, its kill-switch is
active, the alias resolves from Caddy, and the `web` network/provider are ready:

```bash
docker compose up -d --build
```

Check local process health and storage/dependency readiness through the public
HTTPS endpoint:

```bash
curl -fsS https://<your-hostname>/health/live
curl -fsS https://<your-hostname>/health/ready
```

Liveness is local process health. Readiness reports local startup and dependency
state; it does not probe YouTube or bgutil on every request. Public audio is
anonymous. Owner-only browser authorization is checked at
`/admin/youtube/browser/authorize` before Caddy proxies `/youtube-login/` to
noVNC. Confirm the deployed Cloudflare rules do not cache audio, admin, browser,
credential, or job responses.

## Existing `.env` compatibility

Compose still reads the private `.env`, so the existing `SITE_ADDRESS`,
`PUBLIC_BASE_URL`, `YT_BROWSER_*`, `YTDLP_BGUTIL_BASE_URL`, and
`YTDLP_JS_RUNTIME` settings can be retained when their values remain correct.
The new `JUKES_*` cache and credential variables are separate settings.

Legacy `DB_FILE=/data/data.db`, audio-cache MB/TTL settings, `API_KEY`,
`YTMUSIC_AUTH_FILE`, `SECRET_KEY`, remote-login values, and Alexa settings do
not configure the new backend. The default new path `/data/jukes.sqlite3` keeps
the old database file in place. Remove stale Alexa and remote-login values from
`.env` after cutover; never map old keys into the JUKES admin or encryption
settings. Keep the Compose project name stable so its existing named volumes
continue to resolve to the same Docker volumes.

## VPN profile (Surfshark / India) placeholders

Use your existing Gluetun, or let this repository run one:

```bash
docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d --build
```

Put your own profile in `vpn/` (never committed; see `vpn/README.md`):
`vpn/wireguard/wg0.conf` (default) or `vpn/openvpn/surfshark.ovpn` plus
`OPENVPN_USER`/`OPENVPN_PASSWORD`. Set `DOCKER_WEB_SUBNET` to the real subnet of
the `web` network (`docker network inspect web`) so the app can reach
`bgutil-provider`. Then check the exit country is India (`IN`):

```bash
scripts/verify-vpn.sh
```

The Surfshark/India routing is user-reported and **unverified until you run this**.
If `bgutil-provider` does not resolve by name from inside Gluetun's namespace,
set `YTDLP_BGUTIL_BASE_URL` to its IP on the `web` network.

## Ports

| Port | Where | Exposure |
|---|---|---|
| 80, 443 | Caddy | public (Cloudflare origin) |
| 5000 | Flask (inside Gluetun namespace) | Caddy only; optional `127.0.0.1:5000` smoke-test bind via `JUKES_DIRECT_BIND` |
| 6080 | noVNC | Caddy only, behind admin session + lease |
| 8765, 9222, 5900 | browser controller / CDP / VNC | never published |

## Cloudflare Wrangler

Wrangler is installed with Bun (`bun install`; pinned in `bun.lock`). It is not
needed to run the backend. Log in when you are ready:

```bash
bun x wrangler login
```

Cloudflare-side items to configure (see `cloudflare/README.md`): proxied DNS to
this host, a cache-bypass rule for `/audio/*`, `/v1/*` and `/admin/*`, and
(optionally) origin protection so the trusted-proxy settings above are valid.

## What you still need to provide

1. `.env` secrets: run `scripts/setup-secrets.py` (admin password, session key,
   credential key, browser control token). Back the two keys up separately.
2. Your Surfshark India VPN profile in `vpn/` (WireGuard or OpenVPN).
3. `SITE_ADDRESS` / `PUBLIC_BASE_URL` for your real hostname and the Cloudflare
   DNS/proxy (or tunnel) pointing at this host.
4. After verifying Cloudflare ranges: `CADDY_TRUSTED_PROXY_CONFIG` and
   `JUKES_TRUSTED_PROXY_CIDRS` (leave blank until verified).
5. `bun x wrangler login` (when you want Wrangler).
6. First live run: sign in to YouTube through `/admin/` (or upload cookies) and
   confirm a real download works; confirm the exit country with `verify-vpn.sh`.
7. JUKES app changes listed in `docs/JUKES_API.md` (warmup/prepare/polling and
   optional session connect).
