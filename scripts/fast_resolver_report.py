"""Read-only summary of extraction latency and fast-resolver use from the JUKES metrics DB.

Run inside the ytmusic container (it opens the database read-only):
  docker cp scripts/fast_resolver_report.py ytmusic:/tmp/r.py
  docker exec ytmusic python /tmp/r.py --since 2026-10-24T14:50:00Z [--until ...]
Samples and minute counters are kept for 24 h, so a window must start within the last day.
"""
import argparse
import json
import math
import os
import sqlite3
import time
from datetime import datetime

CATEGORIES = ('cold', 'cached', 'joined', 'warmed', 'recovered', 'queue', 'extraction', 'fast', 'ytdlp')
COUNTERS = ('prepare_success', 'prepare_failed', 'temporary_failure', 'terminal_failure', 'retry_fallback',
            'fast_fallback', 'extraction_source', 'cache_hit', 'cache_miss', 'job_completed', 'job_failed')


def stamp(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() if text else None


def percentile(values, p):
    return round(values[max(0, math.ceil(len(values) * p) - 1)], 2) if values else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--since')
    parser.add_argument('--until')
    args = parser.parse_args()
    now = time.time()
    since, until = stamp(args.since) or now - 86400, stamp(args.until) or now
    path = os.environ.get('JUKES_DB_PATH', '/data/jukes.sqlite3')
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    latency = {}
    for category in CATEGORIES:
        values = sorted(r[0] for r in db.execute(
            'SELECT duration FROM jukes_metrics_samples WHERE category=? AND at>=? AND at<?', (category, since, until)))
        latency[category] = {'n': len(values), 'p50': percentile(values, .5), 'p95': percentile(values, .95),
                             'max': round(values[-1], 2) if values else None}
    counters = {name: n for name, n in db.execute(
        'SELECT name, SUM(count) FROM jukes_metrics_minutes WHERE minute>=? AND minute<? GROUP BY name',
        (since // 60 * 60, until)) if name in COUNTERS}
    sources = {category: n for category, n in db.execute(
        "SELECT category, SUM(count) FROM jukes_metrics_minutes WHERE name='extraction_source' AND minute>=? AND minute<? "
        "GROUP BY category", (since // 60 * 60, until))}
    fast, total = sources.get('fast', 0), sum(sources.values())
    print(json.dumps({'window': [datetime.utcfromtimestamp(since).isoformat() + 'Z',
                                 datetime.utcfromtimestamp(until).isoformat() + 'Z'],
                      'latency_seconds': latency, 'counters': counters, 'extractions_by_source': sources,
                      'fast_share': round(fast / total, 3) if total else None}, indent=1))


if __name__ == '__main__':
    main()
