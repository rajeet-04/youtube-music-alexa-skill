import importlib.util
import pytest
from pathlib import Path

spec=importlib.util.spec_from_file_location('bench',Path(__file__).resolve().parents[2]/'scripts/benchmark_progressive.py')
bench=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_nearest_rank_percentile_and_empty_samples():
    assert bench.p95(list(range(1,21)))==19
    assert bench.p95([]) is None


def test_failures_and_hits_are_not_successful_cold_samples():
    rows=[{'state':'ready','cold':True,'first_audio_seconds':2,'complete_seconds':5},
          {'state':'failed','cold':True,'first_audio_seconds':None,'complete_seconds':None},
          {'state':'ready','cold':False,'first_audio_seconds':.1,'complete_seconds':.1}]
    result=bench.summarize(rows)
    assert result['cold_successes']==1
    assert result['cold_failures']==1
    assert result['cache_hits_excluded']==1
    assert result['first_audio_p95_seconds']==2


def test_returned_public_urls_use_selected_benchmark_transport(monkeypatch):
    seen=[]
    class Response:
        status=200
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n): return b'x'
    def open_url(request,**kwargs):
        seen.append(request.full_url); return Response()
    monkeypatch.setattr(bench.urllib.request,'urlopen',open_url)
    bench.http('http://127.0.0.1:5000','GET','https://public.example/v1/audio/song?x=1',first_only=True)
    assert seen==['http://127.0.0.1:5000/v1/audio/song?x=1']


def test_playlist_parser_requires_continuous_published_segments():
    playlist='#EXTM3U\n#EXTINF:2.0,\nseg000000.ts\n#EXTINF:1.8,\nseg000001.ts\n#EXT-X-ENDLIST\n'
    assert bench.parse_playlist(playlist)==([('seg000000.ts',2.),('seg000001.ts',1.8)],True)
    with pytest.raises(ValueError): bench.parse_playlist(playlist.replace('seg000001','seg000003'))


def test_job_failure_after_first_audio_is_a_post_start_failure(monkeypatch):
    calls=0
    def http(base,method,path,body=None,first_only=False):
        nonlocal calls
        if method=='HEAD': return 404,b''
        if method=='POST': return 202,b'{"status":"queued","job_id":"j"}'
        if '/v1/jobs/' in path:
            calls+=1
            if calls==1: return 200,b'{"status":"downloading","job_id":"j","streamable":true,"stream_url":"/v1/streams/id/index.m3u8"}'
            return 200,b'{"status":"failed","error":{"code":"extraction_failed"}}'
        if path.endswith('.m3u8'): return 200,b'#EXTM3U\n#EXTINF:2,\nseg000000.ts\n'
        return 200,b'audio'
    class Consumers:
        def submit(self,*args): return object()
    monkeypatch.setattr(bench,'http',http)
    result=bench._measure('http://backend','video',True,Consumers())
    assert result['streamed']
    assert result['post_start_failure']
