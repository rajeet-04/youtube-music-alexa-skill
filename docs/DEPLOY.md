# Deploying JUKES on a fresh VM (Azure, AWS, OCI)

Everything runs in Docker. No inbound ports are needed: Cloudflare's tunnel dials out. The same steps
work on any Linux VM; every image supports `amd64` and `arm64`, so OCI's free Ampere (ARM) machines work.

## Sizing

2 vCPU and 4 GB RAM. Idle use is about 740 MiB; the sign-in browser needs about 1 GB more while a login
is open. A 1 GB VM thrashes. Disk: 20 GB audio cache limit plus images; 40 GB or more is comfortable.

## Choose the region and the VPN exit together

YouTube blocks datacenter IPs, so all YouTube traffic must leave through the VPN. Time per song is
mostly the distance between the VM and the VPN exit:

| VM region | Surfshark country (`VPN_NATIVE_SERVER_COUNTRIES`) | Measured |
|---|---|---|
| Azure Malaysia West | Singapore | song in under 1 s; London exit was about 5 s |
| AWS ap-south-1 (Mumbai), OCI Hyderabad or Mumbai | India | not measured |
| Anything in Europe | Germany or the Netherlands | not measured |

Measure before settling: from the VM, time a TCP connect to `music.youtube.com` through the VPN.
Under 30 ms is good, 250 ms means the exit is across an ocean.

## First deployment

1. Install Docker and the compose plugin. Clone the repo, check out `azure` (or `master` once merged).
2. Create the `web` network and fill in `.env`:
   `scripts/install-host.sh` does the network, then stops after creating `.env` from `.env.example`.
   Run `uv run --no-project --with werkzeug --with cryptography scripts/setup-secrets.py` to generate the
   admin password hash, session key, credential encryption key and browser control token. Never paste
   them anywhere. Then set:
   - `CLOUDFLARE_TUNNEL_TOKEN`, `SITE_ADDRESS`, `PUBLIC_BASE_URL`, `JUKES_PUBLIC_URL` (your hostname)
   - `WIREGUARD_PRIVATE_KEY` and `WIREGUARD_ADDRESSES` from a Surfshark WireGuard file
   - `VPN_NATIVE_SERVER_COUNTRIES` and `EXPECTED_COUNTRY` for the region chosen above
   - Leave `vpn/.generated/` empty: Gluetun reads a `wg0.conf` there automatically and then refuses to
     start ("endpoint port is set").
3. Cloudflare Zero Trust: tunnel public hostname `HTTP caddy:80` (see `docker-compose.tunnel-named.yml`).
   Optionally add an Access policy on `/admin*` and `/youtube-login*`.
4. Run `scripts/install-host.sh` again: it enables the boot unit and the 03:00 backup cron job.
5. Start: `scripts/start-stack.sh`. It waits for the VPN, runs
   `docker compose -f docker-compose.yml -f docker-compose.tunnel-named.yml -f docker-compose.vpn.yml -f docker-compose.fast.yml up -d`
   and restarts anything that joined Gluetun's network too early.
6. Verify:
   - `bash scripts/verify-vpn.sh` reports the expected exit country
   - `docker ps` shows every container healthy
   - `docker exec ytmusic env | grep -E "FAST_RESOLVER|PROGRESSIVE"` shows both
   - play a song, then `docker logs --tail 5 ytresolver` shows new lines
   - sign in to YouTube once from `/admin/` so the server has cookies (age-restricted songs need them)

## Updating

Use all four compose files every time. A plain `docker compose up` drops the fast resolver and the
trusted-proxy settings, and playback falls back to 20 s or more.

    F="-f docker-compose.yml -f docker-compose.tunnel-named.yml -f docker-compose.vpn.yml -f docker-compose.fast.yml"
    docker compose $F build ytmusic && docker compose $F up -d --no-deps ytmusic

Recreating `gluetun` recreates everything that shares its network. If `ytresolver` or `ytmusic` shows
unhealthy afterwards, `docker restart` it: it may have started during Gluetun's restart loop.

Tests (about 90 s):

    docker run --rm -v $PWD:/r -w /r/flask-server --entrypoint sh $(docker inspect ytmusic --format "{{.Config.Image}}") \
      -c "pip install -q --target /tmp/pt pytest; PYTHONPATH=/tmp/pt python -m pytest -q -p no:cacheprovider tests"

## Backups and moving to another host

`scripts/backup.sh` (cron, 03:00) writes `~/jukes-backups/jukes-*.sqlite3` and `env-*`, keeping 7 days.
These sit on the same disk: copy that folder off the machine (scp, rclone, object storage).

To move: deploy the new host as above, but before first start copy the old `.env` (it holds the credential
key; without it the stored cookies and tokens cannot be read) and run
`scripts/restore.sh ~/jukes-backups/jukes-YYYYMMDD-HHMM.sqlite3`. Installation tokens stay valid, so
phones need no re-login. The audio cache is disposable and refills itself.

## Known gotchas

- `wg0.conf` in `vpn/.generated/` conflicts with the built-in server list (above).
- Per-server WireGuard public keys differ: copying a profile and changing only `Endpoint` does not work.
- The `web` network must exist with the subnet in `DOCKER_WEB_SUBNET` (default `172.18.0.0/16`); if the
  cloud's Docker already uses it, pick another and set it in `.env`.
- Rollback: restore the previous `.env`, run the same four-file `up -d`.
