"""Local video-to-manual application. Bind to loopback; keys stay on the server."""
from __future__ import annotations
import argparse,base64,copy,datetime,hashlib,html,io,json,math,mimetypes,os,re,secrets,shutil,subprocess,sys,threading,time,uuid,webbrowser,zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs,quote
import httpx
from segments import plan_segments, segment_prompt, merge_segments
from protocol import PROMPT,Extraction
from evidence import validate_v2

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('VIDEO_MANUAL_DATA_DIR',str(ROOT/'data'))).resolve()
DATA.mkdir(parents=True,exist_ok=True)
MODEL='gpt-6.1-terra';MAX_OUTPUT=8192
TOKEN=secrets.token_urlsafe(32);ORIGIN='';STATE_LOCK=threading.RLock();WORK=threading.Lock()
# Optional integration points are injected in memory by a trusted local launcher.
external_reserve=None;external_settle=None

def read(p):return json.loads(p.read_text('utf-8'))
def write(p,x):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(p)
def digest(b):return hashlib.sha256(b).hexdigest()
def file_digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def run_media(jid,cmd,duration,label):
    """Drain progress continuously; no whole-file buffering or fixed time cutoff."""
    p=jobdir(jid)
    with (p/'media-error.log').open('wb') as err:
        proc=subprocess.Popen(cmd+['-progress','pipe:1','-nostats'],stdout=subprocess.PIPE,stderr=err,stdin=subprocess.DEVNULL,text=True,encoding='utf-8',errors='replace')
        try:
            for line in proc.stdout:
                k,_,v=line.strip().partition('=')
                if k=='out_time_us' and v.lstrip('-').isdigit():
                    fraction=min(1,max(0,int(v)/1e6/duration))
                    change(jid,message=f'{label} {fraction:.0%}',progress=fraction)
            code=proc.wait()
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
    if code:raise ValueError(label+'に失敗しました。入力動画とディスク空き容量を確認してください。')

def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def jobdir(jid):
    if not re.fullmatch(r'[a-f0-9]{32}',jid):raise ValueError('作業IDが正しくありません。')
    p=DATA/jid
    if not (p/'job.json').is_file():raise FileNotFoundError('作業が見つかりません。')
    return p
def load_job(jid):
    with STATE_LOCK:return read(jobdir(jid)/'job.json')
def change(jid,**values):
    with STATE_LOCK:
        p=jobdir(jid)/'job.json';j=read(p);j.update(values,updated=utc());write(p,j);return j
def safe_error(exc):
    if isinstance(exc,(ValueError,FileNotFoundError)):return str(exc)
    return '処理を完了できませんでした。入力動画や設定を確認してください。'
def key():
    k=os.environ.get('OPENAI_API_KEY','')
    if not k:raise ValueError('OpenAI APIキーが設定されていません。起動前にOPENAI_API_KEYを設定してください。サンプルはキーなしで確認できます。')
    return k
def tools():
    path=os.environ.get('VIDEO_MANUAL_FFMPEG_DIR')
    candidates=[Path(path)] if path else []
    candidates.extend([ROOT/'runtime',ROOT.parent/'video-manual-lab/runtime/ffmpeg'])
    suffix='.exe' if os.name=='nt' else ''
    for d in candidates:
        if all((d/(n+suffix)).is_file() for n in ['ffmpeg','ffprobe']):return d
    found=shutil.which('ffmpeg');probe=shutil.which('ffprobe')
    if found and probe and Path(found).parent==Path(probe).parent:return Path(found).parent
    raise ValueError('FFmpegが見つかりません。VIDEO_MANUAL_FFMPEG_DIRにffmpegとffprobeのフォルダを指定してください。')
def watch_functions():
    d=tools();suffix='.exe' if os.name=='nt' else ''
    sources=[ROOT/'vendor/watch-skill/src',ROOT.parent/'video-manual-lab/vendor/watch-skill/src']
    for p in sources:
        if (p/'watch_skill').is_dir():
            if str(p) not in sys.path:sys.path.insert(0,str(p))
            break
    os.environ.update(WATCHSKILL_BIN_DIR=str(d),WATCHSKILL_DATA_DIR=str(DATA/'watch-state'),WATCHSKILL_OFFLINE='true',WATCHSKILL_SCENE_DESCRIPTIONS='off',WATCHSKILL_OCR_ENABLED='false',WATCHSKILL_LOCAL_WHISPER_ENABLED='false',WATCHSKILL_CLOUD_STT_ENABLED='false')
    try:
        from watch_skill.watch import watch
        from watch_skill.perceive.media import probe
    except ImportError:raise ValueError('画像抽出の依存ライブラリがありません。セットアップを実行してください。') from None
    return watch,probe,d/('ffmpeg'+suffix)

