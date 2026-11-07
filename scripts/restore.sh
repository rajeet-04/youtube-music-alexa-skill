#!/bin/sh
# Restore a backup made by scripts/backup.sh into the running ytmusic data volume.
# Usage: scripts/restore.sh ~/jukes-backups/jukes-YYYYMMDD-HHMM.sqlite3
# Restore the matching env-YYYYMMDD-HHMM file to .env separately: the credential key in it is
# required to read the encrypted cookies stored in this database.
set -eu
FILE=${1:?usage: restore.sh <jukes-*.sqlite3>}
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 2; }
IMAGE=$(docker inspect ytmusic --format '{{.Config.Image}}')
docker stop ytmusic >/dev/null
docker run --rm --volumes-from ytmusic -v "$(cd "$(dirname "$FILE")" && pwd)/$(basename "$FILE"):/b:ro" \
    --entrypoint sh "$IMAGE" -c 'rm -f /data/jukes.sqlite3-wal /data/jukes.sqlite3-shm && cp /b /data/jukes.sqlite3'
docker start ytmusic >/dev/null
echo "restored $FILE; check: docker inspect ytmusic --format '{{.State.Health.Status}}'"
