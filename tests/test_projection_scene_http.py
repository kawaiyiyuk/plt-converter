"""Real Store / child / HTTP coverage for immutable scene delivery."""
import hashlib
import io
import json
from pathlib import Path
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

import test_projection as fixtures
from test_projection import BASE, pdf_bytes
from app.projection import identify_user as actual_identity_guard


class SceneHttpTests(unittest.TestCase):
    setUp = fixtures.ProjectionTests.setUp
    identity = fixtures.ProjectionTests.identity
    upload = fixtures.ProjectionTests.upload
    metadata = fixtures.ProjectionTests.metadata

    def setUp(self):
        fixtures.ProjectionTests.setUp(self)
        self.ledger.policy.derived_max_bytes = 64 * 1024**2
        self.ledger.policy.owner_derived_bytes = 128 * 1024**2

    def prepare(self, doc, **options):
        return self.client.post(f'{BASE}/{doc}/scene', headers={'Authorization':'Bearer fixture'},data=options)

    def create(self, source=None, filename='pattern.pdf'):
        response = self.upload(source, filename)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['document_id']

    def test_real_pdf_bundle_is_immutable_authorized_and_repeat_downloads_never_render(self):
        doc = self.create()
        response = self.prepare(doc)
        self.assertEqual(response.status_code, 200, response.get_json())
        descriptor = response.get_json()
        self.assertEqual(descriptor['document_id'], doc)
        self.assertEqual(descriptor['source_format'], 'pdf')
        with patch('app.projection_runtime.run', side_effect=AssertionError('must reuse cached scene')):
            self.assertEqual(self.prepare(doc).get_json()['revision'], descriptor['revision'])
            for _ in range(3):
                bundle = self.client.get(descriptor['bundle']['path'], headers={'Authorization':'Bearer fixture'})
                self.assertEqual(bundle.status_code, 200)
                data = bundle.data
                bundle.close()
                self.assertEqual(bundle.mimetype, 'application/zip')
                self.assertEqual(bundle.headers['Cache-Control'], 'no-store')
                self.assertEqual(hashlib.sha256(data).hexdigest(), descriptor['bundle']['sha256'])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn('scene.json', archive.namelist())
            scene = json.loads(archive.read('scene.json'))
            self.assertAlmostEqual(scene['width_mm'], descriptor['width_mm'])
            self.assertEqual(sum(i.file_size for i in archive.infolist()), descriptor['bundle']['unpacked_bytes'])
        self.owner = 'user:2'
        self.assertEqual(self.client.get(descriptor['bundle']['path'], headers={'Authorization':'x'}).status_code,404)
        self.owner = 'user:1'
        self.assertEqual(self.client.get(descriptor['bundle']['path']).status_code,401)
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_multi_page_requires_layout_then_new_layout_revokes_old_revision(self):
        doc = self.create(pdf_bytes(2))
        self.assertEqual(self.prepare(doc).status_code,409)
        def layout(columns):
            response=self.client.post(f'{BASE}/{doc}/layout',headers={'Authorization':'x'},data={'method':'manual','rows':2,'columns':columns})
            self.assertEqual(response.status_code,200,response.get_json())
        layout(1)
        first=self.prepare(doc).get_json()
        layout(2)
        self.assertEqual(self.client.get(first['bundle']['path'],headers={'Authorization':'x'}).status_code,404)
        second=self.prepare(doc).get_json()
        self.assertNotEqual(first['revision'],second['revision'])
        self.assertNotEqual(first['width_mm'],second['width_mm'])

    def test_plt_uses_same_scene_path_and_both_derivatives_are_charged(self):
        doc=self.create(b'IN;SP1;PA;PU0,0;PD8000,0,8000,4000,0,4000,0,0;PU;','pattern.plt')
        descriptor=self.prepare(doc).get_json()
        self.assertEqual(descriptor['source_format'],'plt')
        with self.ledger.transaction() as db:
            row=db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()
            self.assertGreater(row['derived_bytes'], self.ledger.source_path(doc).stat().st_size)
        bundle=self.client.get(descriptor['bundle']['path'],headers={'Authorization':'x'})
        self.assertEqual(bundle.status_code,200);bundle.close()

    def test_corruption_never_returns_200_and_delete_cleans_artifacts_and_accounting(self):
        doc=self.create();descriptor=self.prepare(doc).get_json()
        path=self.ledger.scene_path(doc,descriptor['revision'])
        raw=path.read_bytes();path.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        self.assertEqual(self.client.get(descriptor['bundle']['path'],headers={'Authorization':'x'}).status_code,503)
        self.assertEqual(self.client.delete(f'{BASE}/{doc}',headers={'Authorization':'x'}).status_code,204)
        self.assertFalse(path.exists())
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles').fetchone()[0],0)

    def test_download_fd_stays_charged_after_delete_until_close(self):
        doc=self.create();descriptor=self.prepare(doc).get_json()
        response=self.client.get(descriptor['bundle']['path'],headers={'Authorization':'x'},buffered=False)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.delete(f'{BASE}/{doc}',headers={'Authorization':'x'}).status_code,204)
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leases WHERE kind='scene-download'").fetchone()[0],1)
        response.close()
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_concurrent_prepare_coalesces_the_same_supervised_export(self):
        from app.projection_runtime import run as original
        doc=self.create()
        started=threading.Event();follower=threading.Event();release=threading.Event()
        calls=[];responses=[];errors=[]
        original_begin=self.ledger.scene_begin
        original_open=self.ledger.scene_open
        def controlled(store,lease,operation,*args,**kwargs):
            if operation=='scene':
                calls.append(operation);started.set();self.assertTrue(follower.wait(4))
            return original(store,lease,operation,*args,**kwargs)
        def begin(*args,**kwargs):
            result=original_begin(*args,**kwargs)
            if not result[1]:follower.set()
            return result
        def opened(*args,**kwargs):
            result=original_open(*args,**kwargs)
            # Hold the successful descriptor's bounded validation FD until the
            # other request has exercised the existing shared-slot busy path.
            # This is download/validation contention, not a second export.
            if not release.wait(4):
                result[0].close();self.ledger.scene_close(result[1])
                self.fail('concurrent request did not finish its shared-slot check')
            return result
        def request_scene():
            try:
                with self.app.test_client() as client:
                    r=client.post(f'{BASE}/{doc}/scene',headers={'Authorization':'x'})
                    responses.append((r.status_code,r.get_json(),r.headers.get('Retry-After')))
                    if r.status_code==429:release.set()
                    r.close()
            except BaseException as error:
                errors.append(error);release.set()
        with patch('app.projection_runtime.run',side_effect=controlled), \
             patch.object(self.ledger,'scene_begin',side_effect=begin), \
             patch.object(self.ledger,'scene_open',side_effect=opened):
            first=threading.Thread(target=request_scene);second=threading.Thread(target=request_scene)
            try:
                first.start();self.assertTrue(started.wait(3));second.start()
                first.join(6);second.join(6)
                self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
            finally:
                release.set()
                first.join(6)
                if second.ident is not None:second.join(6)
        self.assertEqual(errors,[])
        self.assertEqual(calls,['scene'])
        self.assertEqual(len(responses),2)
        successes=[body for status,body,_ in responses if status==200]
        self.assertGreaterEqual(len(successes),1,responses)
        for status,body,retry_header in responses:
            if status!=200:
                self.assertEqual(status,429,responses)
                self.assertEqual(body['error_code'],'projection_renderer_busy')
                self.assertEqual(body['retry_after'],1)
                self.assertEqual(retry_header,'1')
        revision=successes[0]['revision']
        self.assertTrue(all(body['revision']==revision for body in successes))
        with patch('app.projection_runtime.run',side_effect=AssertionError('retry must reuse scene')):
            for status,_,_ in responses:
                if status==429:
                    retry=self.prepare(doc)
                    self.assertEqual(retry.status_code,200,retry.get_json())
                    self.assertEqual(retry.get_json()['revision'],revision)
                    retry.close()
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_cached_prepare_shares_download_slot_and_retries_without_reexport(self):
        doc=self.create();prepared=self.prepare(doc)
        self.assertEqual(prepared.status_code,200,prepared.get_json())
        descriptor=prepared.get_json();prepared.close()
        self.assertEqual(self.ledger.policy.owner_renderers,1)
        response=self.client.get(descriptor['bundle']['path'],headers={'Authorization':'x'},buffered=False)
        self.assertEqual(response.status_code,200)
        try:
            with patch('app.projection_runtime.run',side_effect=AssertionError('ready scene must not reexport')):
                busy=self.prepare(doc)
                self.assertEqual(busy.status_code,429,busy.get_json())
                self.assertEqual(busy.get_json()['error_code'],'projection_renderer_busy')
                self.assertEqual(busy.get_json()['retry_after'],1)
                self.assertEqual(busy.headers['Retry-After'],'1')
                busy.close()
            with self.ledger.transaction() as db:
                leases=db.execute('SELECT kind,document,generation,scene_revision FROM leases').fetchall()
                self.assertEqual(len(leases),1)
                self.assertEqual(dict(leases[0]),dict(kind='scene-download',document=doc,
                                                   generation=0,scene_revision=descriptor['revision']))
        finally:
            response.close()
        with patch('app.projection_runtime.run',side_effect=AssertionError('retry must reuse scene')):
            retry=self.prepare(doc)
            self.assertEqual(retry.status_code,200,retry.get_json())
            self.assertEqual(retry.get_json()['revision'],descriptor['revision'])
            retry.close()
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_generation_change_during_export_prevents_publication_and_releases_budget(self):
        doc=self.create()
        original=self.ledger.scene_publish
        def race(*args,**kwargs):
            meta=self.ledger.load(doc,self.owner)
            self.assertTrue(self.ledger.update(doc,self.owner,meta,meta['_generation'],invalidate=True))
            return original(*args,**kwargs)
        with patch.object(self.ledger,'scene_publish',side_effect=race):
            response=self.prepare(doc)
        self.assertEqual(response.status_code,409,response.get_json())
        self.assertEqual(list((self.ledger.root/'documents').glob('*.zip')),[])
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles').fetchone()[0],0)

    def test_unconfigured_and_budget_failure_keep_original_and_do_not_publish(self):
        doc=self.create();self.ledger.policy.derived_max_bytes=0
        self.assertEqual(self.prepare(doc).status_code,503)
        self.assertTrue(self.ledger.source_path(doc).is_file())
        self.ledger.policy.derived_max_bytes=256
        self.assertEqual(self.prepare(doc).status_code,400)
        self.assertTrue(self.ledger.source_path(doc).is_file())
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles').fetchone()[0],0)

    def test_expiry_download_and_renew_generation_semantics(self):
        doc=self.create();descriptor=self.prepare(doc).get_json()
        self.assertTrue(self.ledger.renew(doc,self.owner))
        self.assertEqual(self.prepare(doc).get_json()['revision'],descriptor['revision'])
        with self.ledger.transaction() as db:db.execute('UPDATE sessions SET expires=0 WHERE id=?',(doc,))
        self.assertEqual(self.client.get(descriptor['bundle']['path'],headers={'Authorization':'x'}).status_code,404)
        self.assertFalse(self.ledger.scene_path(doc,descriptor['revision']).exists())

    def test_identity_is_revalidated_for_cached_prepare_and_every_download(self):
        with patch('app.projection.identify_user',side_effect=actual_identity_guard), patch('app.projection.identity_request') as identity:
            identity.return_value=(200,{'data':{'user_id':1}})
            doc=self.create();descriptor=self.prepare(doc).get_json()
            self.assertEqual(identity.call_count,2)
            self.assertEqual(self.prepare(doc).status_code,200)
            self.assertEqual(identity.call_count,3)
            identity.return_value=(401,{})
            self.assertEqual(self.prepare(doc).status_code,401)
            self.assertEqual(self.client.get(descriptor['bundle']['path'],headers={'Authorization':'x'}).status_code,401)
            self.assertEqual(identity.call_count,5)
            self.assertTrue(self.ledger.source_path(doc).is_file())
            identity.return_value=(503,{})
            self.assertEqual(self.prepare(doc).status_code,503)
            self.assertTrue(self.ledger.source_path(doc).is_file())

    def test_unlink_failure_retains_owner_and_disk_charge_until_reconcile(self):
        doc=self.create();descriptor=self.prepare(doc).get_json()
        path=self.ledger.scene_path(doc,descriptor['revision'])
        original=Path.unlink
        def fail_scene_unlink(candidate,*args,**kwargs):
            if candidate==path:raise OSError('fixture disk busy')
            return original(candidate,*args,**kwargs)
        with patch.object(Path,'unlink',side_effect=fail_scene_unlink,autospec=True):
            self.assertEqual(self.client.delete(f'{BASE}/{doc}',headers={'Authorization':'x'}).status_code,204)
            with self.ledger.transaction() as db:
                row=db.execute('SELECT budget,derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()
                self.assertIsNotNone(row);self.assertGreater(row['derived_bytes'],0)
                self.assertGreater(row['budget'],0)
        self.ledger.reconcile()
        self.assertFalse(path.exists())
        with self.ledger.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)

    def test_owner_global_and_renderer_guards_also_cover_scene_and_download(self):
        from app.projection_store import ProjectionRejected
        doc=self.create()
        self.ledger.policy.owner_derived_bytes=self.ledger.policy.derived_max_bytes-1
        self.assertEqual(self.prepare(doc).status_code,429)
        self.ledger.policy.owner_derived_bytes=128*1024**2
        self.ledger.policy.global_disk_bytes=self.ledger.policy.system_bytes+1
        self.assertEqual(self.prepare(doc).status_code,429)
        self.ledger.policy.global_disk_bytes=2*1024**3
        descriptor=self.prepare(doc).get_json()
        response=self.client.get(descriptor['bundle']['path'],headers={'Authorization':'x'},buffered=False)
        self.assertEqual(response.status_code,200)
        second=self.client.get(descriptor['bundle']['path'],headers={'Authorization':'x'})
        self.assertEqual(second.status_code,429)
        with self.assertRaises(ProjectionRejected):self.ledger.reserve(self.owner,'tile',doc,0)
        response.close()
        with self.ledger.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_full_page_full_switch_reuses_each_variant_and_charges_all_zips(self):
        from app.projection_runtime import run as original
        doc=self.create(pdf_bytes(2))
        response=self.client.post(f'{BASE}/{doc}/layout',headers={'Authorization':'x'},data={'method':'manual','rows':1,'columns':2})
        self.assertEqual(response.status_code,200)
        calls=[]
        def record(*args,**kwargs):
            if args[2]=='scene':calls.append(args[2])
            return original(*args,**kwargs)
        with patch('app.projection_runtime.run',side_effect=record):
            full=self.prepare(doc,view='layout').get_json()
            page=self.prepare(doc,view='page',page_index=1).get_json()
            reused=self.prepare(doc,view='layout').get_json()
        self.assertEqual(calls,['scene','scene'])
        self.assertEqual(reused['revision'],full['revision'])
        self.assertNotEqual(page['revision'],full['revision'])
        self.assertEqual(full['view'],'layout');self.assertNotIn('page_index',full)
        self.assertEqual(page['view'],'page');self.assertEqual(page['page_index'],1)
        self.assertEqual(page['width_mm'],self.metadata(doc)['pages'][1]['width_mm'])
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],full['bundle']['bytes']+page['bundle']['bytes'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles WHERE document=?',(doc,)).fetchone()[0],2)
        # Explicit page access does not require applying any layout and includes
        # the original full page. Normal multi-page default still requires it.
        new=self.create(pdf_bytes(2))
        self.assertEqual(self.prepare(new).status_code,409)
        direct=self.prepare(new,view='page',page_index=0)
        self.assertEqual(direct.status_code,200,direct.get_json())
        self.assertEqual(direct.get_json()['page_index'],0)

    def test_variant_validation_and_old_fd_only_pins_its_own_revision(self):
        doc=self.create(pdf_bytes(2))
        for options in ({'view':'nope'},{'view':'page','page_index':'1.5'},
                        {'view':'page','page_index':-1},{'view':'page','page_index':2}):
            self.assertEqual(self.prepare(doc,**options).status_code,400)
        self.assertEqual(self.prepare(doc,view='layout').status_code,409)
        self.assertEqual(self.client.post(f'{BASE}/{doc}/layout',headers={'Authorization':'x'},data={'method':'manual','rows':1,'columns':2}).status_code,200)
        full=self.prepare(doc,view='layout').get_json()
        page=self.prepare(doc,view='page',page_index=1).get_json()
        self.assertEqual(self.prepare(doc,view='layout',page_index=1).status_code,400)
        response=self.client.get(page['bundle']['path'],headers={'Authorization':'x'},buffered=False)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.post(f'{BASE}/{doc}/layout',headers={'Authorization':'x'},data={'method':'manual','rows':2,'columns':1}).status_code,200)
        self.assertFalse(self.ledger.scene_path(doc,full['revision']).exists())
        self.assertTrue(self.ledger.scene_path(doc,page['revision']).exists())
        self.assertEqual(self.client.get(full['bundle']['path'],headers={'Authorization':'x'}).status_code,404)
        self.assertEqual(self.client.get(page['bundle']['path'],headers={'Authorization':'x'}).status_code,404)
        response.close()
        self.assertFalse(self.ledger.scene_path(doc,page['revision']).exists())
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],0)

    def test_legacy_one_bundle_shape_migrates_additively_once(self):
        from app.projection_store import Store
        doc=self.create();descriptor=self.prepare(doc).get_json()
        with self.ledger.transaction() as db:
            db.execute('''CREATE TABLE scenes(document TEXT,generation INTEGER,state TEXT,revision TEXT,lease TEXT,
              descriptor TEXT,budget INTEGER,derived_bytes INTEGER)''')
            db.execute('''INSERT INTO scenes SELECT document,generation,state,revision,lease,descriptor,budget,derived_bytes
              FROM scene_bundles''')
            db.execute('DELETE FROM scene_bundles')
            db.execute('DELETE FROM scene_schema_migrations')
        restored=Store(self.ledger.root,self.ledger.policy)
        from app.projection_store import ProjectionRejected
        with self.assertRaises(ProjectionRejected):restored.scene_snapshot(doc,self.owner,0,'page:0')
        self.assertFalse(restored.scene_path(doc,descriptor['revision']).exists())
        self.assertTrue(restored.source_path(doc).is_file())
        self.assertTrue(restored.delete(doc,self.owner))
        restored=Store(self.ledger.root,self.ledger.policy)
        with restored.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles').fetchone()[0],0)

    def test_pipeline_version_revokes_ready_keeps_download_charge_and_reuses_new_revision(self):
        from app.projection_store import ProjectionRejected
        doc=self.create()
        self.ledger.policy.owner_renderers=2
        with patch('app.projection_scene_store.SCENE_PIPELINE_VERSION','previous-exporter',create=True):
            first=self.prepare(doc).get_json()
            old=self.ledger.scene_snapshot(doc,self.owner,0,'page:0')
            file,lease,_=self.ledger.scene_open(doc,self.owner,first['revision'])
        old_path=self.ledger.scene_path(doc,first['revision'])
        try:
            with self.assertRaises(ProjectionRejected):self.ledger.scene_descriptor(doc,self.owner,old)
            with self.assertRaises(ProjectionRejected):self.ledger.scene_open(doc,self.owner,first['revision'])
            with self.ledger.transaction() as db:
                self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],first['bundle']['bytes'])
            self.assertTrue(old_path.exists())
            second=self.prepare(doc)
            self.assertEqual(second.status_code,200,second.get_json())
            second=second.get_json()
            self.assertNotEqual(first['revision'],second['revision'])
            with patch('app.projection_runtime.run',side_effect=AssertionError('same pipeline rendered again')):
                self.assertEqual(self.prepare(doc).get_json()['revision'],second['revision'])
                downloaded=self.client.get(second['bundle']['path'],headers={'Authorization':'x'})
                self.assertEqual(downloaded.status_code,200);downloaded.close()
            with self.ledger.transaction() as db:
                self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],first['bundle']['bytes']+second['bundle']['bytes'])
            self.assertTrue(file.read(1))
        finally:
            file.close();self.ledger.scene_close(lease)
        self.assertFalse(old_path.exists())
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],second['bundle']['bytes'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles WHERE document=?',(doc,)).fetchone()[0],1)

    def test_pipeline_change_without_reader_reclaims_old_budget_before_new_reservation(self):
        doc=self.create();source=self.ledger.source_path(doc).read_bytes()
        self.ledger.policy.owner_derived_bytes=self.ledger.policy.derived_max_bytes
        with patch('app.projection_scene_store.SCENE_PIPELINE_VERSION','previous-exporter',create=True):
            first=self.prepare(doc).get_json()
        second=self.prepare(doc)
        self.assertEqual(second.status_code,200,second.get_json())
        second=second.get_json()
        self.assertNotEqual(first['revision'],second['revision'])
        self.assertFalse(self.ledger.scene_path(doc,first['revision']).exists())
        self.assertEqual(self.ledger.source_path(doc).read_bytes(),source)
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],second['bundle']['bytes'])

    def test_pipeline_change_rejects_inflight_publication_without_replacing_source(self):
        from app.projection_store import ProjectionRejected
        doc=self.create();source=self.ledger.source_path(doc).read_bytes()
        meta=self.ledger.load(doc,self.owner)
        layout={'width_mm':meta['width_mm'],'height_mm':meta['height_mm']}
        with patch('app.projection_scene_store.SCENE_PIPELINE_VERSION','previous-exporter',create=True):
            pending,leader=self.ledger.scene_begin(doc,self.owner,0,'page:0')
        self.assertTrue(leader)
        lease=pending['lease'];folder=self.ledger.lease_path(lease);folder.mkdir(exist_ok=True)
        bundle=folder/'scene.zip'
        with zipfile.ZipFile(bundle,'w') as archive:archive.writestr('scene.json',b'{}')
        from app.projection_scene_store import validate_bundle
        result={**validate_bundle(bundle,self.ledger.policy.derived_max_bytes),'source_sha256':hashlib.sha256(source).hexdigest()}
        self.ledger.policy.owner_renderers=2
        latest,leader=self.ledger.scene_begin(doc,self.owner,0,'page:0')
        self.assertTrue(leader)
        try:
            with self.assertRaises(ProjectionRejected) as failed:
                self.ledger.scene_publish(doc,self.owner,0,'page:0',lease,meta,layout,result)
            self.assertEqual(failed.exception.status,409)
            self.assertEqual(self.ledger.scene_snapshot(doc,self.owner,0,'page:0')['lease'],latest['lease'])
        finally:
            self.ledger.scene_failed(doc,0,'page:0',lease);self.ledger.release(lease)
            self.ledger.scene_failed(doc,0,'page:0',latest['lease']);self.ledger.release(latest['lease'])
        self.assertEqual(self.ledger.source_path(doc).read_bytes(),source)
        self.assertEqual(list((self.ledger.root/'documents').glob('*.zip')),[])

    def test_pre_pipeline_schema_migration_preserves_leased_rows_and_is_idempotent(self):
        from app.projection_store import Store, ProjectionRejected
        doc=self.create();descriptor=self.prepare(doc).get_json()
        file,lease,_=self.ledger.scene_open(doc,self.owner,descriptor['revision'])
        try:
            with self.ledger.transaction() as db:
                db.execute('ALTER TABLE scene_bundles RENAME TO fixture_current_scenes')
                db.execute("""CREATE TABLE scene_bundles(document TEXT NOT NULL,generation INTEGER NOT NULL,
                  variant TEXT NOT NULL,state TEXT NOT NULL,revision TEXT,lease TEXT,descriptor TEXT,
                  budget INTEGER NOT NULL DEFAULT 0,derived_bytes INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(document,generation,variant))""")
                db.execute('INSERT INTO scene_bundles SELECT document,generation,variant,state,revision,lease,descriptor,budget,derived_bytes FROM fixture_current_scenes')
                db.execute('DROP TABLE fixture_current_scenes')
            stores=[];errors=[]
            def reopen():
                try:stores.append(Store(self.ledger.root,self.ledger.policy))
                except BaseException as error:errors.append(error)
            threads=[threading.Thread(target=reopen) for _ in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(5);self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[]);self.assertEqual(len(stores),2)
            restored=Store(self.ledger.root,self.ledger.policy)
            with restored.transaction() as db:
                columns={row['name']:row['pk'] for row in db.execute('PRAGMA table_info(scene_bundles)')}
                self.assertEqual(columns['pipeline'],4)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],descriptor['bundle']['bytes'])
            with self.assertRaises(ProjectionRejected):restored.scene_open(doc,self.owner,descriptor['revision'])
            self.assertTrue(file.read(1))
        finally:
            file.close();self.ledger.scene_close(lease)
        self.assertFalse(self.ledger.scene_path(doc,descriptor['revision']).exists())
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scene_bundles').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT derived_bytes FROM sessions WHERE id=?',(doc,)).fetchone()[0],0)

    def test_scene_uses_supervisor_limits_and_small_metadata_channel(self):
        import subprocess
        original=subprocess.Popen
        seen=[]
        def record(arguments,*args,**kwargs):
            spec=json.loads(Path(arguments[-1]).read_text())
            if spec['operation']=='scene':seen.append(spec)
            return original(arguments,*args,**kwargs)
        doc=self.create()
        with patch('app.projection_runtime.subprocess.Popen',side_effect=record):
            response=self.prepare(doc)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(len(seen),1)
        spec=seen[0]
        self.assertEqual(spec['limits']['file'],64*1024**2)
        self.assertEqual(spec['limits']['cpu'],self.ledger.policy.child_cpu_seconds)
        self.assertEqual(spec['limits']['memory'],self.ledger.policy.child_memory_bytes)
        self.assertEqual(Path(spec['output']).name,'result.json')
        self.assertEqual(set(spec['parameters']),{'layout','derived_max_bytes'})

    def test_global_native_slots_are_shared_by_tile_scene_and_download(self):
        first=self.create();self.owner='user:2';second=self.create();self.owner='user:3';third=self.create()
        first_lease=self.ledger.reserve('user:1','tile',first,0)
        second_scene,_=self.ledger.scene_begin(second,'user:2',0,'page:0')
        try:
            response=self.prepare(third)
            self.assertEqual(response.status_code,429,response.get_json())
        finally:
            self.ledger.release(first_lease)
            self.ledger.scene_failed(second,0,'page:0',second_scene['lease'])
            self.ledger.release(second_scene['lease'])

    def test_static_bundle_validation_rejects_unsafe_names_wrong_asset_hash_and_bombs(self):
        from app.projection_scene_store import validate_bundle
        from app.projection_store import ProjectionRejected
        cases=[{'scene.json':b'{}','../secret':b'x'},
               {'scene.json':b'{}','assets/'+('0'*64)+'.png':b'wrong hash'},
               {'scene.json':b'x'*8193}]
        for index,members in enumerate(cases):
            archive_path=Path(self.temp.name)/f'malicious-{index}.zip'
            with zipfile.ZipFile(archive_path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                for name,data in members.items():archive.writestr(name,data)
            with self.assertRaises(ProjectionRejected):validate_bundle(archive_path,8192)

    def test_scene_disk_bucket_charges_zip_exactly_once(self):
        doc=self.create();descriptor=self.prepare(doc).get_json()
        self.assertTrue(self.ledger.scene_path(doc,descriptor['revision']).is_file())
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
            session_budget=db.execute('SELECT SUM(budget) FROM sessions').fetchone()[0]
            used,_=self.ledger._disk_accounting(db)
        # The actual ZIP block allocation already resides in session_budget.
        # Treating doc.rev.zip as an orphan would add it a second time here.
        self.assertEqual(used,self.ledger.policy.system_bytes+self.ledger.policy.render_pool_bytes+session_budget)

    def test_projector_image_copy_allowlist_includes_both_scene_modules(self):
        dockerfile=(Path(__file__).resolve().parents[1]/'Dockerfile.projection').read_text()
        app_copy=[line.split() for line in dockerfile.splitlines() if line.startswith('COPY app/__init__.py ')][0]
        self.assertIn('app/projection_scene.py',app_copy)
        self.assertIn('app/projection_scene_store.py',app_copy)
        self.assertEqual(app_copy[-1],'./app/')
        self.assertEqual(dockerfile.count('FROM ${PROJECTOR_BASE_IMAGE}'),1)
        self.assertIn('EXPOSE 80',dockerfile)

if __name__=='__main__':unittest.main()
