"""Operational accounting: persistence, windows, sampling and input privacy."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jukes.store import Store


def collector(tmp_path, clock, limit=10000):
    from jukes.metrics import Metrics
    return Metrics(Store(tmp_path / 'metrics.sqlite3'), clock=lambda: clock[0], sample_limit=limit)


def test_metrics_windows_and_percentiles(tmp_path):
    clock = [100000.0]
    m = collector(tmp_path, clock)
    for value in range(1, 100):
        m.record('prepare_success', category='cold', duration_seconds=value)
    assert m.snapshot()['windows']['15m']['latency']['cold']['p99_seconds'] is None
    m.record('prepare_success', category='cold', duration_seconds=100)
    latency = m.snapshot()['windows']['15m']['latency']['cold']
    assert latency['average_seconds'] == 50.5
    assert (latency['p50_seconds'], latency['p95_seconds'], latency['p99_seconds']) == (50, 95, 99)
    assert latency['sample_count'] == 100


def test_once_counter_restart_and_pruning(tmp_path):
    clock = [100000.0]
    m = collector(tmp_path, clock)
    assert m.record('job_completed', once='job:one')
    assert not m.record('job_completed', once='job:one')
    m = collector(tmp_path, clock)
    assert m.snapshot()['lifetime']['job_completed'] == 1
    clock[0] += 86401
    m.prune()
    assert m.snapshot()['lifetime']['job_completed'] == 1
    assert m.snapshot()['windows']['24h']['counters'].get('job_completed', 0) == 0


def test_bounded_samples_and_allowlist(tmp_path):
    clock = [100000.0]
    m = collector(tmp_path, clock, 3)
    for value in range(5):
        m.record('prepare_success', category='cold', duration_seconds=value)
    s = m.snapshot()['windows']['15m']['latency']['cold']
    assert s['sample_count'] == 3 and s['sampled']
    with pytest.raises(ValueError):
        m.record('cookie=secret')
    with pytest.raises(ValueError):
        m.record('prepare_success', category='authorization:secret')
