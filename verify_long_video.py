"""Real local HTTP/FFmpeg integration with synthetic media and mocked model output.

No OpenAI requests are made. Test files stay under ignored tests-output/.
Example: python verify_long_video.py --duration 3601 --interval 10 --padding-mib 0
"""
import argparse,ctypes,json,math,os,struct,subprocess,sys,threading,time,uuid,zipfile
from pathlib import Path
from unittest.mock import patch

def memory():
    if os.name!='nt':return {'rss':None,'peak_rss':None}
    class Counters(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong)]+[(k,ctypes.c_size_t) for k in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    m=Counters();m.cb=ctypes.sizeof(m)
    ctypes.windll.kernel32.GetCurrentProcess.restype=ctypes.c_void_p
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes=[ctypes.c_void_p,ctypes.POINTER(Counters),ctypes.c_ulong]
    if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),ctypes.byref(m),m.cb):raise ctypes.WinError()
    return {'rss':m.WorkingSetSize,'peak_rss':m.PeakWorkingSetSize}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration',type=int,default=610);parser.add_argument('--interval',type=float,choices=(.5,1,2,5,10,30),default=2)
    parser.add_argument('--padding-mib',type=int,default=301);args=parser.parse_args()
    if args.duration<1 or args.padding_mib<0:parser.error('duration must be positive and padding must be nonnegative')
    root=Path(__file__).resolve().parent;out=root/'tests-output'/('integration-'+uuid.uuid4().hex[:8]);out.mkdir(parents=True)
    os.environ['VIDEO_MANUAL_DATA_DIR']=str(out/'data')
    # Test-only cap for fabricated usage records. No credentials or API access.
    os.environ['VIDEO_MANUAL_BUDGET_USD']='1000'
    import httpx
    import server as app
    started=time.perf_counter();ff=app.tools()/('ffmpeg.exe' if os.name=='nt' else 'ffmpeg');video=out/'synthetic.mp4'
    subprocess.run([str(ff),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=1','-t',str(args.duration),'-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(video)],check=True)
    if args.padding_mib:
        padding=args.padding_mib*1024*1024
        with video.open('r+b') as f:
            f.seek(0,2);f.write(struct.pack('>I4s',padding,b'free'));f.seek(padding-9,1);f.write(b'\0')
    host=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);app.ORIGIN=f'http://127.0.0.1:{host.server_port}'
    threading.Thread(target=host.serve_forever,daemon=True).start();before=memory();upload_started=time.perf_counter()
    def blocks():
        with video.open('rb') as f:
            for b in iter(lambda:f.read(1024*1024),b''):yield b
    try:
        with httpx.Client(base_url=app.ORIGIN,timeout=300,trust_env=False) as c:
            token=c.get('/api/status').json()['token']
            r=c.post(f'/api/upload?name=synthetic.mp4&interval={args.interval}',content=blocks(),headers={'X-App-Token':token,'Content-Length':str(video.stat().st_size),'Content-Type':'application/octet-stream'})
            assert r.status_code==201,r.text
            jid=r.json()['id'];uploaded=time.perf_counter()
            while True:
                j=c.get('/api/jobs/'+jid).json()
                if j['phase'] in ('prepared','error'):break
                if time.perf_counter()-started>600:raise RuntimeError('integration test watchdog exceeded')
                time.sleep(.25)
            assert j['phase']=='prepared',j
            prepared=time.perf_counter();p=app.jobdir(jid);m=app.read(p/'manifest.json');expected=math.ceil(args.duration/args.interval)
            assert len(m['frames'])==expected,(len(m['frames']),expected)
            assert m['frames'][-1]['timestamp_seconds']==(expected-1)*args.interval
            def fake(path,body,*unused):
                if path=='responses/input_tokens':return {'input_tokens':1000}
                ids=body['response_format']['json_schema']['schema']['$defs']['Observation']['properties']['image_id']['enum']
                obj={'title':'合成動画の試験','observations':[{'id':'O1','image_id':ids[-1],'visible_state':'テスト画像'}],
                     'events':[{'id':'E1','title':'画像の表示','target':'試験画面','observed_change':'画像が表示されている','value':'','observation_ids':['O1'],'anchor_observation_id':'O1','status':'observed_state','uncertainty':'模擬応答による動作検証'}],'questions':[]}
                return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(obj)}}],'usage':{'prompt_tokens':1000,'completion_tokens':100,'prompt_tokens_details':{'cached_tokens':0,'audio_tokens':0}}}
            with patch.object(app,'api_post',side_effect=fake):app.estimate(jid);app.generate(jid)
            assert app.load_job(jid)['phase']=='ready';manual=app.read(p/'manual.json');last=m['frames'][-1]['timestamp_seconds']
            assert manual['steps'][-1]['start']==last
            r=c.get(f'/api/jobs/{jid}/video',headers={'Range':'bytes=0-31'});assert r.status_code==206 and len(r.content)==32
            archive=out/'manual-video.zip'
            with c.stream('GET',f'/api/jobs/{jid}/export?format=zip') as response:
                assert response.status_code==200
                with archive.open('wb') as f:
                    for b in response.iter_bytes(1024*1024):f.write(b)
            with zipfile.ZipFile(archive) as z:
                assert f'data-time="{last}"' in z.read('manual.html').decode()
                assert z.getinfo('video.mp4').file_size==(p/'video.mp4').stat().st_size
            report={'input_bytes':video.stat().st_size,'duration_seconds':j['duration'],'interval':args.interval,'frames':len(m['frames']),'segments':app.load_job(jid)['segments_count'],'last_step_seconds':last,'upload_seconds':uploaded-upload_started,'prepare_seconds':prepared-uploaded,'wall_seconds':time.perf_counter()-started,'before':before,'after':memory(),'actual_paid_api_calls':0,'inference':'mocked; not an accuracy or model latency evaluation','media':'synthetic test pattern; free-box padding does not represent complex high-bitrate video','job_id':jid}
            (out/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2));print('Results: '+str(out))
    finally:host.shutdown();host.server_close()

if __name__=='__main__':main()
