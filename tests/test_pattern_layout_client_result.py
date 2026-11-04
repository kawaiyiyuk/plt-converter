import copy
import gzip
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from app.pattern_layout_app import ClientResultTooLarge, create_app, decompress_client_result
from app.services.pattern_layout_api import ENGINE_VERSION
from app.services.pattern_layout_fingerprint import recognition_code_version
from app.services.pattern_layout_variants import recognize_with_variants
from app.services.pattern_layout_client_result import ClientResultError, verify_client_result

SOURCE=b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
RUNTIME={'pyodide':'314.0.7','python':'3.14.0','numpy':'2.3.0','shapely':'2.1.2','geos':'3.12.1'}


def candidate_payload(candidate):
    payload={'id':candidate['id'],'status':candidate['status'],'outer':candidate['outer'],
             'fragments':[[fragment['path_id'],fragment['segment_index'],fragment['t0'],fragment['t1']] for fragment in candidate['fragments']]}
    if 'issues' in candidate:payload['issues']=[{'code':issue['code']} for issue in candidate['issues']]
    for key in ('parent_candidate_id','variant_label_cn','loop_id','source_closed_path_ids','area_mm2','excluded_rectangle','content_complete_for_region'):
        if key in candidate:payload[key]=candidate[key]
    return payload


def payload_from(result,name='source.plt'):
    return {'format':'pattern-layout-client-result','format_version':1,
            'source':{'sha256':result['source']['sha256'],'name':name,'path_count':len(result['paths']),
                      'text_count':sum(1 for shape in result['non_path_shapes'] if shape['type']=='text'),
                      'warnings':list(result['source']['warnings'])},
            'engine':{'engine_version':result['source']['engine_version'],'code_version':recognition_code_version(),
                      'runtime_version':hashlib.sha256(b'browser-runtime').hexdigest(),'runtime':dict(RUNTIME)},
            'pieces':[candidate_payload(candidate) for candidate in result['pieces']],
            'auxiliary':[candidate_payload(candidate) for candidate in result['auxiliary']],
            'unassigned':{'count':result['unassigned']['count'],'length_mm':result['unassigned']['length_mm']}}


