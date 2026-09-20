import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from PIL import Image
from fastapi.testclient import TestClient
from carbonshift.api import app, get_store
from carbonshift import assets
from carbonshift.credentials import configure, read_api_key
from carbonshift.demo import queue_demo_request
from carbonshift.docker_runner import DockerRunner, Container
from carbonshift.models import SubmitRequest
from carbonshift.store import Store
from carbonshift.worker import Worker

PROJECT=Path(__file__).resolve().parents[1]


def image_bytes(size=(320,160), color=(255,0,0,0)):
    data=io.BytesIO();Image.new('RGBA',size,color).save(data,format='PNG');return data.getvalue()


class ImageBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'CARBONSHIFT_ASSETS':str(self.root/'assets'),'CARBONSHIFT_API_KEY_FILE':str(self.root/'private/key'),'ELECTRICITYMAPS_API_KEY':''})
        self.env.start();self.store=Store(self.root/'jobs.sqlite3')
        app.dependency_overrides[get_store]=lambda:self.store
        self.client=TestClient(app,base_url='http://localhost')
    def tearDown(self):
        app.dependency_overrides.clear();self.env.stop();self.tmp.cleanup()
    def upload(self,n=2):
        encoded=base64.b64encode(image_bytes()).decode()
        response=self.client.post('/api/batches',json={'files':[{'name':f'../<photo-{i}>.png','content':encoded} for i in range(n)]})
        self.assertEqual(response.status_code,200,response.text);return response.json()
    def preview(self,batch=None,timing='recommended',start=None):
        batch=batch or self.upload();start=start or datetime.now(timezone.utc)+timedelta(minutes=2)
        data={'plan':{'name':'test-image-job','earliest_start':start.isoformat(),'deadline':(start+timedelta(minutes=10)).isoformat(),'duration_minutes':2,'scenario':'no-solar'},'batch':{'batch_id':batch['batch_id'],'manifest_sha256':batch['manifest_sha256'],'max_edge':128,'quality':80},'timing':timing}
        response=self.client.post('/api/batches/preview',json=data)
        self.assertEqual(response.status_code,200,response.text);return response.json()
    def queue(self,preview):
        response=self.client.post('/api/batches/queue/'+preview['preview_id'])
        self.assertEqual(response.status_code,200,response.text);return response.json()
    def process(self,job):
        spec=job['snapshot']['execution']['image_batch'];directory,_=assets.validate_bundle(spec);output=assets.output_directory(job['id'])
        result=subprocess.run([sys.executable,str(PROJECT/'workloads/images.py'),'--input',str(directory),'--output',str(output),'--max-edge',str(spec['max_edge']),'--quality',str(spec['quality']),'--manifest-sha256',spec['manifest_sha256'],'--timeout','60'],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr);return result.stdout
    def finish(self,job,logs):
        self.store.claim(job['scheduled_start'])
        runner=type('Runner',(),{'logs':lambda _,cid:logs})()
        c=Container('test-container','exited',{},job['scheduled_start']+1,job['scheduled_start']+3,0)
        Worker(self.store,runner).finish(self.store.get(job['id']),c,job['scheduled_start']+4)
        return self.store.get(job['id'])

    def test_real_transform_queue_finish_download_and_reopen(self):
        preview=self.preview();job=self.queue(preview)
        self.assertEqual(job['snapshot'],preview['submission'])
        self.assertEqual(job['recommendation'],preview['result'])
        logs=self.process(job);done=self.finish(job,logs)
        self.assertEqual(done['state'],'SUCCEEDED')
        response=self.client.get('/api/jobs/'+job['id']+'/results');self.assertEqual(response.status_code,200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(set(archive.namelist()),{'001.jpg','002.jpg','report.json'})
            with Image.open(io.BytesIO(archive.read('001.jpg'))) as im:
                self.assertEqual(im.size,(128,64));self.assertEqual(im.mode,'RGB')
                self.assertEqual(im.getpixel((10,10)),(255,255,255));self.assertFalse(im.getexif())
        self.assertEqual(Store(self.store.path).get(job['id'])['evidence'],done['evidence'])
        self.assertEqual(self.client.get('/api/jobs/'+job['id']).json()['progress'],{'processed':2,'total':2})

    def test_duplicate_queue_uses_same_job(self):
        preview=self.preview();a=self.queue(preview);b=self.queue(preview)
        self.assertEqual(a['id'],b['id']);self.assertEqual(len(self.store.list()),1)

    def test_changed_planner_result_requires_fresh_preview(self):
        preview=self.preview()
        path=assets.identified('previews',preview['preview_id'])/'plan.json'
        path.chmod(0o600)
        preview['result']['candidate_count']+=1
        path.write_text(json.dumps(preview))
        response=self.client.post('/api/batches/queue/'+preview['preview_id'])
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.store.list(),[])

    def test_changed_files_refused_before_queue(self):
        batch=self.upload();preview=self.preview(batch)
        p=assets.identified('inputs',batch['batch_id'])/'001.image';p.chmod(0o644);p.write_bytes(b'changed')
        response=self.client.post('/api/batches/queue/'+preview['preview_id']);self.assertEqual(response.status_code,422)
        self.assertEqual(self.store.list(),[])

    def test_expired_preview_cannot_queue(self):
        preview=self.preview()
        with patch('carbonshift.store.time.time',return_value=time.time()+3600):
            response=self.client.post('/api/batches/queue/'+preview['preview_id'])
        self.assertEqual(response.status_code,409)

    def test_bad_uploads_and_pixel_limit(self):
        for data in [{'files':[]},{'files':[{'name':'x','content':'invalid%%%'}]},{'files':[{'name':'x','content':base64.b64encode(b'not an image').decode()}]}]:
            self.assertEqual(self.client.post('/api/batches',json=data).status_code,422)
        giant=base64.b64encode(image_bytes((3000,3000))).decode()
        self.assertEqual(self.client.post('/api/batches',json={'files':[{'name':'x.png','content':giant}]}).status_code,422)
        self.assertEqual(self.store.list(),[])

    def test_body_limit_and_origin(self):
        with patch('carbonshift.assets.MAX_BODY',32):
            self.assertEqual(self.client.post('/api/batches',content=b'x'*33).status_code,413)
        self.assertEqual(self.client.post('/api/batches',headers={'Origin':'https://unrelated.example'},json={}).status_code,403)

    def test_missing_and_unfinished_results_refused(self):
        self.assertEqual(self.client.get('/api/jobs/unknown/results').status_code,404)
        job=self.queue(self.preview());self.assertEqual(self.client.get('/api/jobs/'+job['id']+'/results').status_code,409)
        done=self.finish(job,'');self.assertEqual(done['state'],'FAILED');self.assertIn('result validation failed',done['error'])

    def test_modified_result_cannot_download(self):
        job=self.queue(self.preview());self.finish(job,self.process(job));assets.result_path(job['id']).write_bytes(b'altered')
        self.assertEqual(self.client.get('/api/jobs/'+job['id']+'/results').status_code,409)

    def test_invalid_archive_fails_job_without_crashing_worker(self):
        job=self.queue(self.preview());(assets.output_directory(job['id'])/'results.zip').write_bytes(b'not a zip')
        self.assertEqual(self.finish(job,'')['state'],'FAILED')

    def test_safe_names_no_paths_and_readonly_inputs(self):
        batch=self.upload();self.assertEqual(batch['files'][0]['original_name'],'<photo-0>.png')
        directory=assets.identified('inputs',batch['batch_id'])
        self.assertEqual(set(p.name for p in directory.iterdir()),{'001.image','002.image','manifest.json'})
        self.assertEqual((directory/'001.image').stat().st_mode&0o222,0)
        with self.assertRaises(ValueError):assets.identified('inputs','../x')
        target=assets.identified('inputs','a'*32);target.symlink_to(directory,target_is_directory=True)
        with self.assertRaises(ValueError):assets.identified('inputs','a'*32)

    def test_docker_mounts_and_limits(self):
        job=self.queue(self.preview());runner=DockerRunner()
        with patch.object(runner,'command',return_value='container') as command:
            runner.create(job,self.store.queue_id)
        args=command.call_args.args[0]
        self.assertIn('none',args);self.assertIn('--read-only',args);self.assertIn('512m',args);self.assertIn('compress=false',args)
        self.assertIn('65534:65534',args);self.assertIn('carbonshift-workload:0.4',args)
        self.assertTrue(any('dst=/input,readonly' in arg for arg in args));self.assertTrue(any('dst=/output' in arg for arg in args))
        self.assertNotIn('ELECTRICITYMAPS_API_KEY',' '.join(args))

    def test_custom_window_validation_and_earliest_choice(self):
        preview=self.preview(timing='earliest')
        self.assertEqual(preview['result']['recommended']['start'],preview['submission']['optimization']['job']['earliest_start'].replace('Z','+00:00'))
        start=datetime.now(timezone.utc)+timedelta(minutes=2)
        response=self.client.post('/api/planner',json={'earliest_start':start.isoformat(),'deadline':(start+timedelta(hours=49)).isoformat()})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.post('/api/planner',json={'earliest_start':start.isoformat()}).status_code,422)

    def test_old_checksum_idempotency_hash_preserved(self):
        request=queue_demo_request(30,5);legacy=request.model_dump(mode='json');legacy['execution'].pop('image_batch')
        expected=hashlib.sha256(json.dumps({k:v for k,v in legacy.items() if k!='idempotency_key'},sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        self.assertEqual(self.store.submit(request)['request_hash'],expected)

    def test_progress_ignores_invalid_or_out_of_range(self):
        logs='oops\n'+json.dumps({'event':'progress','processed':1,'total':2})+'\n'+json.dumps({'event':'progress','processed':999,'total':2})
        self.assertEqual(assets.progress_from_logs(logs),{'processed':1,'total':2})

    def test_saved_key_permissions_env_override_and_no_disclosure(self):
        with patch('carbonshift.credentials.getpass.getpass',return_value='test-local-key'),patch('builtins.print'):
            configure()
        path=self.root/'private/key';self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(read_api_key(),'') # explicit empty environment overrides saved key
        del os.environ['ELECTRICITYMAPS_API_KEY']
        self.assertEqual(read_api_key(),'test-local-key')
        with patch('carbonshift.api.read_observations',return_value={'records':[]}):
            response=self.client.get('/api/carbon/observations')
        self.assertTrue(response.json()['configured']);self.assertNotIn('test-local-key',response.text)
        path.chmod(0o644)
        with self.assertRaises(ValueError):read_api_key()

    def test_running_worker_updates_progress(self):
        job=self.queue(self.preview());now=job['scheduled_start']+2
        self.store.claim(now)
        labels={'carbonshift.queue':self.store.queue_id,'carbonshift.job':job['id']}
        runner=type('Runner',(),{'inspect':lambda _,name:Container('test','running',labels,now-1),
                               'logs':lambda _,cid:'{"event":"progress","processed":1,"total":2}'})()
        Worker(self.store,runner,clock=lambda:now).tick()
        detail=self.client.get('/api/jobs/'+job['id']).json()
        self.assertEqual(detail['state'],'RUNNING');self.assertEqual(detail['progress']['processed'],1)
