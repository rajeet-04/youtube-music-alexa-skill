#!/usr/bin/env python3
"""TEST ONLY: feed identical throttled real audio to unmodified old/new coordinators.

This is a reproducibility check, not evidence of YouTube performance.
Never used by the production entrypoint or deployment.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def producer(audio_path,metadata_path=None):
    if metadata_path:
        Path(metadata_path).write_text(json.dumps({'duration':90,'acodec':'mp4a.40.2','abr':128}))
    time.sleep(.5)
    with open(audio_path,'rb') as audio:
        while chunk:=audio.read(16_384):
            sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush(); time.sleep(.04)


def main():
    if len(sys.argv)>1 and sys.argv[1]=='produce':
        producer(sys.argv[2],sys.argv[3] if len(sys.argv)>3 else None)
        return
    sys.path.insert(0,'/app')
    from jukes.app import build_services,create_app
    from jukes.music import Track
    from jukes.routes import Settings
    from waitress import serve
    path=Path('/tmp/benchmark-tone.m4a')
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440',
        '-t','90','-c:a','aac','-b:a','128k','-movflags','+faststart','-y',str(path)],check=True)
    services=build_services()
    class FixtureMusic:
        def resolve(self,selector,context):
            return Track(selector.video_id,'Benchmark tone',('Fixture',),duration_ms=90_000)
    services.music=FixtureMusic()
    def spawn(args,**kwargs):
        command=[sys.executable,__file__,'produce',str(path)]
        if '--print-to-file' in args: command.append(args[args.index('--print-to-file')+2])
        return subprocess.Popen(command,**kwargs)
    services.jobs.extractor._popen=spawn
    port=int(os.environ.get('BENCH_FIXTURE_PORT','5005'))
    app=create_app(Settings(public_base_url=f'http://127.0.0.1:{port}'),services,autorefresh=False)
    serve(app,host='0.0.0.0',port=port,threads=16)


if __name__=='__main__': main()
