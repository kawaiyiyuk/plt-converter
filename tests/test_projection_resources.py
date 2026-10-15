"""Synthetic policy tests; numbers are fixtures, never production defaults.

macOS cannot set Python's AS limit to512MiB; its fixture is explicitly1TiB.
Linux512MiB/PDEATHSIG/OOM behavior requires separate Linux runtime acceptance.
"""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.projection_app import create_projection_app
from app.projection_store import FIELDS, MAX_BODY, MAX_SOURCE, Policy, ProjectionRejected, Store, process_identity
from app.projection_runtime import run
import test_projection as fixtures
from test_projection import synthetic_policy, pdf_bytes, BASE

class ResourceTests(unittest.TestCase):
    setUp=fixtures.ProjectionTests.setUp
    identity=fixtures.ProjectionTests.identity
    upload=fixtures.ProjectionTests.upload
    tile=fixtures.ProjectionTests.tile
    metadata=fixtures.ProjectionTests.metadata
    renew=fixtures.ProjectionTests.renew

    def policy_restore(self):
        self.ledger.policy=Policy(self.configuration)

    def create(self):
        self.policy_restore()
        source=pdf_bytes()
        lease=self.ledger.reserve(self.owner,'upload')
        folder=self.ledger.lease_path(lease);folder.mkdir()
        path=folder/'source.pdf';path.write_bytes(source)
        meta={'owner':self.owner,'expires_at':time.time()+3600,'filename':'fixture.pdf','pages':[{'index':0,'width_mm':200*25.4/72,'height_mm':300*25.4/72}],
              'page_count':1,'width_mm':200*25.4/72,'height_mm':300*25.4/72,'requires_metadata_choice':False,'embedded_layout':None}
        document=self.ledger.complete_upload(lease,self.owner,path,meta)
        self.ledger.release(lease)
        return document

    def lease_count(self):
        with self.ledger.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]

    def test_disabled_factory_and_missing_policy_fail_closed(self):
        app=create_projection_app({'PROJECTOR_ENABLED':'0'})
        self.assertEqual(app.test_client().post(BASE).status_code,503)
        for field in FIELDS:
            config=dict(self.configuration);config.pop('PROJECTOR_'+field)
            with self.subTest(field=field),self.assertRaises(ValueError):
                create_projection_app(config)
        for bad in ('0','-1','1.5','1x',' 2','true'):
            config=dict(self.configuration);config['PROJECTOR_GLOBAL_PARSERS']=bad
            with self.assertRaises(ValueError):create_projection_app(config)

    def test_independent_factory_has_no_legacy_routes_or_health(self):
        rules={str(rule) for rule in self.app.url_map.iter_rules()}
        self.assertFalse(any('/api/v1/plt' in rule for rule in rules))
        self.assertFalse(any('/api/v1/pdf/preview' in rule for rule in rules))
        self.assertNotIn('/health/worker',rules)
        self.assertEqual(self.client.get('/health/live').get_json()['service'],'pdf-projector')

    def test_quota_rejects_new_but_not_renew_delete_and_no_absolute_ttl(self):
        first=self.create();second=self.create()
        with self.assertRaises(ProjectionRejected) as error:self.ledger.reserve(self.owner,'upload')
        self.assertEqual(error.exception.code,'projection_owner_capacity')
        with patch('app.projection_layout.projection_connection',side_effect=RuntimeError('Redis down')):
            self.assertEqual(self.renew(first).status_code,200)
            self.assertEqual(self.client.delete(f'{BASE}/{second}',headers={'Authorization':'x'}).status_code,204)
        expiry=self.metadata(first)['expires_at']
        with patch('app.projection.time.time',return_value=expiry-1):
            self.assertEqual(self.renew(first).status_code,200)
        self.assertGreater(self.metadata(first)['expires_at'],expiry+3500)
        lease=self.ledger.reserve(self.owner,'upload');self.ledger.release(lease)

    def test_owner_source_bytes_is_reserved_before_body_not_client_header(self):
        self.policy_restore();self.ledger.policy.global_parsers=10
        lease=self.ledger.reserve(self.owner,'upload')
        self.ledger.policy.owner_source_bytes=MAX_SOURCE
        with self.assertRaises(ProjectionRejected):self.ledger.reserve(self.owner,'upload')
        self.ledger.release(lease)
        with patch.object(self.ledger,'rate',side_effect=lambda *args: Store.rate(self.ledger,*args)):
            self.ledger.policy.upload_owner_rate=1
            one=self.client.post(BASE,headers={'Authorization':'x','X-Client-Key':'a'},data={'file':(io.BytesIO(pdf_bytes()),'x.pdf')})
            two=self.client.post(BASE,headers={'Authorization':'x','X-Client-Key':'b'},data={'file':(io.BytesIO(pdf_bytes()),'x.pdf')})
        self.assertEqual(one.status_code,201);self.assertEqual(two.status_code,429)

    def test_concurrent_document_admission_is_atomic(self):
        self.policy_restore();self.ledger.policy.global_parsers=10
        barrier=threading.Barrier(8);passed=[];failed=[]
        def reserve():
            barrier.wait()
            try:passed.append(self.ledger.reserve(self.owner,'upload'))
            except ProjectionRejected:failed.append(True)
        threads=[threading.Thread(target=reserve) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join(3);self.assertFalse(t.is_alive())
        # Serialized by the transaction, each reserve supersedes the previous
        # upload, so slots 1-3 succeed (slot 1 and 2 become superseded) and the
        # 4th is refused by the superseded-upload cap.
        self.assertEqual(len(passed),3);self.assertEqual(len(failed),5)
        for lease in passed:self.ledger.release(lease)
        self.assertEqual(self.lease_count(),0)

    def test_global_budget_and_free_floor_preserve_control(self):
        doc=self.create();generation=self.ledger.load(doc,self.owner)['_generation']
        self.ledger.policy.global_disk_bytes=self.ledger.policy.system_bytes+1
        with self.assertRaises(ProjectionRejected):self.ledger.reserve('user:2','upload')
        self.assertEqual(self.renew(doc).status_code,200)
        self.ledger.policy.global_disk_bytes=2*1024**3
        with patch('app.projection_store.os.statvfs') as stat:
            stat.return_value.f_bavail=1;stat.return_value.f_frsize=4096
            with self.assertRaises(ProjectionRejected) as error:self.ledger.reserve(self.owner,'tile',doc,generation)
            self.assertEqual(error.exception.status,503)
            self.assertEqual(self.renew(doc).status_code,200)
        self.assertEqual(self.client.delete(f'{BASE}/{doc}',headers={'Authorization':'x'}).status_code,204)

    def test_multipart_size_invalid_pdf_multifile_cleanup_leaves_no_leases(self):
        self.policy_restore()
        for data in ({'file':(io.BytesIO(b'invalid'),'x.pdf')},
                     {'file':(io.BytesIO(pdf_bytes()),'x.txt')},
                     {'file':(io.BytesIO(pdf_bytes()),'x.pdf'),'extra':(io.BytesIO(b'x'),'second.pdf')}):
            response=self.client.post(BASE,headers={'Authorization':'x'},data=data)
            self.assertIn(response.status_code,(400,413,415))
            self.assertEqual(self.lease_count(),0)
            self.assertEqual(list((self.ledger.root/'staging').iterdir()),[])
        response=self.client.post(BASE,headers={'Authorization':'x'},data={'file':(io.BytesIO(b'x'*(MAX_SOURCE+1)),'x.pdf')})
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.lease_count(),0)
        self.assertEqual(list((self.ledger.root/'staging').iterdir()),[])

    def test_upload_failed_child_and_publish_rename_release_budget(self):
        for method in ('child','rename'):
            with self.subTest(method=method):
                target='app.projection_runtime.run' if method=='child' else 'app.projection_store.os.replace'
                error=ProjectionRejected('projection_child_timeout','fixture timeout',503) if method=='child' else OSError('disk full')
                with patch(target,side_effect=error):self.assertEqual(self.upload().status_code,503)
                self.assertEqual(self.lease_count(),0)
                self.assertEqual(list((self.ledger.root/'staging').iterdir()),[])
                self.assertEqual(list((self.ledger.root/'documents').iterdir()),[])

    def test_summary_owner_expiry_no_renew_cap_private_fields(self):
        document=self.create();before=self.metadata(document)
        response=self.client.get(f'{BASE}/{document}',headers={'Authorization':'x'})
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.get_json()['layout'])
        self.assertTrue(response.get_json()['session_summary_supported'])
        self.assertEqual(self.metadata(document),before)
        self.assertNotIn('session_summary_supported',before);self.assertNotIn('rotation_supported',before);self.assertNotIn('rotation_deg',before)
        self.owner='user:2'
        self.assertEqual(self.client.get(f'{BASE}/{document}',headers={'Authorization':'x'}).status_code,404)
        self.owner='user:1'
        with patch('app.projection.time.time',return_value=before['expires_at']):
            self.assertEqual(self.client.get(f'{BASE}/{document}',headers={'Authorization':'x'}).status_code,404)
            self.assertEqual(self.renew(document).status_code,404)

    def test_renew_cannot_be_overwritten_by_stale_metadata_or_expiry_snapshot(self):
        doc=self.create();old=self.ledger.load(doc,self.owner)
        with patch('app.projection.time.time',return_value=old['expires_at']-1):
            self.assertTrue(self.ledger.renew(doc,self.owner))
            latest=self.metadata(doc)['expires_at']
            old['layout']={'rows':1}
            self.assertTrue(self.ledger.update(doc,self.owner,old,old['_generation']))
            self.ledger.reconcile()
            self.assertEqual(self.metadata(doc)['expires_at'],latest)
        self.assertTrue(self.ledger.source_path(doc).exists())

    def test_reconcile_does_not_free_live_child_or_reused_pid(self):
        doc=self.create();lease=self.ledger.reserve(self.owner,'tile',doc,0)
        self.ledger.attach(lease,os.getpid())
        self.ledger.release(lease);self.assertEqual(self.lease_count(),1)
        with self.ledger.transaction() as db:
            db.execute('UPDATE leases SET pid=NULL,supervisor=?,supervisor_identity=NULL WHERE id=?',(99999999,lease))
        self.ledger.reconcile();self.assertEqual(self.lease_count(),0)
        from app.projection_store import process_alive
        if sys.platform.startswith('linux'):
            self.assertFalse(process_alive(os.getpid(),'impossible start identity'))

    def test_lease_reclaimed_after_crash_and_cleanup_failure_keeps_budget(self):
        self.policy_restore();lease=self.ledger.reserve(self.owner,'upload')
        folder=self.ledger.lease_path(lease);folder.mkdir();(folder/'source.pdf').write_bytes(b'x')
        with self.ledger.transaction() as db:db.execute('UPDATE leases SET supervisor=? WHERE id=?',(99999999,lease))
        with patch('shutil.rmtree',side_effect=OSError('fixture unlink failure')):
            with self.assertRaises(OSError):self.ledger.reconcile()
        self.assertEqual(self.lease_count(),1)
        self.ledger.reconcile();self.assertEqual(self.lease_count(),0);self.assertFalse(folder.exists())

    def hanging_process(self,cpu=False):
        actual=subprocess.Popen
        processes=[]
        def launch(*args,**kwargs):
            code='import resource\n'
            if cpu:code+='resource.setrlimit(resource.RLIMIT_CPU,(1,1))\nwhile True: pass\n'
            else:code+='import time\ntime.sleep(30)\n'
            process=actual([sys.executable,'-c',code],**kwargs)
            processes.append(process)
            return process
        return patch('app.projection_runtime.subprocess.Popen',side_effect=launch),processes

    def test_real_child_hang_wall_kill_reaped_and_slot_released(self):
        doc=self.create();lease=self.ledger.reserve(self.owner,'tile',doc,0)
        mock,processes=self.hanging_process()
        start=time.monotonic()
        with mock,self.assertRaises(ProjectionRejected) as error:
            run(self.ledger,lease,'tile',self.ledger.source_path(doc),deadline=.3)
        self.assertEqual(error.exception.code,'projection_child_timeout')
        self.assertLess(time.monotonic()-start,2)
        self.assertIsNotNone(processes[0].returncode)
        self.ledger.release(lease);self.assertEqual(self.lease_count(),0)
        with self.assertRaises(ProcessLookupError):os.kill(processes[0].pid,0)

    def test_real_child_cpu_limit_kills_and_is_reaped(self):
        # Production launches projection_bootstrap, not the raw interpreter, so
        # this first proves bootstrap itself installs RLIMIT_CPU from the spec
        # before importing the task module, and the kernel kills the runaway.
        folder=Path(self.temp.name)/'bootstrap';app_pkg=folder/'app';app_pkg.mkdir(parents=True)
        (app_pkg/'__init__.py').write_text('')
        (app_pkg/'projection_child.py').write_text('def execute(spec):\n    while True:\n        pass\n')
        spec=folder/'request.json'
        spec.write_text(json.dumps({'operation':'tile','source':'unused','output':str(folder/'result.png'),
                                    'parameters':{},'parent':os.getpid(),'module_root':str(folder),
                                    'limits':{'memory':1024**4 if sys.platform.startswith('darwin') else 512*1024*1024,
                                              'cpu':1,'file':1024*1024}}))
        bootstrap=Path(__file__).absolute().parent.parent/'projection_bootstrap.py'
        boot=subprocess.Popen([sys.executable,str(bootstrap),str(spec)],cwd=str(bootstrap.parent),
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            boot.wait(timeout=30)
        finally:
            if boot.poll() is None:
                boot.kill();boot.wait()
        self.assertLess(boot.returncode,0)
        self.assertTrue((folder/'child.json').exists())
        # The supervisor must also reap a SIGXCPU child and release its slot. The
        # mock runs the interpreter directly and sets its own RLIMIT_CPU, which
        # covers reaping/slot release but not bootstrap limit installation.
        doc=self.create();lease=self.ledger.reserve(self.owner,'tile',doc,0)
        mock,processes=self.hanging_process(cpu=True)
        with mock,self.assertRaises(ProjectionRejected) as error:
            run(self.ledger,lease,'tile',self.ledger.source_path(doc),deadline=5)
        self.assertEqual(error.exception.code,'projection_child_failed')
        self.assertLess(processes[0].returncode,0)
        self.ledger.release(lease);self.assertEqual(self.lease_count(),0)
        with self.assertRaises(ProcessLookupError):os.kill(processes[0].pid,0)

    def test_renew_delete_do_not_wait_for_busy_renderer_and_late_result_is_discarded(self):
        doc=self.create();lease=self.ledger.reserve(self.owner,'tile',doc,0)
        mock,processes=self.hanging_process()
        errors=[]
        def render():
            try:run(self.ledger,lease,'tile',self.ledger.source_path(doc),deadline=10)
            except ProjectionRejected as error:errors.append(error.code)
            finally:self.ledger.release(lease)
        with mock:
            thread=threading.Thread(target=render);thread.start()
            start=time.monotonic()
            while not processes and time.monotonic()-start<1:time.sleep(.01)
            self.assertTrue(processes)
            started=time.monotonic()
            self.assertEqual(self.renew(doc).status_code,200)
            self.assertEqual(self.client.delete(f'{BASE}/{doc}',headers={'Authorization':'x'}).status_code,204)
            self.assertLess(time.monotonic()-started,.5)
            thread.join(2);self.assertFalse(thread.is_alive())
        self.assertEqual(errors,['projection_result_stale'])
        self.assertEqual(self.lease_count(),0);self.assertIsNone(self.metadata(doc))
        self.assertFalse(self.ledger.source_path(doc).exists())
        with self.assertRaises(ProcessLookupError):os.kill(processes[0].pid,0)

    def test_layout_generation_cancels_busy_renderer(self):
        doc=self.create();lease=self.ledger.reserve(self.owner,'tile',doc,0)
        mock,processes=self.hanging_process();errors=[]
        def render():
            try:run(self.ledger,lease,'tile',self.ledger.source_path(doc),deadline=10)
            except ProjectionRejected as error:errors.append(error.code)
            finally:self.ledger.release(lease)
        with mock:
            thread=threading.Thread(target=render);thread.start()
            start=time.monotonic()
            while not processes and time.monotonic()-start<1:time.sleep(.01)
            response=self.client.post(f'{BASE}/{doc}/layout',headers={'Authorization':'x'},data={'method':'manual','rows':1,'columns':1})
            self.assertEqual(response.status_code,200)
            thread.join(2);self.assertFalse(thread.is_alive())
        self.assertEqual(errors,['projection_result_stale'])
        self.assertTrue(self.ledger.source_path(doc).exists());self.assertEqual(self.lease_count(),0)

    def test_atomic_renderer_and_parser_admission(self):
        doc=self.create();first=self.ledger.reserve(self.owner,'tile',doc,0)
        with self.assertRaises(ProjectionRejected) as error:self.ledger.reserve(self.owner,'tile',doc,0)
        self.assertEqual(error.exception.code,'projection_renderer_busy')
        # Upload reservation no longer consumes a parser slot before its body;
        # admission is atomic in begin_parse, after the source is complete.
        parser=self.ledger.reserve('user:2','upload')
        self.ledger.begin_parse(parser)
        queued=self.ledger.reserve('user:3','upload')
        with self.assertRaises(ProjectionRejected) as error:self.ledger.begin_parse(queued)
        self.assertEqual(error.exception.code,'projection_parser_busy')
        self.ledger.release(first);self.ledger.release(parser);self.ledger.release(queued)

    def test_rate_table_capacity_and_expiry_are_bounded(self):
        self.policy_restore();self.ledger.policy.rate_rows=2
        Store.rate(self.ledger,'one','sameip','upload')
        with self.assertRaises(ProjectionRejected) as error:Store.rate(self.ledger,'two','differentip','upload')
        self.assertEqual(error.exception.code,'projection_limiter_capacity')
        with self.ledger.transaction() as db:self.assertLessEqual(db.execute('SELECT COUNT(*) FROM rates').fetchone()[0],2)
        with patch('app.projection_store.time.time',return_value=time.time()+121):
            Store.rate(self.ledger,'two','differentip','upload')

    def test_worst_normalized_200page_grid_embedded_layout_analysis_fit_metadata_bound(self):
        from app.projection_layout import build_layout,public_layout
        from app.projection_store import META_BOUND
        doc=self.create();meta=self.ledger.load(doc,self.owner)
        # Largest field populations permitted by the existing contract:
        # 200 pages/placements,576 slots in each of embedded/layout/analysis.
        pages=[{'index':i,'width_mm':1.234567890123456e32,'height_mm':9.876543210123456e32} for i in range(200)]
        layout=build_layout(pages,24,24,'column',{'left':99.999999999,'right':99.999999999,'top':99.999999999,'bottom':99.999999999})
        meta.update(pages=pages,page_count=200,layout=layout,
            embedded_layout={'version':1,**public_layout(layout),'drawing_width_mm':9999.999999999,'drawing_height_mm':9999.999999999,'complete_layout':True},
            analysis={'analysis_id':'projection-layout-'+doc+'-'+'a'*16,'status':'ready','mode':'original',
                      'suggestion':{**public_layout(layout),'confidence':'medium','confidence_score':.9999999999999999}},
            filename='缝纫记忆'*12+'.pdf')
        self.assertLess(len(json.dumps(meta).encode()),META_BOUND)
        self.assertTrue(self.ledger.update(doc,self.owner,meta,0,invalidate=True))
        actual=self.ledger.load(doc,self.owner)
        self.assertEqual(actual['pages'],pages);self.assertEqual(actual['layout']['page_slots'],layout['page_slots'])
        self.assertEqual(len(actual['layout']['placements']),200)
        self.assertEqual(self.renew(doc).status_code,200)
        with self.ledger.transaction() as db:
            row=db.execute('SELECT meta,budget FROM sessions WHERE id=?',(doc,)).fetchone()
            self.assertLess(len(row['meta']),128)
            self.assertGreaterEqual(row['budget'],2*META_BOUND)

    def test_metadata_update_failure_keeps_old_version_and_does_not_leak_third_copy(self):
        doc=self.create();before=self.metadata(doc)
        meta=self.ledger.load(doc,self.owner);meta['layout']={'rows':1}
        with patch.object(self.ledger,'_write_metadata',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.ledger.update(doc,self.owner,meta,0)
        self.assertEqual(self.metadata(doc),before)
        self.assertEqual(len(list((self.ledger.root/'metadata').glob(doc+'.*.json'))),1)
        self.assertEqual(self.renew(doc).status_code,200)

    def test_queued_analysis_ttl_reclaims_budget_without_redis_and_does_not_delete_source(self):
        doc=self.create();lease=self.ledger.reserve(self.owner,'analysis',doc,0)
        folder=self.ledger.lease_path(lease);folder.mkdir();(folder/'request.json').write_text('{}')
        with self.ledger.transaction() as db:db.execute('UPDATE leases SET created=? WHERE id=?',(time.time()-3601,lease))
        with patch('app.projection_layout.projection_connection',side_effect=RuntimeError('Redis offline')):
            self.ledger.reconcile()
        self.assertEqual(self.lease_count(),0);self.assertTrue(self.ledger.source_path(doc).exists())
        self.assertEqual(self.renew(doc).status_code,200)

    def test_db_and_journal_have_explicit_hard_page_bound(self):
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            self.assertEqual(db.execute('PRAGMA max_page_count').fetchone()[0],self.ledger.policy.db_bytes//4096)
            self.assertGreaterEqual(self.ledger.policy.system_bytes,2*self.ledger.policy.db_bytes)