def prepare(jid):
    j=load_job(jid);p=jobdir(jid);watch,probe,ffmpeg=watch_functions();src=p/j['source_file']
    meta=probe(src);duration=meta.duration_seconds
    if not math.isfinite(duration) or duration<=0:raise ValueError('動画の長さを確認できませんでした。')
    count=max(1,math.ceil(duration/j['interval']))
    change(jid,phase='extracting',message='動画から根拠画像を抽出しています。',duration=duration)
    frames=[]
    if count<=120:
        times=[round(i*j['interval'],3) for i in range(count) if i*j['interval']<duration-.08]
        if not times:times=[0.0]
        result=watch(str(src),run_ocr=False,allow_local_whisper=False,allow_cloud_stt=False,out_dir=p/'extraction',use_cache=False,frame_width=meta.width,max_frames=len(times),cue_timestamps=times)
        images=[(Path(f['path']),f['timestamp_seconds']) for f in result.perception.to_dict()['frames']]
        method='watch-skill pinned timestamps'
    else:
        # A single sequential decode is bounded in RAM even for hours of video.
        out=p/'frames';out.mkdir(exist_ok=True)
        cmd=[str(ffmpeg),'-hide_banner','-loglevel','error','-y','-i',str(src),'-an','-vf',f"fps=1/{j['interval']}:start_time=0:round=near:eof_action=pass",'-q:v','4','-start_number','0',str(out/'frame_%08d.jpg')]
        run_media(jid,cmd,duration,'根拠画像を抽出しています')
        images=[(image,round(i*j['interval'],3)) for i,image in enumerate(sorted(out.glob('frame_*.jpg')))]
        method='ffmpeg sequential uniform fps; nominal resampled timestamps'
    for i,(image,t) in enumerate(images):
        image=image.resolve();assert image.is_relative_to(p.resolve())
        frames.append({'image_id':f'F{i:03d}','timestamp_seconds':t,'path':image.relative_to(p).as_posix(),'sha256':file_digest(image)})
    if not frames:raise ValueError('動画から画像を抽出できませんでした。')
    manifest={'frames':frames,'duration_seconds':duration,'interval_seconds':j['interval'],'video_sha256':file_digest(src),'sampling_method':method};write(p/'manifest.json',manifest)
    change(jid,message='ブラウザで再生できる動画を準備しています。')
    cmd=[str(ffmpeg),'-hide_banner','-loglevel','error','-y','-i',str(src),'-map','0:v:0','-map','0:a:0?']
    cmd+=['-vf','scale=min(1920\\,iw):-2','-c:v','libx264','-preset','veryfast','-crf','23','-pix_fmt','yuv420p']
    cmd+=['-c:a','aac','-movflags','+faststart',str(p/'video.mp4')]
    run_media(jid,cmd,duration,'再生用動画を準備しています')
    change(jid,phase='prepared',message='画像抽出が完了しました。長い動画は区間ごとに生成します。',frames_count=len(frames),video_file='video.mp4',progress=1)

def request_body(jid,plan=None):
    p=jobdir(jid);m=read(p/'manifest.json');content=[]
    selected_ids=set(plan['image_ids']) if plan else None
    selected=m['frames'] if plan is None else [f for f in m['frames'] if f['image_id'] in selected_ids]
    for f in selected:
        bb=(p/f['path']).read_bytes()
        if digest(bb)!=f['sha256']:raise ValueError('根拠画像が変更されています。動画を再登録してください。')
        content.extend([{'type':'text','text':f['image_id']},{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(bb).decode(),'detail':'high'}}])
    schema=Extraction.model_json_schema();schema['$defs']['Observation']['properties']['image_id']={'type':'string','enum':[f['image_id'] for f in selected]}
    return {'model':MODEL,'messages':[{'role':'system','content':PROMPT+(segment_prompt(plan) if plan else '')},{'role':'user','content':content}],'max_completion_tokens':MAX_OUTPUT,'reasoning_effort':'low','response_format':{'type':'json_schema','json_schema':{'name':'video_observations','strict':True,'schema':schema}},'service_tier':'default','store':False}
def api_post(path,body,timeout=180):
    # Deliberately no automatic retries for metered requests.
    try:
        with httpx.Client(timeout=timeout,trust_env=False,transport=httpx.HTTPTransport(retries=0)) as c:r=c.post('https://api.openai.com/v1/'+path,json=body,headers={'Authorization':'Bearer '+key()})
    except (httpx.TimeoutException,httpx.TransportError):raise ValueError('APIの通信結果を確認できません。自動再試行はしません。費用の確認が必要です。') from None
    if r.status_code!=200:raise ValueError(f'OpenAI APIでエラーが発生しました（HTTP {r.status_code}）。キー・利用権限・残高を確認してください。')
    return r.json()
