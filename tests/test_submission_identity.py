import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pymupdf

from app import create_app
from app.job_queue import JOB_OUTPUT_VERSIONS, load_job, save_job, submit_job, update_job


class MemoryLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        pass


class SubmissionIdentityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.redis = fakeredis.FakeRedis()
        self.redis.lock = lambda *args, **kwargs: MemoryLock()
        self.enterContext(patch.dict(os.environ, {
            'PLT_TEMP_FOLDER': self.directory.name,
            'PLT_RATE_LIMIT_PER_MINUTE': '100',
            'PLT_UPLOAD_RATE_LIMIT_PER_MINUTE': '100',
            'PLT_UPLOAD_IP_RATE_LIMIT_PER_MINUTE': '100',
            'PLT_USER_MAX_ACTIVE_JOBS': '20',
            'PLT_QUEUE_MAX_PENDING': '30',
        }))
        self.enterContext(patch('app.job_queue.redis_connection', return_value=self.redis))
        self.enterContext(patch('app.routes.redis_connection', return_value=self.redis))

    def submit(self, source=b'IN;PU0,0;PD1016,1016;', filename='source.plt', options=None, request_id='request-1'):
        return submit_job('plt_to_pdf', source, filename,
                          options if options is not None else {'units_per_inch': 1016},
                          'user:42', self.redis, billing_request_id=request_id)

    def test_same_request_replays_but_changed_content_name_or_options_conflicts(self):
        first = self.submit()
        repeated = self.submit()
        self.assertEqual(repeated['job_id'], first['job_id'])
        for changes in [
            {'source': b'IN;PU0,0;PD500,500;'},
            {'filename': 'changed.plt'},
            {'options': {'units_per_inch': 2032}},
        ]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, '请求编号'):
                self.submit(**changes)
        self.assertEqual(self.redis.llen('rq:queue:conversions'), 1)
        self.assertEqual(load_job(first['job_id'], self.redis)['status'], 'billing_pending')

    def test_legacy_record_without_fingerprint_can_be_verified_from_source(self):
        first = self.submit()
        first.pop('fingerprint', None)
        save_job(first, self.redis)
        for key in self.redis.keys('plt-converter:fingerprint:*'):
            self.redis.delete(key)
        self.assertEqual(self.submit()['job_id'], first['job_id'])
        with self.assertRaisesRegex(ValueError, '请求编号'):
            self.submit(source=b'IN;CI500;')

    def test_completed_request_replay_survives_renderer_version_change(self):
        first = self.submit()
        result = Path(first['input_path']).with_name('result.pdf')
        result.write_bytes(b'%PDF-1.4')
        update_job(first['job_id'], self.redis, status='done', result_path=str(result))
        with patch.dict(JOB_OUTPUT_VERSIONS, {'plt_to_pdf': 'next-version'}):
            self.assertEqual(self.submit()['job_id'], first['job_id'])
            with self.assertRaisesRegex(ValueError, '请求编号'):
                self.submit(options={'units_per_inch': 2032})

    def test_old_plain_pdf_request_replays_without_metadata_mode_key(self):
        for job_type, old_options in (
            ('pdf_to_plt', {'rows': 1, 'columns': 1, 'line_width_mm': 1.0}),
            ('pdf_to_pdf', {'paper_size': 'A4', 'source_page_count': 1}),
        ):
            with self.subTest(job_type=job_type):
                source = b'%PDF old ordinary source'
                request_id = f'legacy-{job_type}'
                old_job = submit_job(
                    job_type, source, 'source.pdf', old_options, 'user:42',
                    self.redis, billing_request_id=request_id,
                )
                current_options = {**old_options, 'metadata_mode': None}
                replay = submit_job(
                    job_type, source, 'source.pdf', current_options, 'user:42',
                    self.redis, billing_request_id=request_id,
                )
                self.assertEqual(replay['job_id'], old_job['job_id'])
                for changed_source, changed_name, changed_options in (
                    (b'%PDF changed', 'source.pdf', current_options),
                    (source, 'renamed.pdf', current_options),
                    (source, 'source.pdf', {**current_options, 'rows': 2}),
                    (source, 'source.pdf', {**old_options, 'metadata_mode': 'current'}),
                    (source, 'source.pdf', {**old_options, 'metadata_mode': 'original'}),
                ):
                    with self.assertRaisesRegex(ValueError, '请求编号'):
                        submit_job(job_type, changed_source, changed_name, changed_options,
                                   'user:42', self.redis, billing_request_id=request_id)
                self.assertEqual(load_job(old_job['job_id'], self.redis)['status'], 'billing_pending')

                # Records without a persisted fingerprint must still verify the source.
                old_job.pop('fingerprint', None)
                save_job(old_job, self.redis)
                for key in self.redis.keys('plt-converter:fingerprint:*'):
                    self.redis.delete(key)
                self.assertEqual(submit_job(
                    job_type, source, 'source.pdf', current_options, 'user:42',
                    self.redis, billing_request_id=request_id,
                )['job_id'], old_job['job_id'])

    def test_legacy_missing_source_uses_index_or_rejects_without_touching_old_job(self):
        first = self.submit()
        first.pop('fingerprint', None)
        save_job(first, self.redis)
        Path(first['input_path']).unlink()
        self.assertEqual(self.submit()['job_id'], first['job_id'])
        for key in self.redis.keys('plt-converter:fingerprint:*'):
            self.redis.delete(key)
        with self.assertRaisesRegex(ValueError, '请求编号'):
            self.submit()
        self.assertEqual(load_job(first['job_id'], self.redis)['status'], 'billing_pending')
        self.assertEqual(self.redis.llen('rq:queue:conversions'), 1)

    def test_failed_request_keeps_existing_retry_behavior(self):
        first = self.submit()
        update_job(first['job_id'], self.redis, status='failed')
        retried = self.submit(source=b'IN;CI500;')
        self.assertNotEqual(first['job_id'], retried['job_id'])
        self.assertEqual(retried['status'], 'billing_pending')

    def test_conflict_routes_preserve_original_job_and_billing(self):
        def pdf_with_line(y):
            document = pymupdf.open()
            page = document.new_page(width=300, height=300)
            page.draw_line((20, y), (280, y))
            source = document.tobytes()
            document.close()
            return source

        client = create_app().test_client()
        for index, (route, filename) in enumerate([
            ('/api/v1/plt/jobs', 'source.plt'),
            ('/api/v1/pdf/jobs', 'source.pdf'),
            ('/api/v1/pdf/repage/jobs', 'source.pdf'),
        ]):
            request_id = f'route-request-{index}'
            with self.subTest(route=route), \
                    patch('app.routes.authorize_job', return_value={
                        'user_id': 42, 'request_id': request_id, 'access_method': 'free',
                    }), patch('app.routes.commit_conversion') as commit, \
                    patch('app.routes.release_conversion') as release, \
                    patch('app.routes.pdf_page_count', return_value=1):
                original = b'original' if route.startswith('/api/v1/plt/') else pdf_with_line(40)
                changed = b'changed' if route.startswith('/api/v1/plt/') else pdf_with_line(60)
                first = client.post(route, data={'file': (io.BytesIO(original), filename), 'paper_size': 'A4'})
                self.assertEqual(first.status_code, 200, first.get_json())
                job_id = first.get_json()['job_id']
                commit.reset_mock()
                conflict = client.post(route, data={'file': (io.BytesIO(changed), filename), 'paper_size': 'A4'})
                self.assertEqual(conflict.status_code, 409, conflict.get_json())
                self.assertEqual(load_job(job_id, self.redis)['status'], 'queued')
                self.assertEqual(self.redis.get(f'plt-converter:billing-request:user:42:{request_id}').decode(), job_id)
                commit.assert_not_called()
                release.assert_not_called()

    def test_cancellation_respects_route_type_and_ownership(self):
        client = create_app().test_client()
        routes = {
            'plt_to_pdf': '/api/v1/plt/jobs/',
            'pdf_to_plt': '/api/v1/pdf/jobs/',
            'pdf_to_pdf': '/api/v1/pdf/repage/jobs/',
        }
        with patch('app.routes.authenticated_user_key', return_value='user:42'), \
                patch('app.routes.release_conversion', return_value=True) as release:
            for index, (actual_type, matching_route) in enumerate(routes.items()):
                record = submit_job(actual_type, b'source', f'source-{index}.pdf', {},
                                    'user:42', self.redis, billing_request_id=f'cancel-{index}',
                                    billing_access_method='free')
                for expected_type, wrong_route in routes.items():
                    if expected_type == actual_type:
                        continue
                    with self.subTest(actual_type=actual_type, route=wrong_route):
                        denied = client.delete(wrong_route + record['job_id'])
                        self.assertEqual(denied.status_code, 404)
                        self.assertEqual(load_job(record['job_id'], self.redis)['status'], 'billing_pending')
                        release.assert_not_called()
                allowed = client.delete(matching_route + record['job_id'])
                self.assertEqual(allowed.status_code, 200)
                self.assertEqual(load_job(record['job_id'], self.redis)['status'], 'cancelled')
                release.assert_called_once()
                release.reset_mock()
            other = submit_job('plt_to_pdf', b'other', 'other.plt', {}, 'user:43', self.redis)
            self.assertEqual(client.delete(routes['plt_to_pdf'] + other['job_id']).status_code, 403)
            self.assertEqual(load_job(other['job_id'], self.redis)['status'], 'queued')
            self.assertEqual(client.delete(routes['plt_to_pdf'] + 'missing').status_code, 404)


if __name__ == '__main__':
    unittest.main()
