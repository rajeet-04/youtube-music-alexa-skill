#!/bin/sh
# Recreate only the ytmusic service on a new immutable image, reusing the private runtime overlay.
# Usage: scripts/deploy-image.sh sha256:<image id>
# Roll back by running it again with the previous id (printed at the start).
set -eu
NEW=${1:?usage: deploy-image.sh sha256:<image id>}
D=/tmp/jukes-optimized-deploy
OLD=$(docker inspect --format '{{.Image}}' ytmusic)
echo "current image: $OLD"
[ "$OLD" != "$NEW" ] || { echo "already running $NEW"; exit 0; }
docker image inspect "$NEW" >/dev/null
sudo cat "$D/image.json" | sed "s/sha256:[0-9a-f]\{64\}/$NEW/" | sudo tee "$D/image-current.json" >/dev/null
sudo chmod 600 "$D/image-current.json"
sudo grep -q "$NEW" "$D/image-current.json"
ARGS="--project-directory /home/ubuntu/jukes-backend -p jukes-backend -f /home/ubuntu/jukes-backend/docker-compose.yml -f /home/ubuntu/jukes-backend/docker-compose.vpn.yml -f /home/ubuntu/jukes-backend/docker-compose.tunnel-named.yml -f /tmp/jukes-metrics-deploy/image.yml"
sudo docker compose $ARGS -f "$D/image-current.json" up -d --no-deps --no-build ytmusic
n=0
until [ "$(docker inspect --format '{{.State.Health.Status}}' ytmusic)" = healthy ] || [ $n -ge 40 ]; do n=$((n+1)); sleep 3; done
docker inspect --format 'image={{.Image}} health={{.State.Health.Status}}' ytmusic
echo "rollback: scripts/deploy-image.sh $OLD"
