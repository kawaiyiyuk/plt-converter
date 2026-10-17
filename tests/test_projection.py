import sys
import io
import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf as fitz
from app.projection_app import create_projection_app
from app import projection, projection_app, projection_layout, projection_store, projection_runtime
from app.billing import BillingRejected

BASE = '/api/v1/pdf/projection/documents'


def pdf_bytes(pages=1, rotation=0, crop=False, userunit=1):
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=200, height=300)
        page.draw_line((40, 40), (140, 40), color=(0, 0, 0), width=3)
        page.insert_text((40, 100), 'Projection text')
        if crop:
            page.set_cropbox(fitz.Rect(20, 20, 180, 280))
        page.set_rotation(rotation)
        if userunit != 1:
            doc.xref_set_key(page.xref, 'UserUnit', str(userunit))
    result = doc.tobytes() if pages else b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF'
    doc.close()
    return result



def synthetic_policy(root):
    values = {'OWNER_MAX_DOCUMENTS':2,'OWNER_SOURCE_BYTES':40*1024*1024,
              'GLOBAL_DISK_BYTES':2*1024**3,'FREE_FLOOR_BYTES':2*1024**3,
              'UPLOAD_OWNER_RATE':5,'UPLOAD_IP_RATE':30,'TILE_RATE':3,'TILE_BURST':5,
              'OWNER_RENDERERS':1,'GLOBAL_RENDERERS':2,'GLOBAL_PARSERS':1,
              'PARSE_WALL_SECONDS':90,'TILE_WALL_SECONDS':30,'CHILD_CPU_SECONDS':30,
              'CHILD_MEMORY_BYTES':512*1024*1024 if sys.platform.startswith('linux') else 1024**4}
    return {'PROJECTOR_ENABLED':'1','PROJECTOR_ROOT':root,'PROJECTOR_REDIS_URL':'redis://localhost:6399/0',
            **{'PROJECTOR_'+key:str(value) for key,value in values.items()}}

