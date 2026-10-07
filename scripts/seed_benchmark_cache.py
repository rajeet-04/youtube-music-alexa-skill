"""TEST ONLY: seed cache cardinality with valid, small audio fixture entries.

Run solely in isolated benchmark containers. No production volumes are needed.
This models entry/lease scan cost, not production's total stored byte count.
"""
import shutil
import subprocess
import time
from pathlib import Path
import sys
sys.path.insert(0,'/app')
from jukes.app import cache_config_from_env
from jukes.cache import Cache
from jukes.models import AudioKey


def main():
    cache=Cache(cache_config_from_env())
    source=cache.staging_dir/'benchmark-seed.m4a'
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','sine=frequency=440',
        '-t','1','-c:a','aac','-b:a','128k','-movflags','+faststart',str(source)],check=True)
    size=source.stat().st_size
    now=time.time()-3600
    # One transaction admits an offline fixture dataset; normal backend admission
    # is still exercised by every live benchmark track after this script finishes.
    with cache.store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM jukes_audio').fetchone()[0]==0, 'fresh test cache required'
        for index in range(465):
            key=AudioKey(f'seed{index:07d}','public-m4a-v1')
            path=cache.files_dir/(cache._digest(key)+'.audio')
            shutil.copyfile(source,path)
            connection.execute('INSERT INTO jukes_audio(video_id,policy,path,pool,size_bytes,completed_at,expires_at,last_used,state) VALUES (?,?,?,?,?,?,NULL,?,?)',
                (key.video_id,key.policy,str(path),'requested',size,now,now,'ready'))
    source.unlink()
    print({'fixture_entries':465,'fixture_bytes':size*465,'models':'entry cardinality, not production byte occupancy'})


if __name__=='__main__': main()
