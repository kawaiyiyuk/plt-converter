import hashlib
import io
import json
import os
import unittest
import tempfile
from unittest.mock import patch
from app.pattern_layout_app import create_app


class PatternLayoutServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.policy={'PATTERN_LAYOUT_ROOT':self.temp.name,'PATTERN_LAYOUT_DISK_BUDGET_BYTES':str(256*1024*1024),
                     'PATTERN_LAYOUT_MAX_RESULT_BYTES':str(32*1024*1024),'PATTERN_LAYOUT_JOB_RETENTION_SECONDS':'60',
                     'PATTERN_LAYOUT_MAX_REQUEST_BYTES':str(20*1024*1024),'PATTERN_LAYOUT_TASK_WALL_SECONDS':'30','PATTERN_LAYOUT_TASK_MEMORY_BYTES':str(2*1024*1024*1024)}

    def test_private_service_real_worker_recognizes_and_exports(self):
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client()
            try:
                source=b'IN;SP2;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
                self.assertEqual(client.post('/internal/pattern-layout/jobs').status_code,401)
                headers={'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'test-user-1'}
                response=client.post('/internal/pattern-layout/jobs',headers=headers,data={'file':(io.BytesIO(source),'test.plt')})
                self.assertEqual(response.status_code,202)
                identifier=response.json['id']
                app.extensions['pattern_layout_jobs'][identifier].result(timeout=30)
                response=client.get('/internal/pattern-layout/jobs/'+identifier,headers=headers)
                self.assertEqual(response.json['status'],'done')
                result=response.json['result'];piece_id=result['pieces'][0]['id']
                import gzip
                resource=client.get('/internal/pattern-layout/jobs/'+identifier+'/candidates/'+piece_id,headers=headers)
                piece=json.loads(gzip.decompress(resource.data))
                project={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),'candidates':[piece],'placements':[{'piece_id':piece['id'],'x':0,'y':0}]}
                response=client.post('/internal/pattern-layout/export',headers=headers,data={'file':(io.BytesIO(source),'source.plt'),'project':json.dumps(project)})
                self.assertEqual(response.status_code,202)
                export_id=response.json['id']
                app.extensions['pattern_layout_jobs'][export_id].result(timeout=30)
                response=client.get('/internal/pattern-layout/jobs/'+export_id+'/result',headers=headers)
                self.assertEqual(response.status_code,200)
                self.assertTrue(response.data.startswith(b'IN;PA;'))
                self.assertEqual(client.delete('/internal/pattern-layout/jobs/'+identifier,headers=headers).status_code,409)
                meta=app.extensions['pattern_layout_queue'].read(identifier)
                self.assertEqual(client.post('/internal/pattern-layout/jobs/'+identifier+'/ack',headers=headers,json={'durable_complete':True,'archive_sha256':meta['archive_sha256']}).status_code,200)
                self.assertEqual(client.delete('/internal/pattern-layout/jobs/'+identifier,headers=headers).status_code,204)
                self.assertEqual(client.get('/internal/pattern-layout/jobs/'+identifier,headers=headers).status_code,404)
            finally: app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_absent_internal_key_rejects_even_with_bearer(self):
        with patch.dict(os.environ,self.policy,clear=True):
            app=create_app()
            try: self.assertEqual(app.test_client().get('/internal/pattern-layout/jobs/id',headers={'Authorization':'Bearer none'}).status_code,503)
            finally: app.extensions['pattern_layout_executor'].shutdown()

    def test_second_task_is_queued_and_executes_fifo_on_disk(self):
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client();headers={'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'test-user-1'}
            try:
                source=b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
                first=client.post('/internal/pattern-layout/jobs',headers=headers,data={'file':(io.BytesIO(source),'first.plt')})
                second=client.post('/internal/pattern-layout/jobs',headers={**headers,'X-Idempotency-Key':'second-task'},data={'file':(io.BytesIO(source),'second.plt')})
                self.assertEqual(first.status_code,202);self.assertEqual(second.status_code,202)
                self.assertEqual(second.json['status'],'queued')
                queue=app.extensions['pattern_layout_queue']
                self.assertTrue((queue.root/second.json['id']/'source.plt').is_file())
                for response in (first,second):queue.futures[response.json['id']].result(timeout=30)
                a,b=queue.read(first.json['id']),queue.read(second.json['id'])
                self.assertLessEqual(a['finished_at'],b['started_at'])
                self.assertEqual((a['status'],b['status']),('done','done'))
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_failures_and_cancellation_have_queryable_state_and_expire_by_explicit_policy(self):
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client();headers={'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'test-user-1'}
            try:
                queue=app.extensions['pattern_layout_queue']
                failed=client.post('/internal/pattern-layout/jobs',headers=headers,data={'file':(io.BytesIO(b'IN;'),'invalid.plt')})
                queue.futures[failed.json['id']].result(timeout=30)
                result=client.get('/internal/pattern-layout/jobs/'+failed.json['id'],headers=headers)
                self.assertEqual(result.json['status'],'failed');self.assertIn('error',result.json)
                source=b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
                queued=client.post('/internal/pattern-layout/jobs',headers=headers,data={'file':(io.BytesIO(source),'cancel.plt')})
                cancelled=client.delete('/internal/pattern-layout/jobs/'+queued.json['id'],headers=headers)
                self.assertIn(cancelled.status_code,(200,202))
                future=queue.futures[queued.json['id']]
                if not future.cancelled():future.result(timeout=30)
                self.assertEqual(queue.read(queued.json['id'])['status'],'cancelled')
                import time
                with patch('app.pattern_layout_app.time.time',return_value=time.time()+61):queue.reap()
                self.assertIsNone(queue.read(failed.json['id']))
                self.assertIsNone(queue.read(queued.json['id']))
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_idempotent_async_export_owner_isolation_and_complete_archive(self):
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client();headers={'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'owner-1'}
            try:
                source=b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
                submitted=client.post('/internal/pattern-layout/jobs',headers=headers,data={'file':(io.BytesIO(source),'source.plt')})
                identifier=submitted.json['id'];queue=app.extensions['pattern_layout_queue'];queue.futures[identifier].result(timeout=30)
                other={**headers,'X-Pattern-Layout-Owner':'owner-2'}
                self.assertEqual(client.get('/internal/pattern-layout/jobs/'+identifier,headers=other).status_code,404)
                import zipfile,gzip
                blob=client.get('/internal/pattern-layout/jobs/'+identifier+'/archive',headers=headers)
                self.assertEqual(blob.status_code,200)
                with zipfile.ZipFile(io.BytesIO(blob.data)) as archive:
                    manifest=json.loads(archive.read('manifest.json'))
                    for member in manifest['files']:
                        data=archive.read(member['path'])
                        self.assertEqual(hashlib.sha256(data).hexdigest(),member['sha256'])
                        self.assertEqual(len(data),member['bytes'])
                    audit=json.loads(gzip.decompress(archive.read('resources/audit.json.gz')))
                    index=json.loads(archive.read('index.json'))
                    self.assertEqual([p['id'] for p in index['pieces']],[p['id'] for p in audit['pieces']])
                    self.assertEqual([p['id'] for p in index['auxiliary']],[p['id'] for p in audit['auxiliary']])
                    # Complete archive must be acknowledged, never just index.
                    self.assertEqual(client.post('/internal/pattern-layout/jobs/'+identifier+'/ack',headers=headers,json={'durable_complete':True,'archive_sha256':'wrong'}).status_code,409)
                    self.assertEqual(client.post('/internal/pattern-layout/jobs/'+identifier+'/ack',headers=headers,json={'durable_complete':True,'archive_sha256':blob.headers['X-Archive-Sha256']}).status_code,200)
                    piece=audit['pieces'][0]
                    retrieved=json.loads(gzip.decompress(archive.read('resources/candidates/'+piece['id']+'.json.gz')))
                    self.assertEqual([{k:v for k,v in f.items() if k!='preview_phase_mm'} for f in retrieved['fragments']],piece['fragments'])
                    self.assertTrue(all('preview_phase_mm' in f for f in retrieved['fragments']))
                project={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),'candidates':[piece],'placements':[{'piece_id':piece['id'],'x':0,'y':0}]}
                def submit(payload=project):return client.post('/internal/pattern-layout/export',headers={**headers,'X-Idempotency-Key':'stable-save'},data={'file':(io.BytesIO(source),'source.plt'),'project':json.dumps(payload)})
                first=submit();second=submit()
                self.assertEqual((first.status_code,second.status_code),(202,202));self.assertEqual(first.json['id'],second.json['id'])
                changed={**project,'placements':[{'piece_id':piece['id'],'x':1,'y':0}]}
                self.assertEqual(submit(changed).status_code,409)
                queue.futures[first.json['id']].result(timeout=30)
                status=client.get('/internal/pattern-layout/jobs/'+first.json['id'],headers=headers)
                self.assertTrue(status.json['result_ready'])
                self.assertEqual(client.get('/internal/pattern-layout/jobs/'+first.json['id']+'/result',headers=other).status_code,404)
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_running_cancel_timeout_and_process_crash_reap_before_next_task(self):
        import time
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client();headers={'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'owner-1'}
            try:
                queue=app.extensions['pattern_layout_queue']
                heavy=('IN;'+''.join(f'PU{i*800},0;PD{i*800+400},0,{i*800+400},400,{i*800},400,{i*800},0;' for i in range(250))).encode()
                simple=b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
                def submit(raw,key):return client.post('/internal/pattern-layout/jobs',headers={**headers,'X-Idempotency-Key':key},data={'file':(io.BytesIO(raw),'fixture.plt')}).json['id']
                def running(identifier):
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        meta=queue.read(identifier)
                        if meta.get('status')=='processing':return queue.active_process
                        time.sleep(.01)
                    self.fail('actual child never entered processing')
                first=submit(heavy,'cancel-running');process=running(first)
                client.delete('/internal/pattern-layout/jobs/'+first,headers=headers)
                queue.futures[first].result(timeout=10)
                self.assertFalse(process.is_alive());self.assertIsNotNone(process.exitcode)
                self.assertEqual(queue.read(first)['status'],'cancelled')
                second=submit(simple,'after-cancel');queue.futures[second].result(timeout=30)
                self.assertEqual(queue.read(second)['status'],'done')
                queue.wall_seconds=.02
                third=submit(heavy,'short-wall');queue.futures[third].result(timeout=10)
                self.assertEqual(queue.read(third)['error_code'],'wall_time_exceeded')
                queue.wall_seconds=30
                fourth=submit(simple,'after-wall');queue.futures[fourth].result(timeout=30)
                self.assertEqual(queue.read(fourth)['status'],'done')
                fifth=submit(heavy,'crash-worker');process=running(fifth);process.kill()
                queue.futures[fifth].result(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(queue.read(fifth)['error_code'],'worker_crash_or_memory_budget')
                sixth=submit(simple,'after-crash');queue.futures[sixth].result(timeout=30)
                self.assertEqual(queue.read(sixth)['status'],'done')
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_acknowledged_content_is_not_reused_for_a_new_attachment(self):
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client();headers={'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'owner-1'}
            try:
                queue=app.extensions['pattern_layout_queue'];source=b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
                def submit(h=headers):return client.post('/internal/pattern-layout/jobs',headers=h,data={'file':(io.BytesIO(source),'same-bytes.plt')})
                first=submit().json['id'];queue.futures[first].result(timeout=30)
                meta=queue.read(first)
                self.assertEqual(client.post('/internal/pattern-layout/jobs/'+first+'/ack',headers=headers,json={'durable_complete':True,'archive_sha256':meta['archive_sha256']}).status_code,200)
                self.assertEqual(client.get('/internal/pattern-layout/jobs/'+first+'/archive',headers=headers).status_code,404)
                second=submit();self.assertEqual(second.status_code,202);second=second.json['id']
                self.assertNotEqual(first,second);queue.futures[second].result(timeout=30)
                self.assertEqual(client.get('/internal/pattern-layout/jobs/'+second+'/archive',headers=headers).status_code,200)
                other={**headers,'X-Pattern-Layout-Owner':'owner-2'}
                self.assertEqual(client.get('/internal/pattern-layout/jobs/'+second+'/archive',headers=other).status_code,404)
                explicit={**headers,'X-Idempotency-Key':'same-operation'}
                third=submit(explicit).json['id'];queue.futures[third].result(timeout=30)
                meta=queue.read(third)
                client.post('/internal/pattern-layout/jobs/'+third+'/ack',headers=explicit,json={'durable_complete':True,'archive_sha256':meta['archive_sha256']})
                self.assertEqual(submit(explicit).json['id'],third)
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_completed_exports_release_worst_case_reservation_and_keep_downloads(self):
        from concurrent.futures import ThreadPoolExecutor
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();headers={'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'owner-1'}
            try:
                queue=app.extensions['pattern_layout_queue']
                source=b'IN;PU0,0;PD400,0;PU;'
                def submit(i):
                    project={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),
                             'candidates':[{'id':'p','fragments':[{'path_id':'shape-1','segment_index':0,'t0':0,'t1':1}]}],
                             'placements':[{'piece_id':'p','x':i,'y':0}]}
                    with app.test_client() as client:
                        response=client.post('/internal/pattern-layout/export',headers={**headers,'X-Idempotency-Key':'capacity-'+str(i)},data={'file':(io.BytesIO(source),'source.plt'),'project':json.dumps(project)})
                    with queue.lock:
                        reserved=sum(meta['reserved_bytes'] for folder in queue.root.iterdir() if folder.is_dir() and (meta:=queue.read(folder.name)))
                        self.assertLessEqual(reserved,queue.budget)
                    return response
                ids=[]
                for i in range(6):
                    response=submit(i);self.assertEqual(response.status_code,202);identifier=response.json['id'];ids.append(identifier)
                    queue.futures[identifier].result(timeout=30)
                    self.assertEqual(queue.read(identifier)['status'],'done')
                    self.assertLess(queue.read(identifier)['reserved_bytes'],100000)
                with app.test_client() as client:
                    for identifier in ids:self.assertEqual(client.get('/internal/pattern-layout/jobs/'+identifier+'/result',headers=headers).status_code,200)
                queue.budget=queue.result_budget*3+100000
                with ThreadPoolExecutor(max_workers=6) as pool:responses=list(pool.map(submit,range(6,12)))
                for response in responses:
                    self.assertIn(response.status_code,(202,503))
                    if response.status_code==202:queue.futures[response.json['id']].result(timeout=30)
                self.assertEqual(submit(100).status_code,202)
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_large_project_uses_spooled_file_part_and_total_request_budget_remains_enforced(self):
        policy={**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only','PATTERN_LAYOUT_MAX_REQUEST_BYTES':str(1024*1024)}
        with patch.dict(os.environ,policy):
            app=create_app();client=app.test_client();headers={'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'owner-1'}
            try:
                source=b'IN;PU0,0;PD400,0;PU;'
                project={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),
                         'candidates':[{'id':'p','fragments':[{'path_id':'shape-1','segment_index':0,'t0':0,'t1':1}]}],
                         'placements':[{'piece_id':'p','x':0,'y':0}],'padding':'x'*600000}
                encoded=json.dumps(project).encode()
                response=client.post('/internal/pattern-layout/export',headers=headers,data={'file':(io.BytesIO(source),'source.plt'),'project':(io.BytesIO(encoded),'project.json','application/json')})
                self.assertEqual(response.status_code,202,response.json)
                identifier=response.json['id'];queue=app.extensions['pattern_layout_queue'];queue.futures[identifier].result(timeout=30)
                self.assertEqual(queue.read(identifier)['status'],'done')
                self.assertEqual(client.get('/internal/pattern-layout/jobs/'+identifier+'/result',headers=headers).status_code,200)
                oversized=client.post('/internal/pattern-layout/export',headers=headers,data={'file':(io.BytesIO(source),'source.plt'),'project':(io.BytesIO(b' '*1100000),'project.json','application/json')})
                self.assertEqual(oversized.status_code,413);self.assertEqual(oversized.json['error_code'],'request_too_large')
                ordinary_field=client.post('/internal/pattern-layout/export',headers=headers,data={'file':(io.BytesIO(source),'source.plt'),'project':encoded.decode()})
                self.assertEqual(ordinary_field.status_code,413)
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_public_failure_codes_hide_internal_exception_paths(self):
        from app.pattern_layout_errors import public_task_error
        from app.services.pattern_layout_api import PatternLayoutExportError
        code,message=public_task_error(OSError('/private/not-a-user-visible-path/source.plt'),'export')
        self.assertEqual(code,'export_failed');self.assertNotIn('/private/',message)
        for expected in ('no_selected_fragments','source_warning_confirmation_required','label_pose_not_faithful','line_type_phase_not_faithful'):
            code,message=public_task_error(PatternLayoutExportError(expected),'export')
            self.assertEqual(code,expected);self.assertTrue(any('\u4e00'<=ch<='\u9fff' for ch in message))
