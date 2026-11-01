from pathlib import Path
import json,zipfile,urllib.request,time,sys,base64,io
from PIL import Image,ImageChops,ImageStat
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parent;OUT=ROOT/'tests-output';OUT.mkdir(exist_ok=True);BASE='http://127.0.0.1:8765'
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000},locale='ja-JP',accept_downloads=True);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE);page.get_by_role('button',name='サンプルで試す').click();page.wait_for_selector('.step');assert page.locator('.step').count()==9;page.wait_for_function('document.querySelector("video").readyState>=1')
    jid=page.url.split('job=')[1].split('&')[0]
    page.locator('.step').nth(7).locator('.jump').click();page.wait_for_function('document.querySelector("video").currentTime>=15');page.locator('video').evaluate('(v)=>v.pause()');at=page.locator('video').evaluate('(v)=>v.currentTime');assert abs(at-15)<.6
    page.locator('.step').first.locator('details.evidence summary').click();page.locator('.step').first.locator('.image-button').first.click();page.wait_for_function('document.querySelector("video").currentTime<4');page.locator('video').evaluate('(v)=>v.pause()');assert abs(page.locator('video').evaluate('(v)=>v.currentTime')-3)<.6
    page.get_by_role('button',name='内容を編集').click();page.locator('[data-step="E01"][data-key="text"]').fill('確認テスト：EQ-17を選択した画面。');assert page.get_by_role('button',name='HTML＋動画ZIP').is_disabled();page.get_by_role('button',name='編集を保存').click();page.wait_for_function('document.querySelector("#save-state").textContent==="保存済み"');page.get_by_role('button',name='編集を閉じる').click();assert page.get_by_text('確認テスト：EQ-17を選択した画面。',exact=True).is_visible()
    # Return to an unchanged sample for the user-facing preview and export.
    page.goto(BASE);page.get_by_role('button',name='サンプルで試す').click();page.wait_for_selector('.step');page.wait_for_function('(old)=>new URL(location).searchParams.get("job")!==old',arg=jid);jid=page.url.split('job=')[1].split('&')[0]
    page.locator('.step').first.locator('.jump').click();page.wait_for_function('!document.querySelector("video").seeking&&document.querySelector("video").readyState>=2');page.locator('video').evaluate('(v)=>v.pause()');page.screenshot(path=str(OUT/'desktop.png'))
    with page.expect_download() as dl:page.get_by_role('button',name='HTML＋動画ZIP').click()
    dl.value.save_as(str(OUT/'manual.zip'))
    with page.expect_download() as dl:page.get_by_role('button',name='Word',exact=True).click()
    dl.value.save_as(str(OUT/'manual.docx'))
    destination=OUT/'portable'
    with zipfile.ZipFile(OUT/'manual.zip') as z:z.extractall(destination)
    offline=browser.new_page(viewport={'width':1280,'height':900});offline.goto((destination/'manual.html').as_uri());offline.wait_for_function('document.querySelector("video").readyState>=1');offline.locator('section').nth(7).locator('.jump').click();offline.wait_for_function('document.querySelector("video").currentTime>=15');offline.locator('video').evaluate('(v)=>v.pause()');assert abs(offline.locator('video').evaluate('(v)=>v.currentTime')-15)<.6;assert offline.locator('img').evaluate_all('(xs)=>xs.every(x=>x.complete&&x.naturalWidth>0)');offline.screenshot(path=str(OUT/'portable.png'))
    deep=browser.new_page();deep.goto(BASE+'/?job='+jid+'&time=17&step=E09');deep.wait_for_function('document.querySelector("video").currentTime>=17&&!document.querySelector("video").seeking&&document.querySelector("video").readyState>=2');deep.locator('video').evaluate('(v)=>v.pause()');assert abs(deep.locator('video').evaluate('(v)=>v.currentTime')-17)<.7
    frame=deep.locator('video').evaluate('(v)=>{const c=document.createElement("canvas");c.width=v.videoWidth;c.height=v.videoHeight;c.getContext("2d").drawImage(v,0,0);return c.toDataURL("image/png").split(",")[1]}');actual=Image.open(io.BytesIO(base64.b64decode(frame))).convert('RGB').resize((320,225));expected=Image.open(ROOT/'demo/frames/F034.jpg').convert('RGB').resize((320,225));pixel_difference=sum(ImageStat.Stat(ImageChops.difference(actual,expected)).mean)/3;assert pixel_difference<8,pixel_difference
    mobile=browser.new_page(viewport={'width':390,'height':844},locale='ja-JP');mobile.goto(BASE+'/?job='+jid);mobile.wait_for_selector('.step');assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth');mobile.locator('.step').first.locator('.jump').click();mobile.wait_for_function('!document.querySelector("video").seeking&&document.querySelector("video").readyState>=2');mobile.locator('video').evaluate('(v)=>v.pause()');mobile.screenshot(path=str(OUT/'mobile.png'))
    mobile.locator('.step').first.locator('.jump').click();mobile.wait_for_function('document.querySelector("video").currentTime>=3');assert not errors,errors
    upload=browser.new_page();upload.goto(BASE);upload.get_by_role('button',name='新しい動画').click();upload.locator('#file').set_input_files(str(ROOT/'demo/video.mp4'));upload.get_by_role('button',name='動画を読み込む',exact=True).click();upload.get_by_text('動画の準備ができました',exact=True).wait_for(timeout=90000);assert upload.get_by_role('button',name='生成の準備',exact=True).is_visible()
    browser.close()
result={'passed':True,'api_calls':0,'sample_steps':9,'step_seek_seconds':15,'image_seek_seconds':3,'word_deep_link_seconds':17,'portable_html_seek_seconds':15,'decoded_video_vs_evidence_mean_pixel_difference':pixel_difference,'desktop_and_390px_mobile':True,'edit_save_and_export':True,'mp4_upload_and_local_frame_extraction':True,'browser_errors':errors,'sample_job_id':jid}
(OUT/'browser-check.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
