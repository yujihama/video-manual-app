"""Offline contract tests; saved inference replay, never a paid model request."""
import unittest,tempfile,copy,json,io,zipfile,os
from pathlib import Path
from unittest.mock import patch
test_output=Path(__file__).resolve().parent/'tests-output';test_output.mkdir(exist_ok=True)
os.environ['VIDEO_MANUAL_DATA_DIR']=tempfile.mkdtemp(prefix='unit-',dir=test_output)
import server as app

class AppTests(unittest.TestCase):
    def setUp(self):
        app.BUDGET=app.Budget(app.DATA/(os.urandom(6).hex()+'.json'));app.ORIGIN='http://127.0.0.1:8765';self.jid=app.sample_job();self.p=app.jobdir(self.jid)
    def body_and_estimate(self):
        b=app.request_body(self.jid);app.write(self.p/'estimate.json',{'upper_usd':.20,'max_input_tokens':80000,'body_sha256':app.digest(json.dumps(b,sort_keys=True).encode())});return b
    def test_saved_inference_replay_uses_real_gate_and_accounts_usage(self):
        self.body_and_estimate();payload={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(app.read(self.p/'extraction.json'),ensure_ascii=False)}}],'usage':{'prompt_tokens':56861,'completion_tokens':2578,'prompt_tokens_details':{'cached_tokens':0,'audio_tokens':0}}}
        with patch.object(app,'api_post',return_value=payload) as network:app.generate(self.jid)
        self.assertEqual(network.call_count,1);self.assertEqual(app.load_job(self.jid)['phase'],'ready');self.assertAlmostEqual(app.BUDGET.public()['spent_usd'],.069751);self.assertEqual(len(app.view(self.jid)['manual']['steps']),9)
    def test_uncertain_request_keeps_budget_and_prevents_second_call(self):
        self.body_and_estimate()
        with patch.object(app,'api_post',side_effect=ValueError('uncertain')) as network:
            with self.assertRaises(ValueError):app.generate(self.jid)
            with self.assertRaises(ValueError):app.generate(self.jid)
        self.assertEqual(network.call_count,1);self.assertTrue(app.BUDGET.public()['unresolved'])
    def test_chronology_error_withholds_whole_document(self):
        obj=app.read(self.p/'extraction.json');obj['events'][-1]['observation_ids']=['O006'];obj['events'][-1]['anchor_observation_id']='O006';app.apply_extraction(self.jid,obj)
        self.assertEqual(app.load_job(self.jid)['phase'],'blocked');self.assertEqual(app.read(self.p/'gate.json')['held_events'],9)
    def test_edits_keep_evidence_and_escape_html(self):
        before=app.read(self.p/'manual.json');edit=copy.deepcopy(before);edit['title']='<img src=x onerror=alert(1)>';edit['steps'][0]['start']=999;edit['steps'][0]['images']=[];edit['steps'][0]['text']='<script>alert(1)</script>'
        app.save_manual(self.jid,edit);after=app.read(self.p/'manual.json');self.assertEqual(before['steps'][0]['start'],after['steps'][0]['start']);self.assertEqual(before['steps'][0]['images'],after['steps'][0]['images']);out=app.html_manual(self.jid);self.assertNotIn('<script>alert(1)</script>',out);self.assertIn('&lt;script&gt;',out)
    def test_portable_html_zip_and_word_hyperlinks_have_matching_times(self):
        m=app.read(self.p/'manual.json');z=zipfile.ZipFile(io.BytesIO(app.export_zip(self.jid)));self.assertIn('video.mp4',z.namelist());self.assertIn('images/F039.jpg',z.namelist());self.assertIn('data-time="17.0"',z.read('manual.html').decode())
        word=zipfile.ZipFile(io.BytesIO(app.export_word(self.jid)));rels=word.read('word/_rels/document.xml.rels').decode();self.assertIn('time=17.0',rels);self.assertIn('job='+self.jid,rels);self.assertIn('http://127.0.0.1:8765/',rels)
    def test_changed_source_image_is_rejected_before_model_request(self):
        self.body_and_estimate();f=app.read(self.p/'manifest.json')['frames'][0];(self.p/f['path']).write_bytes(b'changed')
        with patch.object(app,'api_post') as network:
            with self.assertRaises(ValueError):app.generate(self.jid)
        network.assert_not_called()
if __name__=='__main__':unittest.main(verbosity=2)
