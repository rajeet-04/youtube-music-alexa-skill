#!/bin/bash
# Saves a diagnostic bundle to disk whenever a container dies with a non-zero exit or OOM.
DIR=/var/log/jukes-crash
MAX_BYTES=$((150*1024*1024))
mkdir -p "$DIR"
declare -A last
prune() {
  while [ "$(du -sb "$DIR" | cut -f1)" -gt "$MAX_BYTES" ]; do
    f=$(ls -1tr "$DIR" | head -1); [ -n "$f" ] || break; rm -f "$DIR/$f"
  done
}
docker events --filter type=container --filter event=die --format '{{.Actor.Attributes.name}} {{index .Actor.Attributes "exitCode"}}' |
while read -r name code; do
  [ "$code" = "0" ] && continue
  now=$(date +%s)
  [ $((now - ${last[$name]:-0})) -lt 30 ] && continue
  last[$name]=$now
  ts=$(date +%Y%m%d-%H%M%S)
  out="$DIR/$ts-$name-exit$code.log"
  {
    echo "== $name exit=$code at $(date -Is)"
    uptime; free -m
    echo "== state"; docker inspect "$name" --format '{{json .State}} restarts={{.RestartCount}}' 2>&1
    echo "== container log (last 300 lines)"; docker logs --tail 300 "$name" 2>&1
    echo "== dmesg (last 60)"; dmesg -T 2>&1 | tail -60
    echo "== docker ps"; docker ps -a --format '{{.Names}} {{.Status}}'
  } > "$out" 2>&1
  prune
done
