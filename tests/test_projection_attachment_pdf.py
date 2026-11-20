import io
import pickle
import unittest
import pymupdf as fitz
from unittest.mock import patch
from flask import g
from app.projection import ProjectionIdentity, identify_user as identify_projection_user
import test_projection as fixtures
from test_projection_pdf_validation_proof import HEADER, KEY, proof

ATTACHMENT_DOCUMENTS = '/api/v1/pdf/projection/attachment-documents'


class AttachmentPDFTests(unittest.TestCase):
    setUp = fixtures.ProjectionTests.setUp
    metadata = fixtures.ProjectionTests.metadata
    upload = fixtures.ProjectionTests.upload
    def identity(self, auth):
        owner = fixtures.ProjectionTests.identity(self, auth)
        g.projection_client_scope = 'wx_mini_program' if auth == 'Bearer mobile' else None
        return owner

    def validate(self, raw):
        self.app.config['PROJECTOR_PDF_VALIDATION_KEY'] = KEY
        return self.client.post('/api/v1/pdf/projection/validate', headers={HEADER: proof(raw)},
                                data={'file': (io.BytesIO(raw), 'pattern.pdf')})

    def test_identity_scope_stays_request_local_when_queued(self):
        identity = ProjectionIdentity('user:1', 'wx_mini_program')
        with self.app.test_request_context(headers={'Authorization': 'Bearer mobile'}), \
             patch('app.projection._identify_user', return_value=identity):
            owner = identify_projection_user('Bearer mobile')
            self.assertEqual(g.projection_client_scope, 'wx_mini_program')
            self.assertEqual(pickle.loads(pickle.dumps(owner)), 'user:1')
            self.assertIs(type(owner), str)

    def test_validation_retains_active_document(self):
        doc = self.upload().get_json()['document_id']
        response = self.validate(fixtures.pdf_bytes(2))
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['page_count'],2)
        self.assertIsNotNone(self.metadata(doc))
        self.assertEqual(self.validate(b'broken').status_code,400)
        self.assertIsNotNone(self.metadata(doc))
        encrypted = fitz.open(stream=fixtures.pdf_bytes(), filetype='pdf')
        data = encrypted.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='user')
        encrypted.close()
        self.assertEqual(self.validate(data).status_code, 400)
        self.assertEqual(self.validate(fixtures.pdf_bytes(201)).status_code, 400)
        self.assertIsNotNone(self.metadata(doc))

    def test_private_document_rejects_web(self):
        response = self.client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'},
                                    data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'test.pdf')})
        self.assertEqual(response.status_code,201,response.get_json())
        doc = response.get_json()['document_id']
        for method,path in [('get',''),('post','/renew'),('delete',''),('get','/tile'),('post','/layout-analysis'),('post','/scene'),('get','/scene/' + 'a'*64 + '/bundle')]:
            self.assertEqual(getattr(self.client,method)(fixtures.BASE+'/'+doc+path,headers={'Authorization':'Bearer web'}).status_code,404)
        self.assertEqual(self.client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer web'}).status_code, 403)
        self.assertEqual(self.client.get(fixtures.BASE+'/'+doc,headers={'Authorization':'Bearer mobile'}).status_code,200)

    def test_failed_replacement_keeps_mobile_attachment_projection(self):
        response = self.client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'test.pdf')})
        self.assertEqual(response.status_code, 201)
        document = response.get_json()['document_id']
        for payload in ({}, {'file': (io.BytesIO(b'bad'), 'bad.pdf')}, {'file': (io.BytesIO(b'bad'), 'bad.txt')}):
            failed = self.client.post(fixtures.BASE, headers={'Authorization':'Bearer web'}, data=payload)
            self.assertIn(failed.status_code, (400, 415))
            self.assertEqual(self.client.get(fixtures.BASE + '/' + document, headers={'Authorization':'Bearer mobile'}).status_code, 200)
        self.assertFalse(list((self.ledger.root / 'staging').iterdir()))

    def test_successful_replacement_retires_old_document(self):
        self.ledger.policy.owner_max_documents = 1
        response = self.client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'test.pdf')})
        document = response.get_json()['document_id']
        newer = self.client.post(fixtures.BASE, headers={'Authorization':'Bearer web'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'next.pdf')})
        self.assertEqual(newer.status_code, 201, newer.get_json())
        self.assertEqual(self.client.get(fixtures.BASE + '/' + document, headers={'Authorization':'Bearer mobile'}).status_code, 404)
        self.assertEqual(self.client.get(fixtures.BASE + '/' + newer.get_json()['document_id'], headers={'Authorization':'Bearer web'}).status_code, 200)

    def test_capacity_failure_preserves_old_document(self):
        response = self.client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'test.pdf')})
        document = response.get_json()['document_id']
        self.ledger.policy.owner_source_bytes = 20 * 1024 * 1024
        failed = self.client.post(fixtures.BASE, headers={'Authorization':'Bearer web'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'next.pdf')})
        self.assertEqual(failed.status_code, 429)
        self.assertEqual(failed.get_json()['error_code'], 'projection_owner_capacity')
        self.assertEqual(self.client.get(fixtures.BASE + '/' + document, headers={'Authorization':'Bearer mobile'}).status_code, 200)

    def test_plt_replaces_pdf_after_real_parse(self):
        self.ledger.policy.derived_max_bytes = 64 * 1024 * 1024
        self.ledger.policy.owner_derived_bytes = 128 * 1024 * 1024
        response = self.client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'test.pdf')})
        document = response.get_json()['document_id']
        newer = self.client.post(fixtures.BASE, headers={'Authorization':'Bearer web'}, data={'file': (io.BytesIO(b'IN;PU0,0;PD4000,4000;'), 'next.plt')})
        self.assertEqual(newer.status_code, 201, newer.get_json())
        self.assertEqual(newer.get_json()['source_format'], 'plt')
        self.assertEqual(self.client.get(fixtures.BASE + '/' + document, headers={'Authorization':'Bearer mobile'}).status_code, 404)

    def test_failed_replacement_keeps_queued_analysis_until_success(self):
        from app.projection_layout import projection_queue
        response = self.client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'}, data={'file': (io.BytesIO(fixtures.pdf_bytes(2)), 'test.pdf')})
        document = response.get_json()['document_id']
        environment, redis = fixtures.ProjectionTests.queue_environment(self)
        with environment:
            queued = self.client.post(fixtures.BASE + '/' + document + '/layout-analysis', headers={'Authorization':'Bearer mobile'})
            self.assertEqual(queued.status_code, 202, queued.get_json())
            analysis_id = queued.get_json()['analysis_id']
            queue = projection_queue(redis)
            self.assertIn(analysis_id, queue.job_ids)
            failed = self.client.post(fixtures.BASE, headers={'Authorization':'Bearer web'}, data={'file': (io.BytesIO(b'bad'), 'bad.pdf')})
            self.assertEqual(failed.status_code, 400)
            self.assertIn(analysis_id, queue.job_ids)
            new = self.client.post(fixtures.BASE, headers={'Authorization':'Bearer web'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'new.pdf')})
            self.assertEqual(new.status_code, 201, new.get_json())
            self.assertNotIn(analysis_id, queue.job_ids)

    def test_invalid_web_request_preserves_mobile_inflight_parse(self):
        import threading
        import app.projection_runtime as runtime
        entered = threading.Event()
        proceed = threading.Event()
        result = []
        original = runtime.run
        def blocked(*args, **kwargs):
            entered.set()
            self.assertTrue(proceed.wait(5))
            return original(*args, **kwargs)
        def mobile():
            with self.app.test_client() as client:
                response = client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'valid.pdf')})
                result.append((response.status_code, response.get_json()))
        with patch('app.projection_runtime.run', side_effect=blocked):
            thread = threading.Thread(target=mobile)
            thread.start()
            self.assertTrue(entered.wait(5))
            with self.app.test_client() as client:
                self.assertEqual(client.post(fixtures.BASE, headers={'Authorization':'Bearer web'}, data={}).status_code, 400)
            proceed.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 201, result)

    def test_invalid_or_busy_candidate_does_not_cancel_parsing_source(self):
        import threading
        import app.projection_runtime as runtime
        entered, proceed = threading.Event(), threading.Event()
        original = runtime.run
        result = []
        def blocked(*args, **kwargs):
            if threading.current_thread().name == 'mobile-upload':
                entered.set()
                self.assertTrue(proceed.wait(10))
            return original(*args, **kwargs)
        def mobile():
            with self.app.test_client() as client:
                response = client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'valid.pdf')})
                result.append((response.status_code, response.get_json()))
        self.ledger.policy.global_parsers = 2
        with patch('app.projection_runtime.run', side_effect=blocked):
            thread = threading.Thread(target=mobile, name='mobile-upload')
            thread.start()
            self.assertTrue(entered.wait(5))
            with self.app.test_client() as client:
                headers = {'Authorization':'Bearer web'}
                self.assertEqual(client.post(fixtures.BASE, headers=headers, data={'file': (io.BytesIO(b'bad'), 'wrong.txt')}).status_code, 415)
                self.assertEqual(client.post(fixtures.BASE, headers=headers, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'valid.pdf'), 'source_format': 'plt'}).status_code, 400)
                oversized = client.post(fixtures.BASE, headers=headers, data={'file': (io.BytesIO(b'x' * (21 * 1024 * 1024)), 'large.pdf')})
                self.assertEqual(oversized.status_code, 413)
                oversized.request.environ['wsgi.input'].close()
                self.assertEqual(client.post(fixtures.BASE, headers=headers, data={'file': (io.BytesIO(b'bad'), 'wrong.pdf')}).status_code, 400)
                self.ledger.policy.global_parsers = 1
                self.assertEqual(client.post(fixtures.BASE, headers=headers, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'busy.pdf')}).status_code, 429)
            proceed.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 201, result)

    def test_newer_success_cancels_earlier_parse(self):
        import threading
        import app.projection_runtime as runtime
        entered, proceed = threading.Event(), threading.Event()
        original = runtime.run
        result = []
        def blocked(*args, **kwargs):
            if threading.current_thread().name == 'mobile-upload':
                entered.set()
                self.assertTrue(proceed.wait(10))
            return original(*args, **kwargs)
        def mobile():
            with self.app.test_client() as client:
                response = client.post(ATTACHMENT_DOCUMENTS, headers={'Authorization':'Bearer mobile'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'valid.pdf')})
                result.append((response.status_code, response.get_json()))
        self.ledger.policy.global_parsers = 2
        with patch('app.projection_runtime.run', side_effect=blocked):
            thread = threading.Thread(target=mobile, name='mobile-upload')
            thread.start()
            self.assertTrue(entered.wait(5))
            with self.app.test_client() as client:
                newer = client.post(fixtures.BASE, headers={'Authorization':'Bearer web'}, data={'file': (io.BytesIO(fixtures.pdf_bytes()), 'newer.pdf')})
                self.assertEqual(newer.status_code, 201, newer.get_json())
            proceed.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 409, result)
        self.assertEqual(result[0][1]['error_code'], 'projection_upload_superseded')
