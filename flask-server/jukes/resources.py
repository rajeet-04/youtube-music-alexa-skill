"""Cached local capacity snapshots. No shell commands, credentials or upstream calls."""
from __future__ import annotations
import os
import shutil
import threading
import time
from pathlib import Path


class ResourceSampler:
    def __init__(self, audio_dir, *, clock=time.monotonic, read_text=None, disk_usage=shutil.disk_usage):
        self.audio_dir, self.clock = audio_dir, clock
        self.read = read_text or (lambda path: Path(path).read_text())
        self.disk_usage = disk_usage
        self._lock = threading.Lock()
        self._at = None
        self._previous = None
        self._cached = None

    def _text(self, path):
        try:
            return self.read(path)
        except (OSError, ValueError):
            return ''

    def _number(self, path):
        try:
            value = int(self._text(path).strip())
            return value if 0 <= value < 1 << 60 else None
        except ValueError:
            return None

    @staticmethod
    def _fields(text):
        fields = {}
        for line in text.splitlines():
            parts = line.replace(':',' ').split()
            if len(parts)>=2:
                try:
                    fields[parts[0]]=int(parts[1])
                except ValueError:
                    pass
        return fields

    def snapshot(self):
        with self._lock:
            now = self.clock()
            if self._cached is not None and self._at is not None and 0 <= now-self._at < 5:
                return self._cached
            dt = now-self._at if self._at is not None else 0
            cores = float(os.cpu_count() or 1)
            cpu_scope = 'host'
            quota = self._text('/sys/fs/cgroup/cpu.max').split()
            try:
                if quota and quota[0]!='max':
                    cores = min(cores,int(quota[0])/int(quota[1])); cpu_scope='container'
            except (ValueError,IndexError,ZeroDivisionError):
                pass
            usage = self._fields(self._text('/sys/fs/cgroup/cpu.stat')).get('usage_usec')
            if usage is not None:
                usage /= 1e6
                cpu_scope='container'
            else:
                v1usage = self._number('/sys/fs/cgroup/cpuacct/cpuacct.usage')
                v1quota = self._number('/sys/fs/cgroup/cpu/cpu.cfs_quota_us')
                v1period = self._number('/sys/fs/cgroup/cpu/cpu.cfs_period_us')
                if v1quota and v1period:
                    cores=min(cores,v1quota/v1period)
                if v1usage is not None:
                    usage=v1usage/1e9;cpu_scope='container'
            total=idle=None
            try:
                numbers=list(map(int,self._text('/proc/stat').splitlines()[0].split()[1:9]))
                total=sum(numbers);idle=numbers[3]+numbers[4]
            except (ValueError,IndexError):
                pass
            cpu_percent=None
            prev=self._previous
            if prev and dt>0:
                if usage is not None and prev['usage'] is not None and usage>=prev['usage']:
                    cpu_percent=100*(usage-prev['usage'])/dt/cores
                elif cpu_scope=='host' and total is not None and prev['total'] is not None and total>prev['total']:
                    cpu_percent=100*(1-(idle-prev['idle'])/(total-prev['total']))
            if cpu_percent is not None:
                cpu_percent=max(0,min(100,cpu_percent))
            mem=self._fields(self._text('/proc/meminfo'))
            limit=self._number('/sys/fs/cgroup/memory.max')
            used=self._number('/sys/fs/cgroup/memory.current')
            memory_scope='container'
            if used is None:
                limit=self._number('/sys/fs/cgroup/memory/memory.limit_in_bytes')
                used=self._number('/sys/fs/cgroup/memory/memory.usage_in_bytes')
            host_limit=mem.get('MemTotal',0)*1024 or None
            if used is None:
                limit=host_limit
                used=(mem['MemTotal']-mem['MemAvailable'])*1024 if 'MemAvailable' in mem else None
                memory_scope='host'
            elif limit is None or (host_limit and limit>host_limit):
                limit=host_limit
                memory_scope='container usage / host capacity'
            rss=self._fields(self._text('/proc/self/status')).get('VmRSS')
            rx=tx=None
            for line in self._text('/proc/net/dev').splitlines():
                interface,sep,values=line.partition(':')
                if not sep or interface.strip()=='lo':
                    continue
                try:
                    parts=values.split()
                    r,t=int(parts[0]),int(parts[8])
                    rx=(rx or 0)+r;tx=(tx or 0)+t
                except (ValueError,IndexError):
                    continue
            def delta(field,value):
                return (value-prev[field])/dt if prev and dt>0 and value is not None and prev[field] is not None and value>=prev[field] else None
            try:
                disk=self.disk_usage(self.audio_dir)
                disk_view={'total_bytes':disk.total,'used_bytes':disk.used,'free_bytes':disk.free,'scope':'audio filesystem'}
            except OSError:
                disk_view={'total_bytes':None,'used_bytes':None,'free_bytes':None,'scope':'audio filesystem'}
            self._cached={'cpu':{'percent':cpu_percent,'effective_cores':cores,'scope':cpu_scope},
                'memory':{'used_bytes':used,'limit_bytes':limit,'process_bytes':rss*1024 if rss is not None else None,'scope':memory_scope},
                'disk':disk_view, 'network':{'rx_bytes':rx,'tx_bytes':tx,'rx_bytes_per_second':delta('rx',rx),
                    'tx_bytes_per_second':delta('tx',tx),'scope':'network namespace, non-loopback'}}
            self._previous={'usage':usage,'total':total,'idle':idle,'rx':rx,'tx':tx}
            self._at=now
            return self._cached
