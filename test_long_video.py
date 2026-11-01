"""Long-video contracts. Model responses are mocked; no external API requests."""
import copy,io,json,os,threading,unittest
from pathlib import Path
from unittest.mock import patch
import test_app
import server as app
from segments import plan_segments,merge_segments

def observation(frame,event='E01'):
    return {'title':'録画で確認した状態','observations':[{'id':'O01','image_id':frame,'visible_state':'画面の値が変わった'}],
            'events':[{'id':event,'title':'値の表示','target':'入力欄','observed_change':'値が表示された','value':'7','observation_ids':['O01'],'anchor_observation_id':'O01','status':'observed_state','uncertainty':''}],'questions':[]}

class LongVideoTests(unittest.TestCase):
    setUp=test_app.AppTests.setUp
    def long_job(self,n=121):
        p=self.p;original=app.read(p/'manifest.json')['frames'][0]
        frames=[{**original,'image_id':f'F{i:03d}','timestamp_seconds':i*5.0} for i in range(n)]
        app.write(p/'manifest.json',{'frames':frames,'duration_seconds':n*5,'interval_seconds':5})
        app.change(self.jid,phase='prepared',frames_count=n)
        return frames
    def prepare_estimate(self):
        with patch.object(app,'api_post',return_value={'input_tokens':1000}) as api:app.estimate(self.jid)
        return app.read(self.p/'estimate.json')
    def response(self,body):
        images=body['response_format']['json_schema']['schema']['$defs']['Observation']['properties']['image_id']['enum']
        obj=observation(images[-1])
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(obj)}}],
                'usage':{'prompt_tokens':1000,'completion_tokens':100,'prompt_tokens_details':{'cached_tokens':0,'audio_tokens':0}}}
    def test_all_121_frames_covered_with_bounded_context(self):
        frames=self.long_job();plans=plan_segments(frames)
        self.assertEqual([x for p in plans for x in p['owned_image_ids']],[f['image_id'] for f in frames])
        self.assertLessEqual(max(len(p['image_ids']) for p in plans),42)
        self.assertEqual(plans[1]['image_ids'][:2],['F038','F039'])
    def test_multisegment_output_ids_and_late_video_links(self):
        self.long_job();est=self.prepare_estimate()
        with patch.object(app,'api_post',side_effect=lambda _,body:self.response(body)) as api:app.generate(self.jid)
        self.assertEqual(api.call_count,4)
        m=app.read(self.p/'manual.json');self.assertEqual(app.load_job(self.jid)['phase'],'ready')
        self.assertEqual([s['start'] for s in m['steps']],[195,395,595,600])
        self.assertEqual(len(set(s['id'] for s in m['steps'])),4)
        self.assertIn('data-time="600.0"',app.html_manual(self.jid))
    def test_completed_segment_reused_after_budget_pause(self):
        self.long_job(81);est=self.prepare_estimate();first=est['segments'][0]
        folder=self.p/'segments'/first['id'];app.write(folder/'response.json',self.response(app.request_body(self.jid,first)));app.write(folder/'settled.json',{'cost_usd':.0015,'body_sha256':first['body_sha256']})
        self.prepare_estimate()
        with patch.object(app,'api_post',side_effect=lambda _,body:self.response(body)) as api:app.generate(self.jid)
        self.assertEqual(api.call_count,2);self.assertEqual(app.load_job(self.jid)['phase'],'ready')
    def test_uncertain_segment_is_not_automatically_retried(self):
        self.long_job(81);self.prepare_estimate()
        with patch.object(app,'api_post',side_effect=ValueError('uncertain')) as api:
            with self.assertRaises(ValueError):app.generate(self.jid)
            with self.assertRaises(ValueError):app.generate(self.jid)
        self.assertEqual(api.call_count,1);self.assertTrue(app.BUDGET.public()['unresolved'])
    def test_cross_segment_duplicate_context_withholds_document(self):
        frames=self.long_job(81);plans=plan_segments(frames)
        parts=[(plans[0],observation('F039')),(plans[1],observation('F039'))]
        with self.assertRaisesRegex(ValueError,'区間境界'):merge_segments(parts,frames)
    def test_oversize_context_is_split_before_generation(self):
        self.long_job(81)
        def count(_,body,*args):
            images=[x for m in body['input'] if isinstance(m['content'],list) for x in m['content'] if x['type']=='input_image']
            return {'input_tokens':300000 if len(images)>24 else 1000}
        with patch.object(app,'api_post',side_effect=count):app.estimate(self.jid)
        est=app.read(self.p/'estimate.json');self.assertEqual(len(est['segments']),5)
        self.assertTrue(all(len(x['image_ids'])<=24 for x in est['segments']))
    def test_export_writes_to_disk_without_reading_video_into_memory(self):
        out=self.p/'test-export.zip';result=app.export_zip(self.jid,out)
        self.assertEqual(result,out);self.assertTrue(out.stat().st_size>1000)
    def test_tampering_in_final_segment_stops_before_first_paid_request(self):
        frames=self.long_job(81);last=self.p/'last.jpg';last.write_bytes((self.p/frames[-1]['path']).read_bytes())
        frames[-1]['path']='last.jpg';app.write(self.p/'manifest.json',{'frames':frames,'duration_seconds':405,'interval_seconds':5})
        self.prepare_estimate();last.write_bytes(b'changed')
        with patch.object(app,'api_post') as api:
            with self.assertRaises(ValueError):app.generate(self.jid)
        api.assert_not_called();self.assertFalse(app.BUDGET.public()['unresolved'])
    def test_total_budget_checked_before_first_segment(self):
        self.long_job(81);self.prepare_estimate()
        with patch.dict(os.environ,{'VIDEO_MANUAL_BUDGET_USD':'.01'}),patch.object(app,'api_post') as api:
            with self.assertRaisesRegex(ValueError,'累計予算'):app.generate(self.jid)
        api.assert_not_called();self.assertFalse(app.BUDGET.public()['unresolved'])

if __name__=='__main__':unittest.main(verbosity=2)
