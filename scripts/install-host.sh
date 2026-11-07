#!/bin/sh
# One-time host setup for a fresh VM (Azure, AWS, OCI...): Docker network, boot unit, nightly backup.
# Run as your normal user from the checkout; it uses sudo only to install the systemd unit.
# Prerequisites: Docker with the compose plugin, and a filled-in .env (docs/DEPLOY.md).
set -eu
cd "$(dirname "$0")/.."
DIR=$(pwd)
SUBNET=${DOCKER_WEB_SUBNET:-172.18.0.0/16}
if ! docker network inspect web >/dev/null 2>&1; then
    docker network create --subnet "$SUBNET" web \
        || { echo "subnet $SUBNET is taken: set DOCKER_WEB_SUBNET in .env and here, then re-run" >&2; exit 1; }
fi
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "created .env from .env.example: fill it in (see docs/DEPLOY.md), then run this again" >&2
    exit 1
fi
mkdir -p "$HOME/jukes-backups"
sed "s#__DIR__#$DIR#" scripts/jukes-stack.service | sudo tee /etc/systemd/system/jukes-stack.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable jukes-stack.service
( crontab -l 2>/dev/null | grep -v 'scripts/backup.sh' || true
  echo "0 3 * * * $DIR/scripts/backup.sh >> $HOME/jukes-backups/backup.log 2>&1" ) | crontab -
echo "host ready: boot unit enabled, nightly backup scheduled. Start now with: scripts/start-stack.sh"