def count_request(b):
    items=[]
    for m in b['messages']:
        if isinstance(m['content'],str):items.append(m)
        else:items.append({'role':m['role'],'content':[{'type':'input_text','text':x['text']} if x['type']=='text' else {'type':'input_image','image_url':x['image_url']['url'],'detail':'high'} for x in m['content']]})
    counted=api_post('responses/input_tokens',{'model':MODEL,'input':items,'text':{'format':{'type':'json_schema',**b['response_format']['json_schema']}},'reasoning':{'effort':'low'}},120)
    n=counted['input_tokens']+4096
    return n,counted['input_tokens']

def estimate(jid):
    p=jobdir(jid);frames=read(p/'manifest.json')['frames'];plans=plan_segments(frames);estimates=[]
    # Completed requests are reused. Never automatically repeat a metered request.
    previous=read(p/'estimate.json').get('segments',[]) if (p/'estimate.json').exists() else []
    if previous:plans=[{k:x[k] for k in ('id','image_ids','owned_image_ids')} for x in previous]
    reusable={x['id']:x for x in previous if (p/'segments'/x['id']/'settled.json').exists()}
    queue=list(plans)
    while queue:
        plan=queue.pop(0)
        change(jid,phase='estimating',message=f'区間 {len(estimates)+1}/{len(estimates)+len(queue)+1} の画像を送信し、費用を確認しています。')
        b=request_body(jid,plan);signature=digest(json.dumps(b,sort_keys=True).encode())
        old=reusable.get(plan['id'])
        if old:
            if old['body_sha256']!=signature:raise ValueError('完了済み区間の根拠が変わっています。別の動画として登録してください。')
            estimates.append(old);continue
        n,counted=count_request(b)
        if n>272000:
            owned=plan['owned_image_ids'];mid=len(owned)//2
            if not mid:raise ValueError('1枚の画像がモデルの入力枠を超えました。画像解像度を下げてください。')
            children=[]
            for suffix,ids in [('L',owned[:mid]),('R',owned[mid:])]:
                at=next(k for k,f in enumerate(frames) if f['image_id']==ids[0])
                context=[f['image_id'] for f in frames[max(0,at-2):at]]
                children.append({'id':plan['id']+suffix,'image_ids':context+ids,'owned_image_ids':ids})
            queue[0:0]=children
            continue
        estimates.append({**plan,'upper_usd':round((n*2.5+MAX_OUTPUT*5)/1e6,6),'max_input_tokens':n,'counted_input_tokens':counted,'body_sha256':signature,'model':MODEL})
    pending=[x for x in estimates if not (p/'segments'/x['id']/'settled.json').exists()]
    upper=round(sum(x['upper_usd'] for x in pending),6)
    write(p/'estimate.json',{'upper_usd':upper,'segments':estimates,'model':MODEL,'created':utc()})
    change(jid,phase='awaiting_generation',message=f'{len(estimates)}区間に分けて生成します。残りの最大費用を確認してください。',estimate_usd=upper,segments_count=len(estimates),segments_done=len(estimates)-len(pending))

class Budget:
    """Persistent local cap, including unresolved reservations."""
    def __init__(self,path):self.path=path;self.lock=threading.Lock()
    def reserve(self,jid,upper):
        with self.lock:
            x=read(self.path) if self.path.exists() else {'spent':0,'pending':{}}
            if x['pending']:raise ValueError('結果が未確認のAPI要求があります。重複課金を避けるため生成を停止しています。data/budget.jsonとAPI利用状況を確認してください。')
            cap=float(os.environ.get('VIDEO_MANUAL_BUDGET_USD','.60'))
            if x['spent']+upper>cap:raise ValueError('アプリの累計利用上限に達します。使用済み費用と上限設定を確認してください。')
            x['pending'][jid]=upper;write(self.path,x)
    def settle(self,jid,usd):
        with self.lock:
            x=read(self.path);assert usd<=x['pending'][jid]+1e-9
            x['spent']+=usd;x['pending'].pop(jid);write(self.path,x)
    def public(self):
        with self.lock:
            x=read(self.path) if self.path.exists() else {'spent':0,'pending':{}}
            return {'spent_usd':x['spent'],'unresolved':bool(x['pending']),'cap_usd':float(os.environ.get('VIDEO_MANUAL_BUDGET_USD','.60'))}
BUDGET=Budget(DATA/'budget.json')

