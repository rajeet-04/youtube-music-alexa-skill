"""Real-media tests: early output, bounded storage and failed generations."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jukes.cache import Cache
from jukes.config import CacheConfig
from jukes.models import AudioKey
from jukes.streams import Streams


@pytest.fixture
def manager(tmp_path):
    cache = Cache(CacheConfig(database_path=tmp_path/'db', audio_dir=tmp_path/'audio',
        requested_limit_bytes=200_000_000, warmup_limit_bytes=100_000_000,
        min_free_disk_bytes=0))
    streams = Streams(cache, ttl_seconds=0.2)
    yield streams
    streams.shutdown()


@pytest.fixture
def aac(tmp_path):
    path = tmp_path/'tone.aac'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440',
        '-t','12','-c:a','aac','-b:a','128k','-f','adts',str(path)],check=True)
    return path.read_bytes()


def test_two_segments_published_before_upstream_eof(manager, aac):
    key = AudioKey('song', 'p')
    stream = manager.begin(key, {'duration':12, 'acodec':'aac', 'abr':128})
    stream.write(aac)
    deadline = time.monotonic()+5
    while not manager.view(key) and time.monotonic()<deadline:
        time.sleep(.02)
    view = manager.view(key)
    assert view and view['stream_id'] == stream.stream_id
    path, release = manager.lease(stream.stream_id, 'index.m3u8')
    assert '#EXT-X-ENDLIST' not in path.read_text()
    assert len(list(path.parent.glob('*.ts'))) >= 2
    release()
    stream.finish(True)
    manager.complete(key, True)
    assert '#EXT-X-ENDLIST' in path.read_text()


def test_failed_published_generation_never_looks_complete(manager, aac):
    key = AudioKey('song', 'p')
    stream = manager.begin(key, {'duration':12, 'acodec':'aac','abr':128})
    stream.write(aac)
    deadline=time.monotonic()+5
    while not manager.view(key) and time.monotonic()<deadline: time.sleep(.02)
    assert manager.published(key)
    stream.finish(False)
    manager.complete(key, False)
    assert manager.view(key) is None
    assert manager.lease(stream.stream_id,'index.m3u8') is None
    replacement=manager.begin(key, {'duration':12,'acodec':'aac','abr':128})
    assert replacement.stream_id != stream.stream_id


def test_lease_and_grace_prevent_cleanup(manager, aac):
    key=AudioKey('song','p')
    stream=manager.begin(key, {'duration':12,'acodec':'aac','abr':128})
    stream.write(aac); stream.finish(True); manager.complete(key,True)
    path,release=manager.lease(stream.stream_id,'index.m3u8')
    time.sleep(.25); manager.prune()
    assert path.exists()
    release(); manager.prune(); assert path.exists()
    time.sleep(.25); manager.prune(); assert not path.exists()
    assert manager.cache.usage()['requested']==0


def test_no_path_traversal_or_unknown_segments(manager):
    assert manager.lease('unknown','../db') is None


def test_release_is_idempotent_with_two_readers(manager,aac):
    key=AudioKey('song','p')
    stream=manager.begin(key,{'duration':12,'acodec':'aac','abr':128})
    stream.write(aac); stream.finish(True); manager.complete(key,True)
    path,one=manager.lease(stream.stream_id,'index.m3u8')
    _,two=manager.lease(stream.stream_id,'index.m3u8')
    one(); one(); time.sleep(.25); manager.prune()
    assert path.exists()
    two()


def test_partial_mp4_can_publish_without_eof(manager,tmp_path):
    path=tmp_path/'source.m4a'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440',
        '-t','16','-c:a','aac','-b:a','128k','-movflags','+faststart',str(path)],check=True)
    key=AudioKey('mp4','p')
    stream=manager.begin(key,{'duration':16,'acodec':'mp4a.40.2','abr':128})
    payload=path.read_bytes()
    stream.write(payload[:len(payload)*3//4])
    deadline=time.monotonic()+5
    while not manager.view(key) and time.monotonic()<deadline: time.sleep(.02)
    assert manager.view(key)
    stream.write(payload[len(payload)*3//4:]); stream.finish(True); manager.complete(key,True)
    assert not stream.failed


def test_temporary_segment_rename_does_not_kill_monitor(manager,monkeypatch):
    stream=manager.begin(AudioKey('song','p'),{'duration':12,'acodec':'aac','abr':128})
    original=Path.iterdir
    class GoneFile:
        def is_file(self): return True
        def stat(self): raise FileNotFoundError('atomically renamed')
    with manager.lock,monkeypatch.context() as patch:
        patch.setattr(Path,'iterdir',lambda self: iter([GoneFile()]) if self==stream.path else original(self))
        stream.refresh()
    assert not stream.failed


def test_published_playlist_io_failure_isolated_from_file_job(manager,aac,monkeypatch):
    key=AudioKey('song','p')
    stream=manager.begin(key,{'duration':12,'acodec':'aac','abr':128})
    stream.write(aac); stream.finish(True)
    original=Path.write_text
    def fail(path,*args,**kwargs):
        if path.name=='index.publish': raise OSError('disk error')
        return original(path,*args,**kwargs)
    with manager.lock,monkeypatch.context() as patch:
        patch.setattr(Path,'write_text',fail)
        manager.complete(key,True)
    assert stream.failed


def test_long_unknown_or_invalid_media_does_not_publish(manager):
    for metadata in ({}, {'duration':1801}, {'duration':float('nan')}):
        assert manager.begin(AudioKey('song','p'),metadata) is None


def test_copy_aac_and_transcode_opus(manager):
    one=manager.begin(AudioKey('one','p'),{'duration':12,'acodec':'mp4a.40.2','abr':128})
    two=manager.begin(AudioKey('two','p'),{'duration':12,'acodec':'opus','abr':128})
    assert one.command[one.command.index('-c:a')+1]=='copy'
    assert two.command[two.command.index('-c:a')+1]=='aac'
    assert manager.cache.usage()['requested'] >= 2*manager.budget_bytes
