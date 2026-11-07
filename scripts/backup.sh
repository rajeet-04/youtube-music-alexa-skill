#!/bin/sh
# Backup of the JUKES database (installation tokens, encrypted cookies) and .env, kept 7 days.
# The copy stays on this VM: also copy $DEST off the machine (scp/rclone), a dead disk takes both.
set -eu
cd "$(dirname "$0")/.."
DEST=${JUKES_BACKUP_DIR:-$HOME/jukes-backups}
STAMP=$(date +%Y%m%d-%H%M)
umask 077
mkdir -p "$DEST"
# sqlite's backup API gives a consistent copy while the app is writing (WAL).
docker exec ytmusic python -c "
import sqlite3, os
src = sqlite3.connect(os.environ['JUKES_DB_PATH'])
dst = sqlite3.connect('/tmp/backup.sqlite3')
src.backup(dst); dst.close()"
docker cp ytmusic:/tmp/backup.sqlite3 "$DEST/jukes-$STAMP.sqlite3"
chmod 600 "$DEST/jukes-$STAMP.sqlite3"  # docker cp ignores umask
docker exec ytmusic rm -f /tmp/backup.sqlite3
cp .env "$DEST/env-$STAMP"
find "$DEST" -type f -mtime +7 -delete
echo "backup ok: $DEST/jukes-$STAMP.sqlite3"
