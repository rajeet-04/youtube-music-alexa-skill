"""Private, bounded first-party telemetry. No request/credential data is collected."""
from __future__ import annotations

import math
import time
from contextlib import nullcontext

EVENTS = frozenset(('job_completed', 'job_failed', 'prepare_success', 'prepare_failed',
    'temporary_failure', 'terminal_failure', 'retry_fallback', 'retry_recovery',
    'cache_hit', 'cache_miss', 'main_hit', 'warmup_hit', 'joined',
    'eviction', 'warmup_eviction', 'warmup_expiration', 'missing_result',
    'warmup_request', 'warmup_duplicate', 'warmup_cached', 'warmup_started',
    'warmup_success', 'warmup_consumed', 'warmup_inflight', 'warmup_failed',
    'admission_rejected', 'latency_dropped'))
CATEGORIES = ('all', 'cold', 'cached', 'joined', 'warmed', 'recovered', 'queue', 'extraction')
WINDOWS = {'15m': 900, '1h': 3600, '24h': 86400}


def rate(numerator, denominator):
    return numerator / denominator if denominator else None


class Metrics:
    def __init__(self, store, clock=time.time, sample_limit=10000):
        self.store, self.clock = store, clock
        self.sample_limit = max(1, int(sample_limit))
        self._last_prune = 0.0
        with store.transaction() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS jukes_metrics_totals(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jukes_metrics_info(name TEXT PRIMARY KEY, value REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jukes_metrics_minutes(minute INTEGER, name TEXT, category TEXT,
                    count INTEGER NOT NULL, duration REAL NOT NULL DEFAULT 0, last_at REAL NOT NULL, PRIMARY KEY(minute,name,category));
                CREATE TABLE IF NOT EXISTS jukes_metrics_samples(id INTEGER PRIMARY KEY, at REAL, category TEXT, duration REAL);
                CREATE INDEX IF NOT EXISTS jukes_metrics_sample_time ON jukes_metrics_samples(at);
                CREATE TABLE IF NOT EXISTS jukes_metrics_once(marker TEXT PRIMARY KEY, at REAL);
                CREATE TABLE IF NOT EXISTS jukes_metrics_pending(id TEXT PRIMARY KEY, job_id TEXT,
                    started_at REAL, category TEXT, elapsed REAL NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS jukes_metrics_pending_job ON jukes_metrics_pending(job_id);
                CREATE TABLE IF NOT EXISTS jukes_metrics_warmups(result_id TEXT PRIMARY KEY,
                    completed_at REAL, consumed_at REAL, inflight INTEGER NOT NULL DEFAULT 0);
            ''')
            c.execute('INSERT OR IGNORE INTO jukes_metrics_info VALUES (?,?)', ('started_at', clock()))
        self.prune()

    def record(self, name, *, category='all', duration_seconds=None, once=None, connection=None):
        if name not in EVENTS or category not in CATEGORIES:
            raise ValueError('unsupported metric')
        if duration_seconds is not None and (not math.isfinite(duration_seconds) or duration_seconds < 0):
            raise ValueError('invalid duration')
        now = self.clock()
        with (nullcontext(connection) if connection is not None else self.store.transaction()) as c:
            if once is not None:
                if not c.execute('INSERT OR IGNORE INTO jukes_metrics_once VALUES (?,?)', (name+':'+once, now)).rowcount:
                    return False
            c.execute('INSERT INTO jukes_metrics_totals VALUES (?,1) ON CONFLICT(name) DO UPDATE SET value=value+1', (name,))
            c.execute('INSERT INTO jukes_metrics_minutes VALUES (?,?,?,1,?,?) '
                      'ON CONFLICT(minute,name,category) DO UPDATE SET count=count+1,duration=duration+excluded.duration,last_at=excluded.last_at',
                      (int(now//60)*60, name, category, duration_seconds or 0, now))
            if duration_seconds is not None:
                c.execute('INSERT INTO jukes_metrics_samples(at,category,duration) VALUES (?,?,?)',
                          (now, category, duration_seconds))
                c.execute('DELETE FROM jukes_metrics_samples WHERE category=? AND id NOT IN '
                          '(SELECT id FROM jukes_metrics_samples WHERE category=? ORDER BY id DESC LIMIT ?)',
                          (category, category, self.sample_limit))
        return True

    def prune(self, now=None):
        now = self.clock() if now is None else now
        with self.store.transaction() as c:
            c.execute('DELETE FROM jukes_metrics_minutes WHERE minute < ?', (int((now-86400)//60)*60,))
            c.execute('DELETE FROM jukes_metrics_samples WHERE at < ?', (now-86400,))
            # Markers are needed while retained terminal jobs can emit transitions.
            c.execute('DELETE FROM jukes_metrics_once WHERE at < ?', (now-172800,))
            c.execute('DELETE FROM jukes_metrics_warmups WHERE completed_at < ?', (now-93600,))
        self._last_prune = now

    @staticmethod
    def _latency(values, count):
        values = sorted(values)
        def percentile(p):
            return values[max(0, math.ceil(len(values)*p)-1)] if values else None
        return {'average_seconds': None, 'p50_seconds': percentile(.5), 'p95_seconds': percentile(.95),
                'p99_seconds': percentile(.99) if len(values) >= 100 else None,
                'sample_count': len(values), 'total_count': count, 'sampled': count > len(values)}

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        if now-self._last_prune >= 60:
            self.prune(now)
        with self.store.connection() as c:
            totals = {r['name']: r['value'] for r in c.execute('SELECT * FROM jukes_metrics_totals')}
            started = c.execute("SELECT value FROM jukes_metrics_info WHERE name='started_at'").fetchone()[0]
            minutes = c.execute('SELECT * FROM jukes_metrics_minutes WHERE minute >= ?', (now-86400-60,)).fetchall()
            samples = c.execute('SELECT * FROM jukes_metrics_samples WHERE at >= ?', (now-86400,)).fetchall()
            cohorts = c.execute('SELECT * FROM jukes_metrics_warmups').fetchall()
        windows = {}
        for label, seconds in WINDOWS.items():
            cutoff = now-seconds
            rows = [r for r in minutes if r['last_at'] >= cutoff]
            counters = {}
            for r in rows:
                counters[r['name']] = counters.get(r['name'], 0)+r['count']
            latency = {}
            for category in CATEGORIES:
                relevant = [r for r in rows if r['category'] == category and r['name'] == 'prepare_success']
                count = sum(r['count'] for r in relevant)
                values = [s['duration'] for s in samples if s['category'] == category and s['at'] >= cutoff]
                latency[category] = self._latency(values, count)
                latency[category]['average_seconds'] = sum(r['duration'] for r in relevant)/count if count else None
            selected = [r for r in cohorts if r['completed_at'] is not None and r['completed_at'] >= cutoff]
            consumed = sum(r['consumed_at'] is not None for r in selected)
            trends = []
            for minute in sorted({r['minute'] for r in rows}):
                bucket = [r for r in rows if r['minute'] == minute]
                b = {}
                for r in bucket:
                    b[r['name']] = b.get(r['name'], 0)+r['count']
                success = [r for r in bucket if r['name']=='prepare_success' and r['category'] in ('cold','cached','joined','warmed','recovered')]
                n = sum(r['count'] for r in success)
                trends.append({'at': minute, 'average_seconds': sum(r['duration'] for r in success)/n if n else None,
                    'cache_hit_rate': rate(b.get('cache_hit',0), b.get('cache_hit',0)+b.get('cache_miss',0)),
                    'failure_rate': rate(b.get('prepare_failed',0), b.get('prepare_failed',0)+b.get('prepare_success',0)),
                    'warmup_consumed': b.get('warmup_consumed',0)})
            windows[label] = {'counters': counters, 'latency': latency, 'trends': trends,
                'coverage_seconds': min(seconds, max(0, now-started)), 'minute_resolution_seconds': 60,
                'cache_hit_rate': rate(counters.get('cache_hit',0), counters.get('cache_hit',0)+counters.get('cache_miss',0)),
                'failure_rate': rate(counters.get('prepare_failed',0), counters.get('prepare_failed',0)+counters.get('prepare_success',0)),
                'warmup_hit_rate': rate(counters.get('warmup_hit',0), counters.get('cache_hit',0)+counters.get('cache_miss',0)),
                'warmup_cohort': {'completed': len(selected), 'consumed': consumed,
                    'usefulness': rate(consumed, len(selected)), 'maturing': any(now-r['completed_at'] < 7200 for r in selected)}}
        return {'source': 'jukes', 'started_at': started, 'sample_limit': self.sample_limit,
                'lifetime': totals, 'windows': windows}