def generate_single(jid):
    p=jobdir(jid);est=read(p/'estimate.json');b=request_body(jid)
    if digest(json.dumps(b,sort_keys=True).encode())!=est['body_sha256']:raise ValueError('画像または生成条件が変わりました。もう一度準備してください。')
    BUDGET.reserve(jid,est['upper_usd']);rid=None
    if external_reserve:
        try:rid=external_reserve('app-'+jid,est['upper_usd'],{'model':MODEL,'max_input_tokens':est['max_input_tokens'],'max_output_tokens':MAX_OUTPUT})
        except Exception:
            BUDGET.settle(jid,0);raise ValueError('共有利用予算の確認に失敗しました。予算の残額と未確定要求を確認してください。') from None
    change(jid,phase='generating',message='画面の変化から手順を抽出しています。')
    started=time.perf_counter();obj=api_post('chat/completions',b)
    # Preserve successful response before validating; never discard charged output.
    write(p/'response.json',obj);u=obj['usage'];cached=u.get('prompt_tokens_details',{}).get('cached_tokens',0)
    known=set(u.get('prompt_tokens_details',{}))<={'cached_tokens','audio_tokens'}
    usd=((u['prompt_tokens']-cached)+cached*.05+u['completion_tokens']*5)/1e6 if known else (u['prompt_tokens']*2.5+u['completion_tokens']*5)/1e6
    if u['prompt_tokens']>est['max_input_tokens'] or usd>est['upper_usd']:raise ValueError('想定上限と使用量が一致しません。費用を確認してください。')
    if external_settle and rid:external_settle(rid,'app-'+jid,usd,u,time.perf_counter()-started)
    BUDGET.settle(jid,usd);change(jid,cost_usd=usd)
    if obj['choices'][0]['finish_reason']!='stop':raise ValueError('生成が途中で終了しました。出力は保存しましたが手順書として公開しません。')
    try:parsed=json.loads(obj['choices'][0]['message']['content']);Extraction.model_validate(parsed)
    except Exception:raise ValueError('生成結果の形式を確認できませんでした。元の応答は保存されています。') from None
    write(p/'extraction.json',parsed);apply_extraction(jid,parsed)

def generate(jid):
    p=jobdir(jid);est=read(p/'estimate.json')
    if 'segments' not in est:return generate_single(jid)
    plans=est['segments'];remaining=[x for x in plans if not (p/'segments'/x['id']/'settled.json').exists()]
    public=BUDGET.public()
    if public['unresolved']:raise ValueError('結果未確認のAPI要求があります。費用を確認してから再開してください。')
    if public['spent_usd']+sum(x['upper_usd'] for x in remaining)>public['cap_usd']:
        raise ValueError(f"残りの最大費用はUSD {est['upper_usd']:.3f}です。累計予算の残額USD {max(0,public['cap_usd']-public['spent_usd']):.3f}を超えます。VIDEO_MANUAL_BUDGET_USDの設定を確認してください。")
    # Verify the entire approved plan before the first paid request.
    for plan in plans:
        b=request_body(jid,plan)
        if digest(json.dumps(b,sort_keys=True).encode())!=plan['body_sha256']:raise ValueError('根拠画像か生成条件が変わりました。費用を再確認してください。')
    for i,plan in enumerate(plans):
        folder=p/'segments'/plan['id'];folder.mkdir(parents=True,exist_ok=True)
        if (folder/'settled.json').exists():continue
        label=jid+'-'+plan['id'];BUDGET.reserve(label,plan['upper_usd']);rid=None
        if external_reserve:
            try:rid=external_reserve('app-'+label,plan['upper_usd'],{'model':MODEL,'max_input_tokens':plan['max_input_tokens'],'max_output_tokens':MAX_OUTPUT})
            except Exception:
                BUDGET.settle(label,0);raise ValueError('共有予算の確認に失敗しました。完了済み区間は保持されています。') from None
        change(jid,phase='generating',message=f'区間 {i+1}/{len(plans)} の手順を抽出しています。',segments_done=i)
        started=time.perf_counter();obj=api_post('chat/completions',request_body(jid,plan));write(folder/'response.json',obj)
        u=obj['usage'];cached=u.get('prompt_tokens_details',{}).get('cached_tokens',0)
        known=set(u.get('prompt_tokens_details',{}))<={'cached_tokens','audio_tokens'}
        usd=((u['prompt_tokens']-cached)+cached*.05+u['completion_tokens']*5)/1e6 if known else (u['prompt_tokens']*2.5+u['completion_tokens']*5)/1e6
        if u['prompt_tokens']>plan['max_input_tokens'] or usd>plan['upper_usd']:raise ValueError('費用見積もりと使用量が一致しません。保存済み応答と利用状況を確認してください。')
        if external_settle and rid:external_settle(rid,'app-'+label,usd,u,time.perf_counter()-started)
        BUDGET.settle(label,usd);write(folder/'settled.json',{'cost_usd':usd,'body_sha256':plan['body_sha256']})
        change(jid,cost_usd=sum(read(x)['cost_usd'] for x in (p/'segments').glob('*/settled.json')),segments_done=i+1)
    parts=[];manifest=read(p/'manifest.json')
    for plan in plans:
        obj=read(p/'segments'/plan['id']/'response.json')
        if obj['choices'][0]['finish_reason']!='stop':raise ValueError('区間の生成が途中で終了しました。課金済みの応答を保持し、出力を保留しています。')
        try:parsed=json.loads(obj['choices'][0]['message']['content']);Extraction.model_validate(parsed)
        except Exception:raise ValueError('区間の応答形式が不正です。保存済み応答を確認してください。') from None
        subset={'frames':[f for f in manifest['frames'] if f['image_id'] in plan['image_ids']]}
        gate=validate_v2(parsed,subset);write(p/'segments'/plan['id']/'gate.json',gate)
        if gate['status']=='blocked':
            change(jid,phase='blocked',message='区間の根拠画像か順序に問題があるため、出力を保留しました。',issues=gate['structural_errors']);return
        parts.append((plan,parsed))
    try:merged=merge_segments(parts,manifest['frames'])
    except ValueError as e:change(jid,phase='blocked',message=str(e));return
    write(p/'extraction.json',merged);apply_extraction(jid,merged)

