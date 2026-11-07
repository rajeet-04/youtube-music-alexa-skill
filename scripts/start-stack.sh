#!/bin/sh
# Bring the serving stack up after a boot. Containers restart on their own (restart: unless-stopped);
# this waits for the VPN, applies the compose files, and restarts anything that joined gluetun's
# network too early and is still unhealthy.
set -u
cd "$(dirname "$0")/.."
F="-f docker-compose.yml -f docker-compose.tunnel-named.yml -f docker-compose.vpn.yml -f docker-compose.fast.yml"
health() { docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null; }
n=0
until [ "$(health gluetun)" = healthy ] || [ $n -ge 60 ]; do n=$((n + 1)); sleep 3; done
docker compose $F up -d
sleep 20
for c in ytresolver ytmusic youtube-browser; do
    [ "$(health $c)" = healthy ] || docker restart "$c"
done
echo "stack: $(docker ps --format '{{.Names}}={{.Status}}' | tr '\n' ' ')"
