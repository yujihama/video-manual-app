import httpx,json
BASE='http://127.0.0.1:8765'
with httpx.Client(trust_env=False,timeout=10) as c:
    s=c.get(BASE+'/api/status').json();jid=next(x['id'] for x in s['jobs'] if x['phase']=='ready')
    assert c.post(BASE+'/api/sample',json={}).status_code==403
    assert c.get(BASE+'/api/status',headers={'Origin':'https://example.invalid'}).status_code==403
    assert c.get(BASE+'/api/status',headers={'Host':'example.invalid'}).status_code==403
    r=c.get(BASE+f'/api/jobs/{jid}/video',headers={'Range':'bytes=4-19'});assert r.status_code==206 and len(r.content)==16
    assert r.headers['content-range'].startswith('bytes 4-19/')
    assert c.get(BASE+f'/api/jobs/{jid}/video',headers={'Range':'bytes=999999999-'}).status_code==416
    assert c.get(BASE+f'/api/jobs/{jid}/frame?id=../../server.py').status_code==400
    assert c.get(BASE+'/api/jobs/'+('a'*32)).status_code==400
    assert 'sk-' not in json.dumps(s)
print(json.dumps({'checks':8,'passed':8,'api_calls':0,'range_video_streaming':True,'origin_and_mutation_guards':True}))