def apply_extraction(jid,obj):
    p=jobdir(jid);m=read(p/'manifest.json');g=validate_v2(obj,m);write(p/'gate.json',g)
    if g['status']=='blocked':
        change(jid,phase='blocked',message='根拠画像の参照や順序に問題があるため、手順書の出力を保留しました。',issues=g['structural_errors']);return
    frames={f['image_id']:f for f in m['frames']};obs={o['id']:o for o in obj['observations']};steps=[]
    for e in g['resolved_events']:
        if e['id'] in g['held_event_ids']:continue
        ids=list(dict.fromkeys(obs[x]['image_id'] for x in e['observation_ids']))
        steps.append({'id':e['id'],'title':e['title'],'text':e['observed_change'],'target':e['target'],'value':e['value'],'uncertainty':e['uncertainty'],'inferred':e['status']=='inferred_transition','start':e['host_start_seconds'],'end':e['host_seconds'],'images':[{'id':fid,'time':frames[fid]['timestamp_seconds']} for fid in ids]})
    manual={'title':obj['title'],'steps':steps,'questions':[q['question'] for q in obj['questions']],'held_events':g['held_event_ids'],'operator_approved':False,'edited':False};write(p/'manual.json',manual)
    change(jid,phase='ready',message='手順書の下書きができました。動画と照合してください。',steps_count=len(steps))

def submit(jid,fn):
    if not WORK.acquire(blocking=False):raise ValueError('別の動画を処理中です。完了を待ってから実行してください。')
    change(jid,phase='queued',message='処理を開始しています。')
    def worker():
        try:fn(jid)
        except Exception as exc:change(jid,phase='error',message=safe_error(exc))
        finally:WORK.release()
    threading.Thread(target=worker,daemon=True).start()

def new_job(name,interval):
    jid=uuid.uuid4().hex;p=DATA/jid;p.mkdir();j={'id':jid,'name':name[:240],'created':utc(),'updated':utc(),'phase':'uploaded','message':'動画を読み込みました。','interval':interval,'sample':False};write(p/'job.json',j);return jid
def sample_job():
    demo=ROOT/'demo'
    if not (demo/'manifest.json').exists():raise ValueError('サンプルファイルが見つかりません。')
    jid=new_job('サンプル：備品の保管と取消.webm',.5);p=jobdir(jid);shutil.copy2(demo/'video.mp4',p/'video.mp4');m=read(demo/'manifest.json')
    shutil.copytree(demo/'frames',p/'frames');write(p/'manifest.json',m);write(p/'extraction.json',read(demo/'extraction.json'))
    change(jid,sample=True,video_file='video.mp4',duration=m['duration_seconds'],frames_count=len(m['frames']),cost_usd=0)
    apply_extraction(jid,read(p/'extraction.json'));return jid
def view(jid):
    j=load_job(jid);p=jobdir(jid)
    if (p/'manual.json').exists():j['manual']=read(p/'manual.json')
    if (p/'manifest.json').exists():j['frames']=[{'id':f['image_id'],'time':f['timestamp_seconds']} for f in read(p/'manifest.json')['frames']]
    return j
