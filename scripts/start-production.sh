#!/bin/sh
set -eu
# Restore private deployment overlays used by the existing Compose labels.
install -d -m 700 /tmp/jukes-optimized-deploy /tmp/jukes-metrics-deploy
for file in image.json rollback.json compose-args.json rollback.sh; do
    install -m 600 "/home/ubuntu/jukes-backend/deployment/optimized/$file" "/tmp/jukes-optimized-deploy/$file"
done
chmod 700 /tmp/jukes-optimized-deploy/rollback.sh
install -m 600 /home/ubuntu/jukes-backend/deployment/metrics-image.yml /tmp/jukes-metrics-deploy/image.yml
docker start gluetun >/dev/null
count=0
while [ "$(docker inspect --format '{{.State.Health.Status}}' gluetun)" != healthy ]; do
    count=$((count + 1))
    [ "$count" -lt 90 ] || { echo 'VPN health timed out' >&2; exit 1; }
    sleep 2
done
for container in jukes-backend-bgutil-provider-1 youtube-browser ytmusic caddy cloudflared; do
    docker start "$container" >/dev/null
done
count=0
while [ "$(docker inspect --format '{{.State.Health.Status}}' ytmusic)" != healthy ]; do
    count=$((count + 1))
    [ "$count" -lt 60 ] || { echo 'Backend health timed out' >&2; exit 1; }
    sleep 2
done
echo 'JUKES serving stack started; VPN and backend healthy'