class ClientResultVerificationTests(unittest.TestCase):
    def setUp(self):
        self.result=recognize_with_variants(SOURCE,'fixture.plt')
        self.payload=payload_from(self.result,'fixture.plt')

    def assert_error(self,payload,code):
        with self.assertRaises(ClientResultError) as raised:verify_client_result(SOURCE,payload)
        self.assertEqual(raised.exception.code,code)

    def test_server_and_browser_result_round_trip_and_resources_match(self):
        verified=verify_client_result(SOURCE,self.payload)
        self.assertEqual({c['id'] for c in verified['pieces']+verified['auxiliary']},{c['id'] for c in self.result['pieces']+self.result['auxiliary']})
        server_by_id={c['id']:c for c in self.result['pieces']+self.result['auxiliary']}
        for candidate in verified['pieces']+verified['auxiliary']:
            server=server_by_id[candidate['id']]
            self.assertEqual([(f['path_id'],f['segment_index'],f['t0'],f['t1'],f['a'],f['b'],f['style']) for f in candidate['fragments']],
                             [(f['path_id'],f['segment_index'],f['t0'],f['t1'],f['a'],f['b'],f['style']) for f in server['fragments']])
        self.assertEqual([c['text_ids'] for c in verified['pieces']],[c['text_ids'] for c in self.result['pieces']])
        self.assertEqual((verified['source']['width_mm'],verified['source']['height_mm'],verified['source']['warnings']),
                         (self.result['source']['width_mm'],self.result['source']['height_mm'],self.result['source']['warnings']))
        self.assertEqual(verified['source']['recognition_origin'],'browser')
        self.assertTrue(all(c['semantic_piece_verified'] is False for c in verified['pieces']+verified['auxiliary']))
        self.assertEqual(verified['source']['server_verification'],'structure_only')
        self.assertEqual(verified['source']['runtime_version'],self.payload['engine']['runtime_version'])
        from app.services.pattern_layout_resources import candidate_metadata, write_resources
        with tempfile.TemporaryDirectory() as left,tempfile.TemporaryDirectory() as right:
            write_resources(left,self.result,32*1024*1024);write_resources(right,verified,32*1024*1024)
            server_meta={c['id']:candidate_metadata(c) for c in self.result['pieces']+self.result['auxiliary']}
            client_meta={c['id']:candidate_metadata(c) for c in verified['pieces']+verified['auxiliary']}
            provenance=('semantic_piece_verified',)
            for identifier in server_meta:
                self.assertEqual({k:v for k,v in server_meta[identifier].items() if k not in provenance},
                                 {k:v for k,v in client_meta[identifier].items() if k not in provenance})

    def test_source_sha_mismatch(self):
        payload=copy.deepcopy(self.payload);payload['source']['sha256']='0'*64
        self.assert_error(payload,'source_sha_mismatch')

    def test_engine_code_version_mismatch(self):
        payload=copy.deepcopy(self.payload);payload['engine']['code_version']='0'*64
        self.assert_error(payload,'engine_code_mismatch')

    def test_engine_engine_version_mismatch(self):
        payload=copy.deepcopy(self.payload);payload['engine']['engine_version']=ENGINE_VERSION+'-other'
        self.assert_error(payload,'engine_code_mismatch')

    def test_parser_warning_mismatch(self):
        payload=copy.deepcopy(self.payload);payload['source']['warnings']=payload['source']['warnings']+['不同警告']
        self.assert_error(payload,'parser_mismatch')

    def test_parser_path_count_mismatch(self):
        payload=copy.deepcopy(self.payload);payload['source']['path_count']+=1
        self.assert_error(payload,'parser_mismatch')

    def test_unknown_fragment_path(self):
        payload=copy.deepcopy(self.payload);payload['pieces'][0]['fragments'][0][0]='shape-absent'
        self.assert_error(payload,'invalid_source_fragment')

    def test_fragment_segment_index_out_of_range(self):
        payload=copy.deepcopy(self.payload);payload['pieces'][0]['fragments'][0][1]=999
        self.assert_error(payload,'invalid_source_fragment')

    def test_fragment_zero_length_interval(self):
        payload=copy.deepcopy(self.payload);payload['pieces'][0]['fragments'][0][2]=.5;payload['pieces'][0]['fragments'][0][3]=.5
        self.assert_error(payload,'invalid_source_fragment')

    def test_outer_out_of_bounds(self):
        payload=copy.deepcopy(self.payload);payload['pieces'][0]['outer'][0]=[self.result['source']['width_mm']+2,0]
        self.assert_error(payload,'outer_out_of_bounds')

    def test_duplicate_candidate_id(self):
        payload=copy.deepcopy(self.payload);payload['auxiliary'].append(copy.deepcopy(payload['pieces'][0]))
        self.assert_error(payload,'client_result_invalid')

    def test_invalid_candidate_id(self):
        payload=copy.deepcopy(self.payload);payload['pieces'][0]['id']='bad id!'
        self.assert_error(payload,'client_result_invalid')

    def test_not_finite_number(self):
        payload=copy.deepcopy(self.payload);payload['pieces'][0]['outer'][0]=[float('nan'),0]
        self.assert_error(payload,'client_result_invalid')

    def test_candidate_count_limit(self):
        payload=copy.deepcopy(self.payload)
        payload['pieces']=[{'id':f'C{i}','status':'s','fragments':[],'outer':[]} for i in range(5001)]
        self.assert_error(payload,'client_result_invalid')

    def test_fragment_count_limit(self):
        payload=copy.deepcopy(self.payload)
        payload['pieces'][0]['fragments']=[['shape-1',0,0.,1.]]*500001
        self.assert_error(payload,'client_result_invalid')

    def test_outer_point_count_limit(self):
        payload=copy.deepcopy(self.payload)
        payload['pieces'][0]['outer']=[[0.,0.]]*50001
        self.assert_error(payload,'client_result_invalid')

    def test_recognition_code_version_ignores_runtime_environment(self):
        from app.services import pattern_layout_fingerprint as fingerprint
        before_code=fingerprint.recognition_code_version();before_runtime=fingerprint.recognition_runtime_version()
        with patch.object(fingerprint,'environment',return_value={'different':'environment'}):
            self.assertEqual(fingerprint.recognition_code_version(),before_code)
            self.assertNotEqual(fingerprint.recognition_runtime_version(),before_runtime)


class ClientResultTokenLimitTests(unittest.TestCase):
    def test_decompress_rejects_too_many_json_tokens_before_byte_limit(self):
        from app import pattern_layout_app
        compressed=gzip.compress(b'['*10)
        with patch.object(pattern_layout_app,'MAX_CLIENT_RESULT_JSON_TOKENS',5):
            with self.assertRaises(ClientResultTooLarge):decompress_client_result(compressed)

    def test_decompress_allows_exact_json_token_limit(self):
        from app import pattern_layout_app
        compressed=gzip.compress(b'['*5)
        with patch.object(pattern_layout_app,'MAX_CLIENT_RESULT_JSON_TOKENS',5):
            self.assertEqual(decompress_client_result(compressed),b'['*5)


class ClientResultHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.policy={'PATTERN_LAYOUT_ROOT':self.temp.name,'PATTERN_LAYOUT_DISK_BUDGET_BYTES':str(256*1024*1024),
                     'PATTERN_LAYOUT_MAX_RESULT_BYTES':str(32*1024*1024),'PATTERN_LAYOUT_JOB_RETENTION_SECONDS':'60',
                     'PATTERN_LAYOUT_MAX_REQUEST_BYTES':str(20*1024*1024),'PATTERN_LAYOUT_TASK_WALL_SECONDS':'30','PATTERN_LAYOUT_TASK_MEMORY_BYTES':str(2*1024*1024*1024)}

    def headers(self):return {'Authorization':'Bearer local-test-only','X-Pattern-Layout-Owner':'owner-1'}

    def submit(self,client,payload,key=None):
        headers=self.headers()
        if key is not None:headers={**headers,'X-Idempotency-Key':key}
        return client.post('/internal/pattern-layout/client-results',headers=headers,
                           data={'file':(io.BytesIO(SOURCE),'fixture.plt'),
                                 'result':(io.BytesIO(gzip.compress(json.dumps(payload,ensure_ascii=False).encode())),'result.json.gz')})

    def test_http_client_result_runs_and_serves_archive_with_raw_client_audit(self):
        result=recognize_with_variants(SOURCE,'fixture.plt');payload=payload_from(result,'fixture.plt')
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client()
            try:
                capabilities=client.get('/internal/pattern-layout/capabilities',headers=self.headers())
                self.assertEqual(capabilities.json['client_result_format_version'],1)
                self.assertEqual(capabilities.json['recognition_code_version'],recognition_code_version())
                self.assertEqual(capabilities.json['engine_version'],ENGINE_VERSION)
                response=self.submit(client,payload)
                self.assertEqual(response.status_code,202)
                identifier=response.json['id'];queue=app.extensions['pattern_layout_queue'];queue.futures[identifier].result(timeout=30)
                status=client.get('/internal/pattern-layout/jobs/'+identifier,headers=self.headers())
                self.assertEqual(status.json['status'],'done')
                candidate_id=status.json['result']['pieces'][0]['id']
                resource=client.get('/internal/pattern-layout/jobs/'+identifier+'/candidates/'+candidate_id,headers=self.headers())
                self.assertEqual(resource.status_code,200)
                archive=client.get('/internal/pattern-layout/jobs/'+identifier+'/archive',headers=self.headers())
                self.assertEqual(archive.status_code,200)
                import zipfile
                with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
                    audit=json.loads(gzip.decompress(bundle.read('resources/audit.json.gz')))
                    self.assertEqual(audit,payload)
                    self.assertIn('index.json',bundle.namelist())
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_http_oversized_compressed_client_result_is_413(self):
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client()
            try:
                response=client.post('/internal/pattern-layout/client-results',headers=self.headers(),
                                     data={'file':(io.BytesIO(SOURCE),'fixture.plt'),
                                           'result':(io.BytesIO(b'x'*(8*1024*1024+1)),'result.json.gz')})
                self.assertEqual(response.status_code,413)
                self.assertEqual(response.json['error_code'],'client_result_too_large')
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_http_too_many_json_tokens_is_413_before_json_loads(self):
        from app import pattern_layout_app
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client()
            try:
                with patch.object(pattern_layout_app,'MAX_CLIENT_RESULT_JSON_TOKENS',3),patch.object(pattern_layout_app,'json') as app_json:
                    response=client.post('/internal/pattern-layout/client-results',headers=self.headers(),
                                         data={'file':(io.BytesIO(SOURCE),'fixture.plt'),
                                               'result':(io.BytesIO(gzip.compress(b'['*10)),'result.json.gz')})
                    self.assertEqual(response.status_code,413)
                    self.assertEqual(response.json['error_code'],'client_result_too_large')
                    app_json.loads.assert_not_called()
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)

    def test_http_different_payload_is_not_reused(self):
        result=recognize_with_variants(SOURCE,'fixture.plt');first=payload_from(result,'fixture.plt')
        second=copy.deepcopy(first);second['source']['name']='another-name.plt'
        with patch.dict(os.environ,{**self.policy,'PATTERN_LAYOUT_SERVICE_KEY':'local-test-only'}):
            app=create_app();client=app.test_client();queue=app.extensions['pattern_layout_queue']
            try:
                one=self.submit(client,first);two=self.submit(client,second)
                self.assertEqual((one.status_code,two.status_code),(202,202))
                self.assertNotEqual(one.json['id'],two.json['id'])
                for response in (one,two):queue.futures[response.json['id']].result(timeout=30)
            finally:app.extensions['pattern_layout_executor'].shutdown(wait=True,cancel_futures=True)


if __name__=='__main__':unittest.main()
