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


def test_warmup_cohort_does_not_mix_old_completions_with_new_consumption(tmp_path):
    clock = [100000.0]
    m = collector(tmp_path,clock)
    with m.store.transaction() as c:
        m.warmup_started('old',c)
    m.warmup_completed('old',clock[0])
    clock[0] += 1000
    assert m.consume_warmup('old')
    with m.store.transaction() as c:
        m.warmup_started('new',c)
    m.warmup_completed('new',clock[0])
    cohort = m.snapshot()['windows']['15m']['warmup_cohort']
    assert cohort['completed']==1 and cohort['consumed']==0 and cohort['usefulness']==0
    assert not m.consume_warmup('old')


def test_failed_jobs_and_evictions_do_not_inflate_prepare_success(tmp_path):
    clock=[100000.0]; m=collector(tmp_path,clock)
    observation=m.begin_preparation(clock[0])
    with m.store.transaction() as c:
        c.execute("INSERT INTO jukes_jobs VALUES ('job','v','p',1,'failed','video_unavailable',1,2,0,0,'')")
    m.attach_preparation(observation,'job','cold')
    totals=m.snapshot()['lifetime']
    assert totals['prepare_failed']==1 and totals['terminal_failure']==1
    assert totals.get('prepare_success',0)==0
    m.finish_job('job','evicted','cache_evicted')
    assert m.snapshot()['lifetime']['prepare_failed']==1


def test_pending_observations_are_bounded(tmp_path):
    clock=[100000.0]; m=collector(tmp_path,clock)
    with m.store.transaction() as c:
        c.executemany("INSERT INTO jukes_metrics_pending(id,started_at,category) VALUES (?,?,'cold')",
            [(str(n),clock[0]) for n in range(10000)])
    assert m.begin_preparation(clock[0]) is None
    assert m.snapshot()['lifetime']['latency_dropped']==1


def test_live_preparation_latency_uses_monotonic_clock(tmp_path):
    from jukes.metrics import Metrics
    wall=[100000.0]; mono=[10.0]
    m=Metrics(Store(tmp_path/'mono.db'),clock=lambda:wall[0],monotonic=lambda:mono[0])
    observation=m.begin_preparation(wall[0])
    with m.store.transaction() as c:
        c.execute("INSERT INTO jukes_jobs VALUES ('job','v','p',1,'queued',NULL,1,2,0,0,'')")
    m.attach_preparation(observation,'job','cold')
    wall[0]+=1000;mono[0]+=2
    m.finish_job('job','ready')
    assert m.snapshot()['windows']['1h']['latency']['cold']['p50_seconds']==2


def test_warmup_maturity_uses_configured_ttl(tmp_path):
    from jukes.metrics import Metrics
    clock=[100000.0]
    m=Metrics(Store(tmp_path/'ttl.db'),clock=lambda:clock[0],warmup_ttl=30)
    with m.store.transaction() as c:m.warmup_started('warm',c)
    m.warmup_completed('warm',clock[0]);clock[0]+=31
    assert not m.snapshot()['windows']['15m']['warmup_cohort']['maturing']


def test_normal_traffic_prunes_without_admin_visits(tmp_path):
    clock=[100000.0];m=collector(tmp_path,clock)
    m.record('job_completed',once='old')
    clock[0]+=86401
    m.record('job_completed',once='new')
    with m.store.connection() as c:
        assert c.execute('SELECT MIN(minute) FROM jukes_metrics_minutes').fetchone()[0]>=186360
    assert m.snapshot()['lifetime']['job_completed']==2


def test_observer_saturation_keeps_outcomes_without_latency(tmp_path):
    clock=[100000.0];m=collector(tmp_path,clock)
    with m.store.transaction() as c:
        c.executemany("INSERT INTO jukes_metrics_pending(id,started_at,category) VALUES (?,?,'cold')",
            [(str(n),clock[0]) for n in range(10000)])
        c.execute("INSERT INTO jukes_jobs VALUES ('full','v','p',1,'queued',NULL,1,2,0,0,'')")
    observation=m.begin_preparation(clock[0]);assert observation is None
    m.attach_preparation(observation,'full','cold')
    m.finish_job('full','ready')
    snapshot=m.snapshot()
    assert snapshot['lifetime']['prepare_success']==1
    assert snapshot['windows']['15m']['latency']['cold']['sample_count']==0
    assert snapshot['windows']['15m']['latency']['cold']['average_seconds'] is None


def test_recovery_reclaims_orphaned_pending_observers(tmp_path):
    clock=[100000.0];m=collector(tmp_path,clock)
    m.begin_preparation(clock[0])
    restarted=collector(tmp_path,clock)
    with restarted.store.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM jukes_metrics_pending').fetchone()[0]==0
