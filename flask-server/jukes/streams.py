"""Bounded transient HLS generations, separate from validated file cache entries."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from .models import AudioKey, CacheCapacityError

_SEGMENT = re.compile(r"seg[0-9]{6}\.ts\Z")
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")


class Streams:
    # 30 min at <=256 kbps plus container/playlist overhead, reserved up front.
    budget_bytes = 72_000_000

    def __init__(self, cache, *, ttl_seconds=1800):
        self.cache = cache
        self.ttl_seconds = ttl_seconds
        self.root = cache.audio_dir / 'streams'
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.by_key = {}
        self.by_id = {}
        self.requested = {}
        # Called only by the exclusive app coordinator: old processes cannot own these.
        for path in self.root.iterdir():
            if path.is_dir() and _GENERATION.fullmatch(path.name):
                shutil.rmtree(path)
        for row in cache.store.all_reservations():
            if row['policy'] == 'hls-transient-v1':
                cache.release_reservation(AudioKey(row['video_id'],row['policy']))

    def request(self,key):
        with self.lock:
            self.requested[key]=time.monotonic()
            if len(self.requested)>256:
                self.requested.pop(min(self.requested,key=self.requested.get))

    def wanted(self,key):
        with self.lock:
            return time.monotonic()-self.requested.get(key,-1000)<300

    def begin(self, key, metadata):
        duration = metadata.get('duration')
        if not isinstance(duration,(int,float)) or not math.isfinite(duration) or not 0 < duration <= 1800:
            return None  # Full-file compatibility when progressive eligibility is unknown.
        self.prune()
        stream_id = uuid.uuid4().hex
        reservation_key = AudioKey(stream_id,'hls-transient-v1')
        try:
            self.cache.reserve(reservation_key,requested=True,expected_size=self.budget_bytes)
        except CacheCapacityError:
            return None  # Streaming capacity must not prevent completed-file playback.
        try:
            stream = Stream(self,key,stream_id,reservation_key,metadata)
        except (OSError, subprocess.SubprocessError):
            self.cache.release_reservation(reservation_key)
            shutil.rmtree(self.root/stream_id,ignore_errors=True)
            return None
        with self.lock:
            self.by_key[key] = stream
            self.by_id[stream_id] = stream
        return stream

    def view(self, key):
        with self.lock:
            stream = self.by_key.get(key)
            if not stream or not stream.published or stream.failed:
                return None
            stream.touched = time.monotonic()
            return {'stream_id':stream.stream_id,'video_id':key.video_id,
                    'streamable':True,'stream_ready_seconds':stream.ready_seconds}

    def published(self, key):
        with self.lock:
            stream = self.by_key.get(key)
            return bool(stream and stream.published)

    def complete(self,key,success):
        with self.lock:
            stream=self.by_key.get(key)
            if stream:
                stream.complete = bool(success and not stream.failed)
                stream.failed = bool(not success or stream.failed)
                stream.touched=time.monotonic()
                stream.refresh()

    def lease(self,stream_id,filename):
        if filename != 'index.m3u8' and not _SEGMENT.fullmatch(filename):
            return None
        self.prune()
        with self.lock:
            stream=self.by_id.get(stream_id)
            if not stream or stream.failed or not stream.published:
                return None
            if filename != 'index.m3u8' and filename not in stream.segments:
                return None
            path=stream.path/filename
            if not path.is_file(): return None
            stream.readers+=1
            stream.touched=time.monotonic()
        released=False
        def release():
            nonlocal released
            with self.lock:
                if released: return
                released=True
                stream.readers=max(0,stream.readers-1)
                stream.touched=time.monotonic()
        return path,release

    def prune(self):
        with self.lock:
            for ident,stream in list(self.by_id.items()):
                if stream.finished and not stream.readers and time.monotonic()-stream.touched > self.ttl_seconds:
                    shutil.rmtree(stream.path,ignore_errors=True)
                    self.cache.release_reservation(stream.reservation_key)
                    del self.by_id[ident]
                    if self.by_key.get(stream.key) is stream: del self.by_key[stream.key]

    def shutdown(self):
        for stream in list(self.by_id.values()): stream.finish(False)


class Stream:
    def __init__(self,manager,key,stream_id,reservation_key,metadata):
        self.manager,self.key,self.stream_id,self.reservation_key=manager,key,stream_id,reservation_key
        self.path=manager.root/stream_id
        self.path.mkdir(mode=0o700)
        self.started=self.touched=time.monotonic()
        self.published=self.failed=self.finished=self.complete=False
        self.readers=0
        self.segments=set()
        self.ready_seconds=None
        self.write_lock=threading.Lock()
        codec=str(metadata.get('acodec',''))
        abr=metadata.get('abr')
        copy=codec in ('aac','mp4a.40.2') and isinstance(abr,(int,float)) and 0 < abr <= 256
        self.command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error',
            '-probesize','32768','-analyzeduration','1000000','-i','pipe:0',
            '-map','0:a:0','-vn','-t','1800','-c:a','copy' if copy else 'aac']
        if not copy: self.command+=['-b:a','128k']
        self.command+=['-f','hls','-hls_time','2','-hls_list_size','0',
            '-hls_playlist_type','event','-hls_flags','temp_file',
            '-hls_segment_filename',str(self.path/'seg%06d.ts'),str(self.path/'internal.m3u8')]
        self.process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,bufsize=0)
        self.monitor=threading.Thread(target=self._monitor,daemon=True)
        self.monitor.start()

    def _monitor(self):
        while not self.finished:
            with self.manager.lock:
                self.refresh()
                if self.process.poll() is not None and self.process.returncode:
                    self.failed=True
            time.sleep(.025)

    def refresh(self):
        try: self._refresh()
        except OSError:
            self.failed=True
            self.process.kill()

    def _refresh(self):
        if self.failed: return
        # Hard duration/bitrate limits bound normal output; kill unexpected oversized output.
        allocated=0
        for path in self.path.iterdir():
            try:
                if path.is_file(): allocated+=path.stat().st_size
            except FileNotFoundError:
                continue  # FFmpeg atomically renames .tmp segments while we inspect them.
        if allocated > self.manager.budget_bytes:
            self.failed=True
            self.process.kill()
            return
        try: playlist=(self.path/'internal.m3u8').read_text()
        except (OSError,UnicodeError): return
        segments={line for line in playlist.splitlines() if _SEGMENT.fullmatch(line)}
        if len(segments)<2 or any(not (self.path/name).is_file() for name in segments): return
        if not self.published:
            # Validate the first immutable segment before offering any bytes to a client.
            try:
                probe=subprocess.run(['ffprobe','-v','error','-show_streams','-of','json',
                    str(self.path/sorted(segments)[0])],capture_output=True,timeout=3,check=True)
                streams=json.loads(probe.stdout).get('streams',[])
                if not streams or any(s.get('codec_type')!='audio' or s.get('codec_name')!='aac' for s in streams):
                    self.failed=True; self.process.kill(); return
            except (OSError,ValueError,subprocess.SubprocessError):
                self.failed=True; self.process.kill(); return
            self.published=True
            self.ready_seconds=time.monotonic()-self.started
        if not self.complete: playlist=playlist.replace('#EXT-X-ENDLIST\n','')
        tmp=self.path/'index.publish'
        tmp.write_text(playlist)
        os.replace(tmp,self.path/'index.m3u8')
        self.segments=segments

    def write(self,data):
        if self.failed or self.finished: return
        with self.write_lock:
            try:
                view=memoryview(data)
                while view:
                    written=self.process.stdin.write(view)
                    if not written: raise BrokenPipeError
                    view=view[written:]
            except (BrokenPipeError,OSError,ValueError): self.failed=True

    def finish(self,success):
        with self.write_lock:
            if self.finished: return
            if not success:
                self.failed=True
                self.process.kill()
            try: self.process.stdin.close()
            except (OSError,ValueError): pass
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(); self.failed=True
            if self.process.returncode: self.failed=True
            with self.manager.lock:
                self.refresh()
                self.finished=True
                self.touched=time.monotonic()
        self.monitor.join(timeout=1)
