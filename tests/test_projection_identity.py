"""Projection-only identity failures, no backend/Redis/parser/network operations."""
import io
from http.client import HTTPException, IncompleteRead, BadStatusLine, RemoteDisconnected
import json
import os
from pathlib import Path
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.projection_app import create_projection_app
from app.projection_layout import worker_main
from test_projection import synthetic_policy

BASE = '/api/v1/pdf/projection/documents'

class ProjectionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict(os.environ, {'WX_BACKEND_URL': 'https://identity.invalid'}, clear=True)
        patcher.start(); self.addCleanup(patcher.stop)
        self.app = create_projection_app(synthetic_policy(self.temp.name))
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.store = self.app.extensions['projection_store']
        lease = self.store.reserve('user:1', 'upload')
        folder = self.store.lease_path(lease); folder.mkdir()
        source = folder / 'source.pdf'; source.write_bytes(b'%PDF-test-identity-only')
        self.document = self.store.complete_upload(lease, 'user:1', source,
            {'owner': 'user:1', 'filename':'fixture.pdf', 'expires_at':time.time()+3600,
             'pages':[{'width_mm':100,'height_mm':100}], 'page_count':1})
        self.store.release(lease)
        self.endpoints = [('POST',BASE), ('GET',f'{BASE}/{self.document}'),
            ('GET',f'{BASE}/{self.document}/tile'), ('POST',f'{BASE}/{self.document}/renew'),
            ('DELETE',f'{BASE}/{self.document}'), ('GET',f'{BASE}/{self.document}/layout-analysis'),
            ('POST',f'{BASE}/{self.document}/layout-analysis'), ('POST',f'{BASE}/{self.document}/layout')]

    def response(self, body, status=200):
        response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        response.status = status
        return response

    def snapshot(self):
        return {str(path.relative_to(self.store.root)):path.read_bytes()
                for path in self.store.root.rglob('*') if path.is_file()}

    def test_identity_backend_failures_are_retryable_and_preserve_every_document(self):
        failures = [HTTPError('https://identity.invalid', code, 'failure', {}, io.BytesIO(b'{"message":"upstream"}'))
                    for code in (404,410,429,502)]
        failures += [URLError('offline'), TimeoutError(), self.response(b'not-json'),
                     self.response(b'\xff'), self.response([]), self.response({'data':[]}),
                     self.response({'data':{}}), self.response({'data':{'user_id':[1]}}),
                     self.response({'data':{'user_id':{'id':1}}}), self.response({'data':{'user_id':True}})]
        for failure in failures:
            if isinstance(failure, HTTPError):
                self.addCleanup(failure.close)
            for method,path in self.endpoints:
                with self.subTest(failure=type(failure).__name__, method=method, path=path):
                    before = self.snapshot()
                    # Fresh streams for a separate upstream response on every request.
                    if isinstance(failure, io.BytesIO):
                        effect = None; result = self.response(failure.getvalue())
                    else:
                        effect = failure; result = None
                    with patch('app.billing.urlopen',side_effect=effect,return_value=result):
                        response = self.client.open(path, method=method, headers={'Authorization':'Bearer private-fixture'})
                    self.assertEqual(response.status_code,503)
                    self.assertEqual(response.json['error_code'],'projection_identity_unavailable')
                    self.assertEqual(response.headers['Retry-After'],'1')
                    self.assertNotIn('private-fixture',response.get_data(as_text=True))
                    self.assertEqual(self.snapshot(),before)

    def test_interrupted_http_response_read_is_retryable_on_all_endpoints(self):
        for error in (IncompleteRead(b'{"data":',10),BadStatusLine('broken protocol'),RemoteDisconnected()):
            class BrokenResponse(io.BytesIO):
                status = 200
                def read(self):
                    raise error
            for method,path in self.endpoints:
                with self.subTest(error=type(error).__name__,method=method,path=path):
                    before = self.snapshot()
                    with patch('app.billing.urlopen',side_effect=lambda *a,**kw:BrokenResponse()):
                        response = self.client.open(path,method=method,headers={'Authorization':'Bearer read-private'})
                    self.assertEqual(response.status_code,503)
                    self.assertEqual(response.json['error_code'],'projection_identity_unavailable')
                    self.assertEqual(response.headers['Retry-After'],'1')
                    self.assertNotIn('read-private',response.get_data(as_text=True))
                    self.assertEqual(self.snapshot(),before)

    def test_two_http_read_failures_release_both_identity_slots(self):
        from app import projection
        from app.projection_store import ProjectionRejected
        entered = threading.Event(); release = threading.Event(); count_lock = threading.Lock(); count = [0]
        class BrokenResponse(io.BytesIO):
            status = 200
            def read(self):
                with count_lock:
                    count[0] += 1
                    if count[0] == 2: entered.set()
                self_test.assertTrue(release.wait(3))
                raise IncompleteRead(b'{"data":',10)
        self_test = self
        with patch('app.billing.urlopen',side_effect=lambda *a,**kw:BrokenResponse()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(projection.identify_user,'Bearer interrupted-'+str(index)) for index in (1,2)]
                try:
                    self.assertTrue(entered.wait(3))
                    with self.assertRaises(ProjectionRejected) as busy:
                        projection.identify_user('Bearer third-private')
                    self.assertEqual(busy.exception.code,'projection_identity_busy')
                finally:
                    release.set()
                for future in pending:
                    with self.assertRaises(ProjectionRejected) as unavailable:
                        future.result()
                    self.assertEqual(unavailable.exception.code,'projection_identity_unavailable')
        self.assertEqual(projection._identity_flights,{})
        with patch('app.billing.urlopen',side_effect=lambda *a,**kw:self.response({'data':{'user_id':1}})) as recovered:
            self.assertEqual(projection.identify_user('Bearer interrupted-1'),'user:1')
            self.assertEqual(projection.identify_user('Bearer interrupted-2'),'user:1')
            self.assertEqual(recovered.call_count,2)
        self.assertEqual(projection._identity_flights,{})

    def test_only_authentication_rejections_keep_401_403(self):
        for status in (401,403):
            for method,path in self.endpoints:
                error = HTTPError('https://identity.invalid',status,'failure',{},io.BytesIO(b'{"message":"denied"}'))
                self.addCleanup(error.close)
                with self.subTest(status=status, method=method,path=path), patch('app.billing.urlopen',side_effect=error):
                    response = self.client.open(path,method=method,headers={'Authorization':'Bearer fixture'})
                    self.assertEqual(response.status_code,status)
                    self.assertNotIn('Retry-After',response.headers)
        with patch('app.billing.urlopen',side_effect=AssertionError('missing auth must not call backend')):
            self.assertEqual(self.client.get(BASE+'/'+self.document).status_code,401)

    def test_valid_identity_calls_only_identity_and_keeps_owned_not_found(self):
        with patch('app.billing.urlopen',side_effect=lambda *a,**kw:self.response({'data':{'user_id':1}})) as backend:
            response = self.client.get(f'{BASE}/{self.document}',headers={'Authorization':'Bearer fixture'})
            self.assertEqual(response.status_code,200)
            response = self.client.get(BASE+'/'+'a'*64,headers={'Authorization':'Bearer fixture'})
            self.assertEqual(response.status_code,404)
            self.assertEqual(response.json['error_code'],'projection_not_found')
            response = self.client.post(f'{BASE}/{self.document}/renew',headers={'Authorization':'Bearer fixture'})
            self.assertEqual(response.status_code,200)
            for call in backend.call_args_list:
                self.assertEqual(call.args[0].full_url,'https://identity.invalid/api/v1/points/conversion/identity')
                self.assertEqual(call.args[0].get_header('Authorization'),'Bearer fixture')
                self.assertFalse(any('service-token' in key.lower() for key in call.args[0].headers))
        with patch('app.billing.urlopen',side_effect=lambda *a,**kw:self.response({'data':{'user_id':2}})):
            response = self.client.delete(f'{BASE}/{self.document}',headers={'Authorization':'Bearer other'})
            self.assertEqual(response.status_code,404)
            self.assertIsNotNone(self.store.load(self.document,'user:1'))
        with patch('app.billing.urlopen',side_effect=lambda *a,**kw:self.response({'data':{'user_id':1}})):
            response = self.client.delete(f'{BASE}/{self.document}',headers={'Authorization':'Bearer fixture'})
            self.assertEqual(response.status_code,204)

    def test_only_inflight_same_token_identity_is_shared_without_result_cache(self):
        from app import projection
        entered = threading.Event(); release = threading.Event(); followers = threading.Event()
        count_lock = threading.Lock(); count = [0]
        class ObservedFuture(Future):
            def result(self, *args, **kwargs):
                with count_lock:
                    count[0] += 1
                    if count[0] == 5: followers.set()
                return super().result(*args, **kwargs)
        def backend(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.response({'data':{'user_id':1}})
        with patch('app.projection.Future', ObservedFuture, create=True), patch('app.billing.urlopen',side_effect=backend) as upstream:
            with ThreadPoolExecutor(max_workers=6) as pool:
                first = pool.submit(projection.identify_user,'Bearer private-fixture')
                self.assertTrue(entered.wait(3))
                rest = [pool.submit(projection.identify_user,'Bearer private-fixture') for _ in range(5)]
                try:
                    self.assertTrue(followers.wait(3))
                    self.assertEqual(upstream.call_count,1)
                    self.assertNotIn('private-fixture',repr(getattr(projection,'_identity_flights',{})))
                finally:
                    release.set()
                self.assertEqual([future.result() for future in [first,*rest]],['user:1']*6)
            self.assertEqual(projection.identify_user('Bearer private-fixture'),'user:1')
            self.assertEqual(upstream.call_count,2)
            self.assertEqual(getattr(projection,'_identity_flights',{}),{})

    def test_two_distinct_identity_slots_reject_third_immediately_on_every_endpoint(self):
        from app import projection
        entered = threading.Event(); release = threading.Event(); shared_waiting = threading.Event()
        class ObservedFuture(Future):
            def result(self,*args,**kwargs):
                shared_waiting.set()
                return super().result(*args,**kwargs)
        count_lock = threading.Lock(); count = [0]
        def backend(*args, **kwargs):
            with count_lock:
                count[0] += 1
                if count[0] == 2: entered.set()
                if count[0] > 2 and not release.is_set():
                    raise AssertionError('third identity must be rejected before calling upstream')
            self.assertTrue(release.wait(3))
            return self.response({'data':{'user_id':1}})
        with patch('app.projection.Future',ObservedFuture), patch('app.billing.urlopen',side_effect=backend) as upstream:
            with ThreadPoolExecutor(max_workers=3) as pool:
                pending = [pool.submit(projection.identify_user,'Bearer slot-'+str(index)) for index in (1,2)]
                self.assertTrue(entered.wait(3))
                shared = pool.submit(projection.identify_user,'Bearer slot-1')
                before = self.snapshot()
                try:
                    self.assertTrue(shared_waiting.wait(3))
                    for method,path in self.endpoints:
                        with self.subTest(method=method,path=path):
                            response = self.client.open(path,method=method,headers={'Authorization':'Bearer over-cap-private'})
                            self.assertEqual(response.status_code,503)
                            self.assertEqual(response.json['error_code'],'projection_identity_busy')
                            self.assertEqual(response.headers['Retry-After'],'1')
                            self.assertEqual(response.json['retry_after'],1)
                            self.assertNotIn('over-cap-private',response.get_data(as_text=True))
                            self.assertEqual(self.snapshot(),before)
                    # A second factory in this process must not reset global in-flight capacity.
                    another = create_projection_app(synthetic_policy(self.temp.name)).test_client()
                    response = another.get(BASE+'/'+self.document,headers={'Authorization':'Bearer new-app'})
                    self.assertEqual(response.json['error_code'],'projection_identity_busy')
                    self.assertEqual(upstream.call_count,2)
                    self.assertEqual(len(projection._identity_flights),2)
                finally:
                    release.set()
                self.assertEqual([future.result() for future in [*pending,shared]],['user:1']*3)
                self.assertEqual(upstream.call_count,2)
            self.assertEqual(projection._identity_flights,{})
            response = self.client.get(BASE+'/'+self.document,headers={'Authorization':'Bearer new-slot'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(upstream.call_count,3)

    def test_identity_failure_wakes_followers_frees_capacity_and_is_never_cached(self):
        from app import projection
        from app.billing import BillingRejected
        from app.projection_store import ProjectionRejected
        class AbortIdentity(BaseException):
            pass
        for failure, expected_type, expected_status in (
                (401,BillingRejected,401), (403,BillingRejected,403),
                (502,ProjectionRejected,503), (TimeoutError(),ProjectionRejected,503),
                (IncompleteRead(b'{"data":',10),ProjectionRejected,503),
                (BadStatusLine('broken protocol'),ProjectionRejected,503),
                (RemoteDisconnected(),ProjectionRejected,503),
                (AbortIdentity('aborted'),AbortIdentity,None)):
            with self.subTest(failure=str(failure)):
                entered = threading.Event(); release = threading.Event(); following = threading.Event()
                class ObservedFuture(Future):
                    def result(self,*args,**kwargs):
                        following.set()
                        return super().result(*args,**kwargs)
                def backend(*args, **kwargs):
                    entered.set()
                    self.assertTrue(release.wait(3))
                    if isinstance(failure,int):
                        error = HTTPError('https://identity.invalid',failure,'failure',{},io.BytesIO(b'{}'))
                        self.addCleanup(error.close)
                        raise error
                    if isinstance(failure,HTTPException):
                        class BrokenResponse(io.BytesIO):
                            status = 200
                            def read(self):
                                raise failure
                        return BrokenResponse()
                    raise failure
                with patch('app.projection.Future',ObservedFuture), patch('app.billing.urlopen',side_effect=backend) as upstream:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first = pool.submit(projection.identify_user,'Bearer private-failure')
                        self.assertTrue(entered.wait(3))
                        second = pool.submit(projection.identify_user,'Bearer private-failure')
                        try:
                            self.assertTrue(following.wait(3))
                            self.assertEqual(upstream.call_count,1)
                        finally:
                            release.set()
                        for pending in (first,second):
                            with self.assertRaises(expected_type) as caught:
                                pending.result()
                            error = caught.exception
                            status = getattr(error,'status_code',getattr(error,'status',None))
                            self.assertEqual(status,expected_status)
                            self.assertNotIn('private-failure',str(error))
                    self.assertEqual(projection._identity_flights,{})
                with patch('app.billing.urlopen',side_effect=lambda *a,**kw:self.response({'data':{'user_id':1}})) as recovered:
                    self.assertEqual(projection.identify_user('Bearer private-failure'),'user:1')
                    self.assertEqual(recovered.call_count,1)
                    self.assertEqual(projection._identity_flights,{})

    def test_service_key_presence_blocks_http_and_worker_even_when_empty(self):
        for value in ('fixture-never-print',''):
            with self.subTest(value_present=bool(value)):
                with self.assertRaisesRegex(ValueError,'CONVERSION_SERVICE_TOKEN') as raised:
                    create_projection_app({'CONVERSION_SERVICE_TOKEN':value,'PROJECTOR_ENABLED':'0'})
                self.assertNotIn('fixture-never-print',str(raised.exception))
                with patch.dict(os.environ,{'CONVERSION_SERVICE_TOKEN':value}), \
                     patch('app.projection_layout.Worker',side_effect=AssertionError('worker must not start')):
                    with self.assertRaisesRegex(ValueError,'CONVERSION_SERVICE_TOKEN'):
                        worker_main()

if __name__ == '__main__':
    unittest.main()