def save_manual(jid,updates):
    p=jobdir(jid);j=load_job(jid)
    if j['phase']!='ready':raise ValueError('手順書が完成してから編集できます。')
    original=read(p/'manual.json');byid={s['id']:s for s in original['steps']};steps=[]
    for s in updates.get('steps',[]):
        if s.get('id') not in byid:raise ValueError('手順の参照が正しくありません。')
        old=copy.deepcopy(byid[s['id']])
        for k in ['title','text','target','value','uncertainty']:
            if k in s:
                if not isinstance(s[k],str) or len(s[k])>5000:raise ValueError('編集内容が長すぎます。')
                old[k]=s[k]
        steps.append(old)
    if len(steps)!=len(original['steps']) or [s['id'] for s in steps]!=[s['id'] for s in original['steps']]:raise ValueError('根拠との対応を保つため、この画面では手順の追加・並べ替えはできません。')
    title=updates.get('title',original['title'])
    if not isinstance(title,str) or not title.strip() or len(title)>250:raise ValueError('題名は1〜250文字で入力してください。')
    original.update(title=title.strip(),steps=steps,edited=True);write(p/'manual.json',original);return view(jid)

def stamp(t):
    minute=int(t//60);second=t-minute*60
    return (f'{minute//60}:{minute%60:02d}:{second:04.1f}' if minute>=60 else f'{minute:02d}:{second:04.1f}')
def html_manual(jid):
    p=jobdir(jid);m=read(p/'manual.json');esc=html.escape;parts=[]
    for n,s in enumerate(m['steps'],1):
        pictures=''.join(f'<figure><button class="picture" data-time="{im["time"]}"><img src="images/{im["id"]}.jpg" alt="根拠画像 {im["id"]}"></button><figcaption>{im["id"]}・<button data-time="{im["time"]}">{stamp(im["time"])}</button></figcaption></figure>' for im in s['images'])
        parts.append(f'<section id="step-{n}"><h2>{n}. {esc(s["title"])}</h2><button class="jump" data-time="{s["start"]}">▶ 動画で確認 {stamp(s["start"])}〜{stamp(s["end"])}</button><p>{esc(s["text"])}</p><dl><dt>操作対象</dt><dd>{esc(s["target"])}</dd><dt>値</dt><dd>{esc(s["value"])}</dd></dl>'+ ('<p class="note">画面の変化から推定しています。</p>' if s['inferred'] else '')+(f'<p class="note">要確認：{esc(s["uncertainty"])}</p>' if s['uncertainty'] else '')+pictures+'</section>')
    questions=''.join('<li>'+esc(q)+'</li>' for q in m['questions'])
    return '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(m['title'])+'</title><style>body{margin:0;font:16px/1.8 system-ui;color:#203047;background:#f5f7fa}header,main{max-width:1360px;margin:auto;padding:24px}main{display:grid;grid-template-columns:1fr 1fr;gap:28px}.player{position:sticky;top:20px;align-self:start}video{width:100%;background:#15243a;border-radius:12px}section{background:white;padding:24px;margin-bottom:22px;border-radius:12px}img{width:100%}figure{margin:18px 0}button{cursor:pointer;color:#176a61;background:#e6f4ef;border:0;border-radius:7px;padding:9px 12px;font:inherit}.picture{padding:0;width:100%}h2{font-size:21px}.note{color:#775e27}dt{font-weight:bold}dd{margin:0 0 8px}.notice{background:#fff3d3;padding:14px;border-radius:8px}@media(max-width:800px){main{display:block}.player{position:relative;top:0;margin-bottom:24px}}@media print{.player,button{display:none}main{display:block}}</style><header><h1>'+esc(m['title'])+'</h1><p class="notice">録画を根拠にした下書きです。作業責任者の承認前です。時刻は根拠画像の範囲で、クリックした瞬間を特定するものではありません。</p></header><main><aside class="player"><video id="video" controls preload="metadata" src="video.mp4"></video><p id="position">手順の時刻や画像を押すと、動画の該当位置へ移動します。</p></aside><article>'+''.join(parts)+'<section><h2>未確認事項</h2><ul>'+questions+'</ul></section></article></main><script>const v=document.getElementById("video");function seek(t){const go=()=>{v.currentTime=t;v.play().catch(()=>{});document.getElementById("position").textContent="参照位置："+t.toFixed(1)+"秒";};if(v.readyState)go();else v.addEventListener("loadedmetadata",go,{once:true});}document.addEventListener("click",e=>{const b=e.target.closest("[data-time]");if(b)seek(Number(b.dataset.time));});</script></html>'
def export_zip(jid,destination=None):
    p=jobdir(jid);m=read(p/'manual.json');frames={f['image_id']:f for f in read(p/'manifest.json')['frames']};buffer=io.BytesIO() if destination is None else destination
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_STORED,allowZip64=True) as z:
        z.writestr('manual.html',html_manual(jid));z.write(p/'video.mp4','video.mp4');z.writestr('manual.json',json.dumps(m,ensure_ascii=False,indent=2))
        for fid in dict.fromkeys(im['id'] for s in m['steps'] for im in s['images']):z.write(p/frames[fid]['path'],'images/'+fid+'.jpg')
        z.writestr('はじめに.txt','すべて展開してmanual.htmlをブラウザで開いてください。各手順の「動画で確認」や根拠画像を押すと、同梱動画の該当位置を再生します。フォルダ内のvideo.mp4とimagesを一緒に保管してください。作業責任者の承認前の下書きです。')
    return buffer.getvalue() if destination is None else destination
def export_word(jid):
    from docx import Document
    from docx.shared import Inches
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    p=jobdir(jid);m=read(p/'manual.json');frames={f['image_id']:f for f in read(p/'manifest.json')['frames']};doc=Document();doc.add_heading(m['title'],0)
    doc.add_paragraph('録画を根拠にした下書きです。作業責任者の承認前です。動画へのリンクはこのPCでアプリが起動している間に利用できます。他のPCへ渡す場合はHTML＋動画ZIPをご利用ください。')
    for n,s in enumerate(m['steps'],1):
        doc.add_heading(f'{n}. {s["title"]}',1);doc.add_paragraph(s['text']);doc.add_paragraph('操作対象：'+s['target']+'\n値：'+s['value'])
        para=doc.add_paragraph();url=ORIGIN+'/?job='+jid+'&time='+str(s['start'])+'&step='+s['id'];rid=para.part.relate_to(url,RT.HYPERLINK,is_external=True);link=OxmlElement('w:hyperlink');link.set(qn('r:id'),rid);run=OxmlElement('w:r');props=OxmlElement('w:rPr');color=OxmlElement('w:color');color.set(qn('w:val'),'176A61');props.append(color);under=OxmlElement('w:u');under.set(qn('w:val'),'single');props.append(under);run.append(props);text=OxmlElement('w:t');text.text='動画で確認 '+stamp(s['start'])+'〜'+stamp(s['end']);run.append(text);link.append(run);para._p.append(link)
        if s['uncertainty']:doc.add_paragraph('要確認：'+s['uncertainty'])
        for im in s['images']:doc.add_picture(str(p/frames[im['id']]['path']),width=Inches(6));doc.add_paragraph(im['id']+'・元動画 '+stamp(im['time']))
    doc.add_heading('未確認事項',1)
    for q in m['questions']:doc.add_paragraph(q)
    b=io.BytesIO();doc.save(b);return b.getvalue()

class Handler(BaseHTTPRequestHandler):
    server_version='VideoManualLocal/1.0'
    def log_message(self,fmt,*args):pass
    def check_origin(self,mutating=False):
        host=self.headers.get('Host','')
        if host not in {urlparse(ORIGIN).netloc,'localhost:'+str(self.server.server_port)}:raise PermissionError('許可されていない接続元です。')
        origin=self.headers.get('Origin')
        if origin and origin not in {ORIGIN,ORIGIN.replace('127.0.0.1','localhost')}:raise PermissionError('許可されていない接続元です。')
        if mutating and self.headers.get('X-App-Token')!=TOKEN:raise PermissionError('画面を再読み込みしてください。')
    def send(self,code,bb,ctype='application/json; charset=utf-8',filename=None):
        self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(bb)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Cache-Control','no-store')
        if filename:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(filename))
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(bb)
    def json(self,x,status=200):self.send(status,json.dumps(x,ensure_ascii=False).encode())
    def file(self,p,ctype,filename=None):
        size=p.stat().st_size;start=0;end=size-1;code=200;value=self.headers.get('Range')
        if value:
            m=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
            if not m or not (m[1] or m[2]):self.send(416,b'','text/plain');return
            if m[1]:start=int(m[1]);end=min(int(m[2]),end) if m[2] else end
            else:start=max(0,size-int(m[2]))
            if start>end or start>=size:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.send_header('Content-Length','0');self.end_headers();return
            code=206
        self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes');self.send_header('X-Content-Type-Options','nosniff')
        if code==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        if filename:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(filename))
        self.end_headers()
        if self.command=='HEAD':return
        with p.open('rb') as f:
            f.seek(start);left=end-start+1
            while left:
                b=f.read(min(65536,left))
                if not b:break
                self.wfile.write(b);left-=len(b)
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        try:
            self.check_origin();u=urlparse(self.path);path=u.path
            if path=='/':self.send(200,(ROOT/'static/index.html').read_bytes(),'text/html; charset=utf-8');return
            if path in ['/app.js','/style.css']:
                p=ROOT/'static'/path[1:];self.send(200,p.read_bytes(),'text/javascript; charset=utf-8' if p.suffix=='.js' else 'text/css; charset=utf-8');return
            if path=='/api/status':
                jobs=sorted([read(p) for p in DATA.glob('*/job.json')],key=lambda x:x['updated'],reverse=True)
                self.json({'token':TOKEN,'api_available':bool(os.environ.get('OPENAI_API_KEY')),'budget':BUDGET.public(),'jobs':[{k:j[k] for k in ['id','name','phase','updated','sample']} for j in jobs[:30]]});return
            match=re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(video|frame|export))?',path)
            if match:
                jid,kind=match.groups();p=jobdir(jid);j=load_job(jid);q=parse_qs(u.query)
                if not kind:self.json(view(jid));return
                if kind=='video':self.file(p/j['video_file'],'video/mp4');return
                if kind=='frame':
                    fid=q.get('id',[''])[0];f=next((f for f in read(p/'manifest.json')['frames'] if f['image_id']==fid),None)
                    if not f:raise FileNotFoundError('根拠画像が見つかりません。')
                    self.file(p/f['path'],'image/jpeg');return
                if j['phase']!='ready':raise ValueError('手順書が完成してからダウンロードできます。')
                if q.get('format',['zip'])[0]=='docx':self.send(200,export_word(jid),'application/vnd.openxmlformats-officedocument.wordprocessingml.document','手順書.docx')
                else:
                    export_path=p/('export-'+uuid.uuid4().hex+'.zip')
                    try:export_zip(jid,export_path);self.file(export_path,'application/zip','手順書と動画.zip')
                    finally:export_path.unlink(missing_ok=True)
                return
            self.send(404,b'Not found','text/plain')
        except PermissionError as e:self.json({'error':str(e)},403)
        except (ValueError,FileNotFoundError) as e:self.json({'error':str(e)},400)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        except Exception:self.json({'error':'ファイルを読み込めませんでした。'},500)
    def do_POST(self):
        try:
            self.check_origin(True);u=urlparse(self.path);path=u.path;n=int(self.headers.get('Content-Length','0'))
            if n<0:raise ValueError('ファイルサイズが不正です。')
            if path=='/api/upload':
                if WORK.locked():raise ValueError('別の動画を処理中です。完了してから読み込んでください。')
                q=parse_qs(u.query);name=q.get('name',['動画.mp4'])[0];ext=Path(name).suffix.lower();interval=float(q.get('interval',['.5'])[0])
                if ext not in {'.mp4','.webm','.mov','.mkv','.avi','.m4v'}:raise ValueError('MP4、WebMなどの動画ファイルを選択してください。')
                if interval not in {.5,1,2,5,10,30}:raise ValueError('抽出間隔が正しくありません。')
                if not n:raise ValueError('空のファイルは利用できません。')
                if n>shutil.disk_usage(DATA).free-16*1024*1024:raise ValueError('動画を保存するディスクの空き容量が不足しています。')
                jid=new_job(Path(name).name,interval);p=jobdir(jid);filename='source'+ext
                with (p/filename).open('wb') as f:
                    left=n
                    while left:
                        bb=self.rfile.read(min(left,1048576))
                        if not bb:raise ValueError('動画の受信が中断されました。')
                        f.write(bb);left-=len(bb)
                change(jid,source_file=filename,message='動画を準備しています。');submit(jid,prepare);self.json({'id':jid},201);return
            if n>1024*1024:raise ValueError('要求が大きすぎます。')
            body=json.loads(self.rfile.read(n) or b'{}')
            if path=='/api/sample':self.json({'id':sample_job()},201);return
            match=re.fullmatch(r'/api/jobs/([a-f0-9]{32})/(estimate|generate|save)',path)
            if not match:self.json({'error':'操作が見つかりません。'},404);return
            jid,action=match.groups();j=load_job(jid)
            if action=='save':self.json(save_manual(jid,body));return
            key()
            allowed=['prepared','awaiting_generation','error'] if action=='estimate' else ['awaiting_generation']
            if action=='estimate' and not (jobdir(jid)/'manifest.json').exists():raise ValueError('動画の準備が完了していません。もう一度読み込んでください。')
            if j['phase'] not in allowed:raise ValueError('処理状態が変わりました。画面を再読み込みしてください。')
            submit(jid,estimate if action=='estimate' else generate);self.json({'id':jid})
        except PermissionError as e:self.json({'error':str(e)},403)
        except (ValueError,FileNotFoundError) as e:self.json({'error':str(e)},400)
        except Exception:self.json({'error':'処理を開始できませんでした。'},500)

def main():
    global ORIGIN
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args();ORIGIN=f'http://127.0.0.1:{args.port}'
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print('Video Manual: '+ORIGIN,flush=True)
    if not args.no_browser:threading.Timer(.5,lambda:webbrowser.open(ORIGIN)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
if __name__=='__main__':main()
