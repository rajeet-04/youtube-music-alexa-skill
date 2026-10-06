"""Local telemetry deltas, quotas and graceful missing data."""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def fixture(tmp_path):
    from jukes.resources import ResourceSampler
    now=[0.0]
    files={'/proc/stat':'cpu 100 0 0 900 0 0 0 0',
        '/proc/meminfo':'MemTotal: 1000 kB\nMemAvailable: 400 kB',
        '/sys/fs/cgroup/cpu.max':'50000 100000',
        '/sys/fs/cgroup/cpu.stat':'usage_usec 1000000',
        '/sys/fs/cgroup/memory.max':'512000',
        '/sys/fs/cgroup/memory.current':'256000',
        '/proc/net/dev':'eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\nlo: 99999 0 0 0 0 0 0 0 99999',
        '/proc/self/status':'VmRSS: 20 kB'}
    def read(path):
        if path not in files: raise OSError('missing')
        return files[path]
    sampler=ResourceSampler(tmp_path,clock=lambda:now[0],read_text=read,
        disk_usage=lambda _:SimpleNamespace(total=1000,used=400,free=600))
    return sampler,now,files


def test_resource_first_sample_and_quota_deltas(tmp_path):
    sampler,now,files=fixture(tmp_path)
    first=sampler.snapshot()
    assert first['cpu']['percent'] is None
    assert first['network']['rx_bytes_per_second'] is None
    assert first['memory']['limit_bytes']==512000
    assert first['memory']['used_bytes']==256000
    assert first['cpu']['effective_cores']==0.5
    now[0]=6
    files['/sys/fs/cgroup/cpu.stat']='usage_usec 2500000'
    files['/proc/net/dev']='eth0: 1600 0 0 0 0 0 0 0 3200 0 0 0 0 0 0 0'
    second=sampler.snapshot()
    assert second['cpu']['percent']==50
    assert second['network']['rx_bytes_per_second']==100
    assert second['network']['tx_bytes_per_second']==200
    now[0]=7
    assert sampler.snapshot()==second


def test_resource_missing_interfaces_and_counter_reset(tmp_path):
    sampler,now,files=fixture(tmp_path)
    sampler.snapshot()
    files.clear(); now[0]=6
    result=sampler.snapshot()
    assert result['cpu']['percent'] is None
    assert result['memory']['limit_bytes'] is None
    assert result['network']['rx_bytes_per_second'] is None
    assert result['disk']['free_bytes']==600


def test_resource_network_counter_reset_is_not_negative(tmp_path):
    sampler,now,files=fixture(tmp_path)
    sampler.snapshot();now[0]=6
    files['/proc/net/dev']='eth0: 1 0 0 0 0 0 0 0 2 0 0 0 0 0 0 0'
    result=sampler.snapshot()
    assert result['network']['rx_bytes_per_second'] is None


def test_resource_v1_quota_and_memory_limits(tmp_path):
    sampler,now,files=fixture(tmp_path)
    for path in list(files):
        if '/sys/fs/cgroup/' in path:del files[path]
    files.update({'/sys/fs/cgroup/cpuacct/cpuacct.usage':'1000000000',
      '/sys/fs/cgroup/cpu/cpu.cfs_quota_us':'25000', '/sys/fs/cgroup/cpu/cpu.cfs_period_us':'100000',
      '/sys/fs/cgroup/memory/memory.limit_in_bytes':'256000', '/sys/fs/cgroup/memory/memory.usage_in_bytes':'128000'})
    sampler.snapshot();now[0]=10
    files['/sys/fs/cgroup/cpuacct/cpuacct.usage']='2250000000'
    result=sampler.snapshot()
    assert result['cpu']['percent']==50
    assert result['memory']['limit_bytes']==256000 and result['memory']['used_bytes']==128000