class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'PROJECTOR_ROOT': self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.configuration = synthetic_policy(self.temp.name)
        self.app = create_projection_app(self.configuration)
        self.ledger = self.app.extensions['projection_store']
        self.ledger.policy.owner_max_documents = 100
        self.ledger.policy.owner_source_bytes = 100*20*1024*1024
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        identity = patch('app.projection.identify_user', side_effect=self.identity)
        identity.start()
        self.addCleanup(identity.stop)
        self.owner = 'user:1'
        limits = patch.object(self.ledger, 'rate')
        limits.start()
        self.addCleanup(limits.stop)

    def metadata(self,document):
        with self.ledger.transaction() as db:
            row=db.execute('SELECT * FROM sessions WHERE id=?',(document,)).fetchone()
        return self.ledger.read_metadata(row) if row else None

    def execute_analysis(self,document,analysis_id,mode):
        from app.projection_layout import execute_layout_analysis
        meta=self.metadata(document)
        lease=(meta.get('analysis') or {}).get('lease') if meta else 'missing'
        return execute_layout_analysis(document,self.owner,str(self.ledger.root),analysis_id,mode,self.configuration,lease)

    def identity(self, auth):
        if not auth:
            raise BillingRejected('请先登录', 401)
        return self.owner

    def upload(self, source=None, filename='pattern.pdf'):
        response=self.client.post(BASE,headers={'Authorization':'Bearer fixture'},
                    data={'file':(io.BytesIO(source or pdf_bytes()),filename)})
        # Werkzeug's test client may spool its *input fixture* body; the real
        # HTTP server owns that transport. Close it explicitly in unit tests.
        response.request.environ['wsgi.input'].close()
        return response

    def tile(self, doc_id, **options):
        params = {'x_mm': 0, 'y_mm': 0, 'width_mm': 200 * 25.4 / 72,
                  'height_mm': 300 * 25.4 / 72, 'width_px': 200, 'height_px': 300}
        params.update(options)
        return self.client.get(f'{BASE}/{doc_id}/tile', query_string=params,
                               headers={'Authorization': 'Bearer fixture'})

    def test_upload_and_tile_preserve_bytes_dimensions_and_content(self):
        source = pdf_bytes()
        response = self.upload(source)
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body['page_count'], 1)
        self.assertEqual(body['expires_in'], 3600)
        self.assertAlmostEqual(body['width_mm'], 200 * 25.4 / 72)
        self.assertEqual((Path(self.temp.name) / 'documents' / (body['document_id'] + '.pdf')).read_bytes(), source)
        tile = self.tile(body['document_id'])
        self.assertEqual(tile.status_code, 200)
        pix = fitz.Pixmap(tile.data)
        self.assertEqual((pix.width, pix.height, pix.n), (200, 300, 3))
        self.assertEqual(pix.pixel(80, 40), (0, 0, 0))
        self.assertEqual(pix.pixel(10, 10), (255, 255, 255))
        self.assertIn('no-store', tile.headers['Cache-Control'])

    def test_rotated_crop_userunit_dimensions_and_render(self):
        source = pdf_bytes(rotation=90, crop=True, userunit=2)
        body = self.upload(source).get_json()
        with fitz.open(stream=source, filetype='pdf') as doc:
            rect = doc[0].rect
            self.assertAlmostEqual(body['width_mm'], rect.width * 25.4 / 72)
            self.assertAlmostEqual(body['height_mm'], rect.height * 25.4 / 72)
            response = self.tile(body['document_id'], width_mm=body['width_mm'], height_mm=body['height_mm'], width_px=round(rect.width), height_px=round(rect.height))
            actual = fitz.Pixmap(response.data)
            expected = doc[0].get_pixmap(alpha=False, colorspace=fitz.csRGB)
            self.assertEqual(actual.samples, expected.samples)

    def test_outside_page_is_white_and_negative_origin_keeps_position(self):
        doc_id = self.upload().get_json()['document_id']
        response = self.tile(doc_id, x_mm=-10 * 25.4 / 72, y_mm=-10 * 25.4 / 72)
        pix = fitz.Pixmap(response.data)
        self.assertEqual(pix.pixel(0, 0), (255, 255, 255))
        self.assertEqual(pix.pixel(90, 50), (0, 0, 0))
        response = self.tile(doc_id, x_mm=1000, y_mm=1000)
        self.assertEqual(set(fitz.Pixmap(response.data).samples), {255})

    def test_invalid_empty_and_encrypted_pdf_are_rejected(self):
        for source in (b'garbage', pdf_bytes(pages=0)):
            with self.subTest(source=source[:20]):
                self.assertEqual(self.upload(source).status_code, 400)
        self.assertEqual(self.upload(filename='file.txt').status_code, 415)
        doc = fitz.open(stream=pdf_bytes(), filetype='pdf')
        encrypted = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='secret')
        doc.close()
        self.assertEqual(self.upload(encrypted).status_code, 400)
        doc = fitz.open(stream=pdf_bytes(), filetype='pdf')
        owner_only = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='')
        doc.close()
        self.assertEqual(self.upload(owner_only).status_code, 400)

    def test_auth_owner_expiry_and_close(self):
        self.assertEqual(self.client.post(BASE).status_code, 401)
        body = self.upload().get_json()
        doc_id = body['document_id']
        self.owner = 'user:2'
        self.assertEqual(self.tile(doc_id).status_code, 404)
        self.assertEqual(self.client.delete(f'{BASE}/{doc_id}', headers={'Authorization': 'x'}).status_code, 404)
        self.owner = 'user:1'
        self.assertEqual(self.client.delete(f'{BASE}/{doc_id}', headers={'Authorization': 'x'}).status_code, 204)
        self.assertEqual(self.tile(doc_id).status_code, 404)
        body = self.upload().get_json()
        meta_path = Path(self.temp.name) / 'documents' / (body['document_id'] + '.json')
        meta = self.metadata(body['document_id'])
        with patch('app.projection.time.time', return_value=meta['expires_at']):
            self.assertEqual(self.tile(body['document_id']).status_code, 404)
        self.assertIsNone(self.metadata(body['document_id']))
        self.assertFalse(meta_path.with_suffix('.pdf').exists())

    def test_parameter_guards(self):
        doc_id = self.upload().get_json()['document_id']
        for options in ({'x_mm': 'nan'}, {'y_mm': 'inf'}, {'width_mm': 0}, {'height_mm': -1}, {'width_px': 2049}, {'width_px': 1.5}, {'width_px': 2000, 'height_px': 2000}, {'width_mm': 'nan'}, {'x_mm': '1e308'}, {'width_px': 2048, 'height_px': 2048, 'width_mm': 100, 'height_mm': 100}):
            with self.subTest(options=options):
                self.assertEqual(self.tile(doc_id, **options).status_code, 400)

    def test_size_and_complexity_limits(self):
        self.app.config['PLT_MAX_UPLOAD_BYTES'] = 100
        self.assertEqual(self.upload().status_code,413)
        self.app.config['PLT_MAX_UPLOAD_BYTES'] = 20*1024*1024
        # Complexity native validation is now in the child. Exercise its real
        # accepted PDF complexity boundary without an ineffective parent mock.
        with patch.dict(os.environ, {'PDF_MAX_TOTAL_PIXELS':'1000000'}):
            self.assertEqual(self.upload(pdf_bytes(pages=20)).status_code,400)

    def test_sqlite_writer_transaction_serializes_second_writer(self):
        # Short SQLite writer transactions now replace document/stripe locks.
        entered=threading.Event(); done=threading.Event()
        def second_writer():
            entered.set()
            with self.ledger.transaction() as db:
                db.execute('SELECT 1')
            done.set()
        with self.ledger.transaction():
            worker=threading.Thread(target=second_writer);worker.start()
            self.assertTrue(entered.wait(1));self.assertFalse(done.wait(.1))
        worker.join(2);self.assertTrue(done.is_set())

    def test_upload_rate_limit_returns_retry_after_and_get_is_independent(self):
        from app.projection_store import ProjectionRejected
        doc_id=self.upload().get_json()['document_id']
        def rate(owner,ip,kind):
            if kind=='upload':
                raise ProjectionRejected('projection_rate','上传过快',429,60)
        with patch.object(self.ledger,'rate',side_effect=rate):
            response=self.upload()
            self.assertEqual(response.status_code,429)
            self.assertEqual(response.headers['Retry-After'],'60')
            self.assertEqual(self.tile(doc_id).status_code,200)

    def test_sqlite_owner_upload_rate_limit_rejects_second_upload(self):
        # Projection rate records are now bounded SQLite rows, owner keyed;
        # Redis availability has no influence on renew/delete.
        from app.projection_store import Store,ProjectionRejected
        self.ledger.policy.upload_owner_rate=1
        with patch.object(self.ledger,'rate',side_effect=lambda *args: Store.rate(self.ledger,*args)):
            first=self.upload();self.assertEqual(first.status_code,201)
            second=self.upload();self.assertEqual(second.status_code,429)
            self.assertGreater(int(second.headers['Retry-After']),0)
            self.assertEqual(self.tile(first.get_json()['document_id']).status_code,200)

    def test_without_renewal_session_expires_after_sixty_minutes_and_get_does_not_renew(self):
        start = 1_000_000
        with patch('app.projection.time.time', return_value=start):
            response = self.upload()
        self.assertEqual(response.status_code, 201)
        doc_id = response.get_json()['document_id']
        for elapsed in (1800, 3599):
            with self.subTest(elapsed=elapsed), patch('app.projection.time.time', return_value=start + elapsed):
                tile = self.tile(doc_id)
                self.assertEqual(tile.status_code, 200)
                pixmap = fitz.Pixmap(tile.data)
                self.assertEqual((pixmap.width, pixmap.height, pixmap.n), (200, 300, 3))
                self.assertEqual(pixmap.pixel(80, 40), (0, 0, 0))
        with patch('app.projection.time.time', return_value=start + 3600):
            self.assertEqual(self.tile(doc_id).status_code, 404)
        self.assertEqual(response.get_json()['expires_in'], 3600)

    def renew(self, doc_id, authenticated=True):
        return self.client.post(f'{BASE}/{doc_id}/renew', headers={'Authorization': 'Bearer fixture'} if authenticated else {})

    def test_renewal_keeps_real_pdf_available_for_three_hours_then_expires(self):
        start = 1_000_000
        source = pdf_bytes()
        with patch('app.projection.time.time', return_value=start):
            body = self.upload(source).get_json()
        doc_id = body['document_id']
        root = Path(self.temp.name) / 'documents'
        original = self.metadata(doc_id)
        with patch.object(self.ledger, 'rate') as upload_quota:
            for elapsed in range(300, 10801, 300):
                with patch('app.projection.time.time', return_value=start + elapsed):
                    response = self.renew(doc_id)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json(), {'document_id': doc_id, 'expires_in': 3600, 'rotation_supported': True, 'session_summary_supported': True})
                    self.assertIn('no-store', response.headers['Cache-Control'])
                    tile = self.tile(doc_id)
                    self.assertEqual(tile.status_code, 200)
                    self.assertEqual(fitz.Pixmap(tile.data).pixel(80, 40), (0, 0, 0))
                meta = self.metadata(doc_id)
                self.assertEqual(meta['expires_at'], start + elapsed + 3600)
                self.assertEqual({k: v for k, v in meta.items() if k != 'expires_at'}, {k: v for k, v in original.items() if k != 'expires_at'})
            self.assertTrue(all(call.args[-1] == 'tile' for call in upload_quota.call_args_list))
        # The projection path must never reach legacy billing/queue code; patch
        # assertions on app.billing/app.routes cannot fire because those modules
        # are not imported by the projection package, so assert that fact instead.
        for name in ('authorize_conversion', 'commit_conversion'):
            for module in (projection, projection_app, projection_layout, projection_store, projection_runtime):
                self.assertNotIn(name, inspect.getsource(module))
        self.assertEqual((root / f'{doc_id}.pdf').read_bytes(), source)
        with patch('app.projection.time.time', return_value=start + 10800 + 3599):
            self.assertEqual(self.tile(doc_id).status_code, 200)
            self.assertEqual(self.metadata(doc_id)['expires_at'], start + 10800 + 3600)
        with patch('app.projection.time.time', return_value=start + 10800 + 3600):
            self.assertEqual(self.renew(doc_id).status_code, 404)
            self.assertEqual(self.tile(doc_id).status_code, 404)

    def test_renewal_identity_deadline_and_deletion_cannot_revive(self):
        start = 1_000_000
        with patch('app.projection.time.time', return_value=start):
            doc_id = self.upload().get_json()['document_id']
        meta_path = Path(self.temp.name) / 'documents' / f'{doc_id}.json'
        original = json.dumps(self.metadata(doc_id)).encode()
        with patch('app.projection.time.time', return_value=start + 100):
            self.assertEqual(self.renew(doc_id, authenticated=False).status_code, 401)
            self.owner = 'user:2'
            self.assertEqual(self.renew(doc_id).status_code, 404)
            self.assertEqual(json.dumps(self.metadata(doc_id)).encode(), original)
            self.owner = 'user:1'
            self.assertEqual(self.renew('../invalid').status_code, 404)
        with patch('app.projection.time.time', return_value=start + 3599):
            self.assertEqual(self.renew(doc_id).status_code, 200)
        with patch('app.projection.time.time', return_value=start + 3600):
            self.assertEqual(self.tile(doc_id).status_code, 200)
            self.assertEqual(self.client.delete(f'{BASE}/{doc_id}', headers={'Authorization': 'x'}).status_code, 204)
            self.assertEqual(self.renew(doc_id).status_code, 404)
            expired = self.upload().get_json()['document_id']
        with patch('app.projection.time.time', return_value=start + 7200):
            self.assertEqual(self.renew(expired).status_code, 404)
            self.assertEqual(self.tile(expired).status_code, 404)

    def test_renewal_atomic_metadata_write_failure_preserves_session(self):
        import hashlib
        import math
        import sqlite3
        doc_id=self.upload().get_json()['document_id']
        before=self.metadata(doc_id)
        source_hash=hashlib.sha256(self.ledger.source_path(doc_id).read_bytes()).hexdigest()
        cached=self.tile(doc_id).data
        connect=sqlite3.connect
        denied=[]
        def denied_expiry_connection(*args,**kwargs):
            connection=connect(*args,**kwargs)
            def authorizer(action,table,column,database,trigger):
                if action==sqlite3.SQLITE_UPDATE and table=='sessions' and column=='expires':
                    denied.append((table,column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorizer)
            return connection
        now=before['expires_at']-3600+30
        with patch('app.projection.time.time',return_value=now):
            # Execute actual Store.renew/BEGIN IMMEDIATE/_load/UPDATE; only
            # SQLite's authorizer rejects this column write. No renew mock.
            with patch('app.projection_store.sqlite3.connect',side_effect=denied_expiry_connection):
                response=self.renew(doc_id)
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.get_json()['error_code'],'projection_storage_unavailable')
            self.assertIn('no-store',response.headers['Cache-Control'])
            self.assertEqual(denied,[('sessions','expires')])
            self.assertEqual(self.metadata(doc_id),before)
            summary=self.client.get(f'{BASE}/{doc_id}',headers={'Authorization':'x'})
            self.assertEqual(summary.status_code,200)
            self.assertEqual(summary.get_json()['expires_in'],math.ceil(before['expires_at']-now))
            self.assertEqual(hashlib.sha256(self.ledger.source_path(doc_id).read_bytes()).hexdigest(),source_hash)
            self.assertEqual(self.tile(doc_id).data,cached)
            self.assertEqual(self.renew(doc_id).status_code,200)
            self.assertEqual(self.metadata(doc_id)['expires_at'],now+3600)

    def test_renewal_and_delete_serialized_in_both_orders(self):
        for first in ('renew','delete'):
            doc_id=self.upload().get_json()['document_id']
            if first=='renew':
                self.assertEqual(self.renew(doc_id).status_code,200)
            self.assertEqual(self.client.delete(f'{BASE}/{doc_id}',headers={'Authorization':'x'}).status_code,204)
            self.assertEqual(self.renew(doc_id).status_code,404)
            self.assertIsNone(self.metadata(doc_id))
            self.assertFalse(self.ledger.source_path(doc_id).exists())

    def test_multiple_pages_upload_and_explicit_rotated_page_tile(self):
        doc = fitz.open()
        first = doc.new_page(width=200, height=300)
        first.draw_line((40, 40), (140, 40), width=3)
        second = doc.new_page(width=100, height=150)
        second.draw_line((20, 30), (80, 30), color=(1, 0, 0), width=3)
        second.insert_text((20, 70), 'Page two')
        annot = second.add_line_annot((20, 110), (80, 110))
        annot.set_colors(stroke=(0, 0, 1)); annot.update()
        second.set_rotation(90)
        source = doc.tobytes()
        expected = second.get_pixmap(colorspace=fitz.csRGB, alpha=False)
        doc.close()
        response = self.upload(source)
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body['page_count'], 2)
        self.assertEqual([p['index'] for p in body['pages']], [0, 1])
        self.assertFalse(body['requires_metadata_choice'])
        self.assertAlmostEqual(body['pages'][1]['width_mm'], 150 * 25.4 / 72)
        tile = self.tile(body['document_id'], page_index=1, width_mm=150 * 25.4 / 72, height_mm=100 * 25.4 / 72, width_px=150, height_px=100)
        self.assertEqual(tile.status_code, 200)
        self.assertEqual(fitz.Pixmap(tile.data).samples, expected.samples)
        for index in (-1, 2, '1.5', 'nan'):
            self.assertEqual(self.tile(body['document_id'], page_index=index).status_code, 400)

    def test_pdf_page_limit_is_explicit_not_truncated(self):
        self.assertEqual(self.upload(pdf_bytes(pages=200)).status_code, 201)
        response = self.upload(pdf_bytes(pages=201))
        self.assertEqual(response.status_code, 400)
        self.assertIn('200', response.get_json()['error'])

    def test_manual_layout_mixed_rotated_pages_preserves_visible_annotations(self):
        doc = fitz.open()
        first = doc.new_page(width=200, height=300)
        first.draw_line((40, 40), (140, 40), width=3)
        second = doc.new_page(width=100, height=150)
        second.draw_line((20, 30), (80, 30), color=(1, 0, 0), width=3)
        annot = second.add_line_annot((20, 110), (80, 110))
        annot.set_colors(stroke=(0, 0, 1)); annot.update()
        second.set_rotation(90)
        source = doc.tobytes(); doc.close()
        body = self.upload(source).get_json(); doc_id = body['document_id']
        response = self.client.post(f'{BASE}/{doc_id}/layout', headers={'Authorization': 'x'}, data={'method': 'manual', 'rows': 1, 'columns': 2, 'order': 'row', **{f'crop_{side}_mm': 10 * 25.4 / 72 for side in ('left', 'right', 'top', 'bottom')}})
        self.assertEqual(response.status_code, 200)
        layout = response.get_json()['layout']
        self.assertEqual(layout['page_slots'], [0, 1])
        self.assertAlmostEqual(layout['width_mm'], 330 * 25.4 / 72)
        self.assertAlmostEqual(layout['height_mm'], 300 * 25.4 / 72)
        tile = self.tile(doc_id, view='layout', width_mm=layout['width_mm'], height_mm=layout['height_mm'], width_px=330, height_px=300)
        self.assertEqual(tile.status_code, 200)
        pix = fitz.Pixmap(tile.data)
        self.assertEqual(pix.pixel(80, 40), (0, 0, 0))
        self.assertEqual(pix.pixel(300, 50), (255, 0, 0))
        self.assertGreater(pix.pixel(220, 50)[2], pix.pixel(220, 50)[0])
        self.assertEqual(pix.pixel(220, 200), (255, 255, 255))
        partial = self.tile(doc_id, view='layout', x_mm=170 * 25.4 / 72, width_mm=60 * 25.4 / 72, height_mm=100 * 25.4 / 72, width_px=60, height_px=100)
        self.assertEqual(fitz.Pixmap(partial.data).pixel(50, 50), pix.pixel(220, 50))
        root = Path(self.temp.name) / 'documents'
        self.assertEqual((root / f'{doc_id}.pdf').read_bytes(), source)

    def queue_environment(self):
        from contextlib import ExitStack
        import fakeredis
        stack = ExitStack()
        redis = fakeredis.FakeRedis()
        redis.lock = lambda *args, **kwargs: threading.RLock()
        stack.enter_context(patch('app.projection_layout.projection_connection', return_value=redis))
        stack.enter_context(patch('app.projection_layout.has_live_projection_worker', return_value=True))
        return stack, redis

    def analyze(self, doc_id, mode=None):
        return self.client.post(f'{BASE}/{doc_id}/layout-analysis', headers={'Authorization': 'x'}, data={} if mode is None else {'metadata_mode': mode})

    def analysis_status(self, doc_id):
        return self.client.get(f'{BASE}/{doc_id}/layout-analysis', headers={'Authorization': 'x'})

    def reliable_suggestion(self):
        return {'rows': 1, 'columns': 2, 'order': 'row', 'page_slots': [0, 1], 'crop_margins_mm': {side: 0 for side in ('left', 'right', 'top', 'bottom')}, 'confidence': 'medium', 'source': 'vector_seams'}

    def test_analysis_independent_queue_idempotence_and_exact_auto_apply(self):
        from app.projection_layout import execute_layout_analysis, projection_queue
        body = self.upload(pdf_bytes(pages=2)).get_json(); doc_id = body['document_id']
        self.assertEqual(self.tile(doc_id, view='layout').status_code, 409)
        environment, redis = self.queue_environment()
        with environment:
            first = self.analyze(doc_id)
            self.assertEqual(first.status_code, 202)
            analysis_id = first.get_json()['analysis_id']
            self.assertEqual(self.analyze(doc_id).get_json()['analysis_id'], analysis_id)
            self.assertEqual(projection_queue(redis).count, 1)
            job = projection_queue(redis).fetch_job(analysis_id)
            self.assertEqual(job.timeout, 90)
            self.assertEqual(job.ttl, 3600)
            self.assertEqual(job.origin, 'pdf-projection-layout')
            self.assertNotIn(pdf_bytes(), job.args)
            with patch('app.projection_runtime.run', return_value=self.reliable_suggestion()):
                result = self.execute_analysis(doc_id, analysis_id, 'current')
            self.assertEqual(result['status'], 'ready')
            ready = self.analysis_status(doc_id).get_json()
            self.assertEqual(ready['analysis_id'], analysis_id)
            self.assertEqual(ready['suggestion']['confidence'], 'medium')
            for form in ({'method': 'auto'}, {'method': 'auto', 'analysis_id': 'stale'}):
                self.assertEqual(self.client.post(f'{BASE}/{doc_id}/layout', headers={'Authorization': 'x'}, data=form).status_code, 409)
            response = self.client.post(f'{BASE}/{doc_id}/layout', headers={'Authorization': 'x'}, data={'method': 'auto', 'analysis_id': analysis_id})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['layout']['page_slots'], [0, 1])
            self.assertEqual(self.tile(doc_id, view='layout').status_code, 200)

    def test_analysis_metadata_requires_explicit_choice_and_old_signature_adapter(self):
        from app.services.pdf_metadata import encode_pdf_layout_metadata
        from app.projection_layout import execute_layout_analysis
        metadata = {'version': 1, 'rows': 1, 'columns': 2, 'order': 'row', 'page_slots': [0, 1], 'crop_margins_mm': {side: 0 for side in ('left', 'right', 'top', 'bottom')}, 'drawing_width_mm': 400 * 25.4 / 72, 'drawing_height_mm': 300 * 25.4 / 72, 'complete_layout': True}
        with fitz.open(stream=pdf_bytes(pages=2), filetype='pdf') as doc:
            doc.set_metadata({'subject': encode_pdf_layout_metadata(metadata)})
            source = doc.tobytes()
        body = self.upload(source).get_json(); doc_id = body['document_id']
        self.assertTrue(body['requires_metadata_choice'])
        root = Path(self.temp.name) / 'documents'
        environment, redis = self.queue_environment()
        with environment:
            self.assertEqual(self.analyze(doc_id).status_code, 422)
            original_id = self.analyze(doc_id, 'original').get_json()['analysis_id']
            with patch('app.projection_layout.optimize_pdf_layout') as engine:
                result = self.execute_analysis(doc_id, original_id, 'original')
                engine.assert_not_called()
            self.assertEqual(result['status'], 'ready')
            current_id = self.analyze(doc_id, 'current').get_json()['analysis_id']
            def legacy_one_argument_engine(analysis_source):
                with fitz.open(stream=analysis_source, filetype='pdf') as analysis_doc:
                    self.assertEqual(analysis_doc.metadata['subject'], '')
                return {'confidence': 'low', 'reason': 'insufficient vector evidence'}
            with patch('app.projection_layout.optimize_pdf_layout', side_effect=legacy_one_argument_engine):
                from app.projection_layout import analyze_source
                suggestion = analyze_source(source, 'current', metadata)
                with patch('app.projection_runtime.run', return_value=suggestion):
                    result = self.execute_analysis(doc_id, current_id, 'current')
            self.assertEqual(result['status'], 'insufficient_evidence')
            self.assertEqual((root / f'{doc_id}.pdf').read_bytes(), source)
            self.assertEqual(self.tile(doc_id).status_code, 200)
            self.assertEqual(self.tile(doc_id, view='layout').status_code, 409)

    def test_analysis_late_publication_never_revives_delete_or_overrides_manual(self):
        from app.projection_layout import execute_layout_analysis
        for action in ('delete', 'manual', 'new_analysis'):
            with self.subTest(action=action):
                doc_id = self.upload(pdf_bytes(pages=2)).get_json()['document_id']
                root = Path(self.temp.name) / 'documents'
                environment, redis = self.queue_environment()
                with environment:
                    analysis_id = self.analyze(doc_id).get_json()['analysis_id']
                    def delayed_analysis(*args, **kwargs):
                        self.assertEqual(self.renew(doc_id).status_code, 200, 'heavy analysis must release document lock')
                        if action == 'delete':
                            self.assertEqual(self.client.delete(f'{BASE}/{doc_id}', headers={'Authorization': 'x'}).status_code, 204)
                        elif action == 'manual':
                            response = self.client.post(f'{BASE}/{doc_id}/layout', headers={'Authorization': 'x'}, data={'method': 'manual', 'rows': 2, 'columns': 1, 'order': 'column'})
                            self.assertEqual(response.status_code, 200)
                        else:
                            self.assertNotEqual(self.analyze(doc_id, 'original').get_json()['analysis_id'], analysis_id)
                        return self.reliable_suggestion()
                    with patch('app.projection_runtime.run', side_effect=delayed_analysis):
                        result = self.execute_analysis(doc_id, analysis_id, 'current')
                    self.assertEqual(result['status'], 'discarded')
                    if action == 'delete':
                        self.assertFalse((root / f'{doc_id}.json').exists())
                    elif action == 'manual':
                        meta = self.metadata(doc_id)
                        self.assertEqual(meta['layout']['rows'], 2)
                        self.assertNotIn('analysis', meta)
                    else:
                        self.assertNotEqual(self.analysis_status(doc_id).get_json()['analysis_id'], analysis_id)

    def test_manual_layout_validates_grid_crop_and_preserves_large_virtual_dimensions(self):
        doc_id = self.upload(pdf_bytes(pages=2)).get_json()['document_id']
        for options in ({'rows': 1, 'columns': 1}, {'rows': 25, 'columns': 1}, {'rows': 1.5, 'columns': 2}, {'rows': 1, 'columns': 2, 'order': 'bad'}, {'rows': 1, 'columns': 2, 'crop_left_mm': 'nan'}, {'rows': 1, 'columns': 2, 'crop_top_mm': -1}, {'rows': 1, 'columns': 2, 'crop_left_mm': 70}):
            response = self.client.post(f'{BASE}/{doc_id}/layout', headers={'Authorization': 'x'}, data={'method': 'manual', **options})
            self.assertEqual(response.status_code, 400)
        from app.projection_layout import build_layout
        pages = [{'index': 0, 'width_mm': 3000, 'height_mm': 10}, {'index': 1, 'width_mm': 3000, 'height_mm': 10}]
        layout = build_layout(pages, 1, 2, 'row', {'left': 101})
        self.assertEqual(layout['width_mm'], 5899)
        for slots in ([0, 0], [0, None], [0, 2]):
            with self.assertRaises(ValueError):
                build_layout(pages, 1, 2, 'row', {}, slots)
        blank = build_layout(pages, 1, 3, 'row', {})
        self.assertEqual(blank['page_slots'], [0, 1, None])
        self.assertEqual(blank['width_mm'], 9000)

    def test_analysis_queue_full_timeout_and_wrong_owner_preserve_original_pages(self):
        from app.projection_layout import projection_queue
        doc_id = self.upload(pdf_bytes(pages=2)).get_json()['document_id']
        environment, redis = self.queue_environment()
        with environment:
            with patch('app.projection_layout.projection_queue') as queue:
                queue.return_value.count = 20
                response = self.analyze(doc_id)
                self.assertEqual(response.status_code, 429)
                self.assertIn('Retry-After', response.headers)
            self.owner = 'user:2'
            self.assertEqual(self.analyze(doc_id).status_code, 404)
            self.assertEqual(self.analysis_status(doc_id).status_code, 404)
            self.owner = 'user:1'
            analysis_id = self.analyze(doc_id).get_json()['analysis_id']
            job = projection_queue(redis).fetch_job(analysis_id)
            with patch('app.projection_layout.Job.fetch', return_value=job), patch.object(job, 'get_status', return_value='failed'):
                response = self.analysis_status(doc_id)
            self.assertEqual(response.get_json()['status'], 'failed')
            self.assertEqual(self.tile(doc_id).status_code, 200)
            self.assertEqual(self.tile(doc_id, view='layout').status_code, 409)

    def test_scanned_pdf_low_evidence_still_browsable_and_manual_layout_supported(self):
        from app.projection_layout import execute_layout_analysis
        doc = fitz.open()
        image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False); image.clear_with(128)
        for _ in range(2):
            page = doc.new_page(width=200, height=300)
            page.insert_image(fitz.Rect(0, 0, 200, 300), pixmap=image)
        source = doc.tobytes(); doc.close()
        doc_id = self.upload(source).get_json()['document_id']
        environment, redis = self.queue_environment()
        with environment:
            analysis_id = self.analyze(doc_id).get_json()['analysis_id']
            result = self.execute_analysis(doc_id, analysis_id, 'current')
            self.assertEqual(result['status'], 'insufficient_evidence')
            tile = self.tile(doc_id, page_index=1)
            self.assertEqual(tile.status_code, 200)
            self.assertEqual(fitz.Pixmap(tile.data).pixel(100, 150), (128, 128, 128))
            response = self.client.post(f'{BASE}/{doc_id}/layout', headers={'Authorization': 'x'}, data={'method': 'manual', 'rows': 1, 'columns': 2, 'order': 'row'})
            self.assertEqual(response.status_code, 200)

    def test_missing_projection_worker_and_stale_analysis_fail_without_changing_view(self):
        from app.projection_layout import execute_layout_analysis, projection_queue
        doc_id = self.upload(pdf_bytes(pages=2)).get_json()['document_id']
        root = Path(self.temp.name) / 'documents'
        environment, redis = self.queue_environment()
        with environment:
            with patch('app.projection_layout.has_live_projection_worker', return_value=False):
                response = self.analyze(doc_id)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(self.tile(doc_id).status_code, 200)
                self.assertNotIn('analysis', self.metadata(doc_id))
            first_id = self.analyze(doc_id).get_json()['analysis_id']
            with patch('app.projection_layout.has_live_projection_worker', return_value=False):
                self.assertEqual(self.analysis_status(doc_id).get_json()['status'], 'failed')
                self.assertEqual(projection_queue(redis).fetch_job(first_id).get_status(), 'canceled')
                self.assertEqual(self.analyze(doc_id).status_code, 503)
            self.assertEqual(self.execute_analysis(doc_id, first_id, 'current')['status'], 'discarded')
            retried_id = self.analyze(doc_id).get_json()['analysis_id']
            self.assertNotEqual(retried_id, first_id)
            from datetime import datetime, timedelta, timezone
            job = projection_queue(redis).fetch_job(retried_id)
            job.started_at = datetime.now(timezone.utc) - timedelta(seconds=95)
            with patch('app.projection_layout.Job.fetch', return_value=job), patch.object(job, 'get_status', return_value='started'):
                self.assertEqual(self.analysis_status(doc_id).get_json()['status'], 'failed')
            self.assertEqual(self.tile(doc_id).status_code, 200)

    def test_projection_worker_liveness_requires_only_own_queue_and_fresh_heartbeat(self):
        from app.projection_layout import has_live_projection_worker
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace
        worker = SimpleNamespace(refresh=lambda: None, queues=[SimpleNamespace(name='pdf-projection-layout')], last_heartbeat=datetime.now(timezone.utc), get_state=lambda: 'idle', worker_ttl=420)
        with patch('app.projection_layout.Worker.all', return_value=[worker]):
            self.assertTrue(has_live_projection_worker(connection=object()))
            worker.queues.append(SimpleNamespace(name='conversions'))
            self.assertFalse(has_live_projection_worker(connection=object()))
            worker.queues.pop()
            worker.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=421)
            self.assertFalse(has_live_projection_worker(connection=object()))
