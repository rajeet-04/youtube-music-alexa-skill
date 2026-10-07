#!/usr/bin/env python3
"""Measure cold request-to-first-audio and completion with identical public API workloads.

No cache deletion: HEAD hits are recorded and excluded from cold statistics.
Run paired fresh-volume control/candidate containers in alternating order.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def p95(values):
    return sorted(values)[math.ceil(len(values)*.95)-1] if values else None


def summarize(rows):
    cold=[r for r in rows if r['cold']]
    good=[r for r in cold if r['state']=='ready']
    return {'requests':len(rows),'cold_successes':len(good),
        'cold_failures':len(cold)-len(good),'cache_hits_excluded':len(rows)-len(cold),
        'post_start_failures':sum(bool(r.get('post_start_failure')) for r in cold),
        'max_buffer_deficit_seconds':max((r.get('stream_validation',{}).get('max_buffer_deficit_seconds',0.) for r in cold),default=0.),
        'first_audio_p95_seconds':p95([r['first_audio_seconds'] for r in good]),
        'completion_p95_seconds':p95([r['complete_seconds'] for r in good]),
        'first_audio_max_seconds':max((r['first_audio_seconds'] for r in good),default=None)}


def http(base,method,path,body=None,first_only=False):
    # Keep all requests on the selected transport (local origin or public proxy).
    parsed=urllib.parse.urlsplit(path)
    if parsed.scheme: path=urllib.parse.urlunsplit(('','',parsed.path,parsed.query,''))
    data=json.dumps(body).encode() if body is not None else None
    request=urllib.request.Request(urllib.parse.urljoin(base+'/',path),data=data,method=method,
        headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=30) as response:
            return response.status,response.read(1 if first_only else -1)
    except urllib.error.HTTPError as error:
        return error.code,error.read()


def parse_playlist(text):
    entries=[]
    duration=None
    for line in text.splitlines():
        if line.startswith('#EXTINF:'): duration=float(line.split(':',1)[1].split(',')[0])
        elif line and not line.startswith('#'):
            if line!=f'seg{len(entries):06d}.ts' or duration is None or not math.isfinite(duration) or duration<=0:
                raise ValueError('discontinuous or invalid playlist')
            entries.append((line,duration)); duration=None
    return entries,'#EXT-X-ENDLIST' in text


def consume_hls(base,url,deadline):
    """Fetch every segment through ENDLIST and decode the concatenated AAC timeline."""
    fetched=[]
    started=last_progress=time.monotonic()
    duration=gap=deficit=0.
    total=0
    with tempfile.TemporaryFile() as media:
        while time.monotonic()<deadline:
            status,body=http(base,'GET',url)
            if status!=200: raise RuntimeError('playlist_http_'+str(status))
            entries,ended=parse_playlist(body.decode())
            if [name for name,_ in entries[:len(fetched)]]!=fetched:
                raise RuntimeError('playlist_history_changed')
            if fetched: deficit=max(deficit,time.monotonic()-started-duration)
            for name,seconds in entries[len(fetched):]:
                status,segment=http(base,'GET',urllib.parse.urljoin(url,name))
                if status!=200 or not segment: raise RuntimeError('segment_http_'+str(status))
                total+=len(segment)
                if total>100_000_000: raise RuntimeError('stream_too_large')
                media.write(segment)
                fetched.append(name); duration+=seconds
                gap=max(gap,time.monotonic()-last_progress); last_progress=time.monotonic()
            if ended:
                if not fetched: raise RuntimeError('empty_stream')
                media.seek(0)
                probe=subprocess.run(['ffprobe','-v','error','-show_streams','-of','json','pipe:0'],
                    stdin=media,capture_output=True,timeout=15,check=True)
                streams=json.loads(probe.stdout).get('streams',[])
                if not streams or any(s.get('codec_type')!='audio' or s.get('codec_name')!='aac' for s in streams):
                    raise RuntimeError('invalid_stream_codecs')
                media.seek(0)
                decoded=subprocess.run(['ffmpeg','-v','error','-i','pipe:0','-map','0:a:0','-f','null','-'],
                    stdin=media,capture_output=True,timeout=30)
                if decoded.returncode or decoded.stderr:
                    raise RuntimeError('stream_decode_failed')
                return {'segments':len(fetched),'bytes':total,'audio_seconds':duration,
                    'max_playlist_gap_seconds':gap,'max_buffer_deficit_seconds':max(0.,deficit),
                    'decoded':True}
            time.sleep(.1)
    raise RuntimeError('stream_never_completed')


def measure(base,video,progressive=False):
    with ThreadPoolExecutor(max_workers=1) as consumers:
        return _measure(base,video,progressive,consumers)


def _measure(base,video,progressive,consumers):
    check,_=http(base,'HEAD','/v1/audio/'+video)
    started=time.monotonic()
    row={'video_id':video,'cold':check==404,'state':'failed','first_audio_seconds':None,
         'complete_seconds':None,'streamed':False,'error':None}
    if check not in (200,404):
        row['error']='unexpected_head_'+str(check); return row
    opt='?progressive=1' if progressive else ''
    status,raw=http(base,'POST','/v1/audio/prepare'+opt,{'video_id':video})
    info=json.loads(raw)
    expected_duration=(info.get('duration_ms') or 0)/1000
    deadline=started+150
    consumer=None
    while status in (200,202) and time.monotonic()<deadline:
        if row['first_audio_seconds'] is None and info.get('streamable') and info.get('stream_url'):
            stream_status,playlist=http(base,'GET',info['stream_url'])
            segments=[line for line in playlist.decode().splitlines() if line.endswith('.ts')] if stream_status==200 else []
            if segments:
                url=urllib.parse.urljoin(info['stream_url'],segments[0])
                audio_status,_=http(base,'GET',url,first_only=True)
                if audio_status==200:
                    row['first_audio_seconds']=time.monotonic()-started
                    row['streamed']=True
                    consumer=consumers.submit(consume_hls,base,info['stream_url'],deadline)
        if info.get('status')=='ready':
            row['complete_seconds']=time.monotonic()-started
            if row['first_audio_seconds'] is None:
                audio_status,_=http(base,'GET',info['audio_url'],first_only=True)
                if audio_status not in (200,206):
                    row['error']='audio_http_'+str(audio_status); return row
                row['first_audio_seconds']=time.monotonic()-started
            if consumer:
                try:
                    row['stream_validation']=consumer.result(timeout=max(1,deadline-time.monotonic()+30))
                    actual=row['stream_validation']['audio_seconds']
                    if expected_duration and abs(actual-expected_duration)>max(2,expected_duration*.02):
                        raise RuntimeError('stream_duration_mismatch')
                except Exception as error:
                    row['error']='post_start_'+str(error); row['post_start_failure']=True
                    return row
            row['state']='ready'; return row
        if info.get('status') in ('failed','evicted'):
            row['error']=info.get('error'); row['post_start_failure']=row['streamed']; return row
        job_id=info.get('job_id')
        if not job_id: break
        # After first audio, wait for full completion without a stream-ready busy poll.
        query='?wait=10'+('&progressive=1' if progressive and row['first_audio_seconds'] is None else '')
        before=time.monotonic()
        status,raw=http(base,'GET','/v1/jobs/'+job_id+query)
        info=json.loads(raw)
        if time.monotonic()-before<.05 and info.get('status') not in ('ready','failed'):
            time.sleep(.05)
    row['error']=info.get('error') or 'timeout_or_http_'+str(status)
    row['post_start_failure']=row['streamed']
    return row


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('base')
    parser.add_argument('--videos',nargs='+',required=True)
    parser.add_argument('--parallel',type=int,default=4)
    parser.add_argument('--progressive',action='store_true')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    def run(video):
        try: row=measure(args.base.rstrip('/'),video,args.progressive)
        except Exception as error:
            row={'video_id':video,'cold':True,'state':'failed','first_audio_seconds':None,
                 'complete_seconds':None,'error':type(error).__name__}
        print(json.dumps(row),flush=True)
        return row
    with ThreadPoolExecutor(args.parallel) as pool: rows=list(pool.map(run,args.videos))
    result={'base':args.base,'parallel':args.parallel,'progressive':args.progressive,
            'summary':summarize(rows),'rows':rows}
    with open(args.output,'w') as output: json.dump(result,output,indent=2)
    print(json.dumps(result['summary'],indent=2),flush=True)


if __name__=='__main__': main()
