import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fakeredis

from app import create_app
from app.billing import BillingRejected
from app.job_queue import (
    JOB_OUTPUT_VERSIONS,
    QueueRejected,
    cleanup_expired_job_files,
    confirm_job_billing,
    enforce_rate_limit,
    load_job,
    queue_position,
    submit_job,
)
from app.tasks import execute_job, mark_job_failed, mark_job_stopped


class MemoryLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        return None


class JobQueueTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.redis = fakeredis.FakeRedis()
        self.redis.lock = lambda *args, **kwargs: MemoryLock()
        self.environment = patch.dict(os.environ, {
            'PLT_TEMP_FOLDER': self.temp_dir.name,
            'PLT_JOB_RETENTION_SECONDS': '300',
            'PLT_RATE_LIMIT_PER_MINUTE': '10',
            'PLT_USER_MAX_ACTIVE_JOBS': '2',
            'PLT_QUEUE_MAX_PENDING': '5',
        })
        self.environment.start()
        self.redis_patch = patch('app.job_queue.redis_connection', return_value=self.redis)
        self.redis_patch.start()

    def tearDown(self):
        self.redis_patch.stop()
        self.environment.stop()
        self.temp_dir.cleanup()

    def submit(self, user='user-a', source=b'IN;PU0,0;PD1016,1016;'):
        return submit_job('plt_to_pdf', source, 'sample.plt', {
            'units_per_inch': 1016,
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'line_width_mm': 0.265,
            'single_page_output': False,
            'show_page_number': True,
            'enabled_pages': [0],
        }, user, connection=self.redis)

    def test_submit_and_deduplicate(self):
        first = self.submit()
        second = self.submit()

        self.assertEqual(first['job_id'], second['job_id'])
        self.assertTrue(second['deduplicated'])
        rate_keys = self.redis.keys('plt-converter:rate:jobs:user-a:*')
        self.assertEqual(len(rate_keys), 1)
        self.assertEqual(int(self.redis.get(rate_keys[0])), 1)
        self.assertEqual(self.redis.llen('rq:queue:conversions'), 1)
        self.assertEqual(queue_position(first['job_id'], self.redis), 1)

    def test_billed_job_waits_for_confirmation_and_confirmation_is_idempotent(self):
        record = submit_job(
            'plt_to_pdf',
            b'IN;PU0,0;PD1016,1016;',
            'sample.plt',
            {'units_per_inch': 1016},
            'user:42',
            connection=self.redis,
            billing_request_id='billing-request-1',
        )

        self.assertEqual(record['status'], 'billing_pending')
        confirmed = confirm_job_billing(record['job_id'], 'user:42', self.redis)
        from app.job_queue import update_job
        update_job(record['job_id'], self.redis, status='done')
        repeated = confirm_job_billing(record['job_id'], 'user:42', self.redis)

        self.assertEqual(confirmed['status'], 'queued')
        self.assertTrue(confirmed['billing_confirmed'])
        self.assertEqual(repeated['status'], 'done')

    def test_output_version_invalidates_completed_deduplication(self):
        first = self.submit()
        from app.job_queue import update_job
        result_path = Path(first['input_path']).parent / 'sample.pdf'
        result_path.write_bytes(b'%PDF-1.4')
        update_job(first['job_id'], self.redis, status='done', result_path=str(result_path))

        with patch.dict(JOB_OUTPUT_VERSIONS, {'plt_to_pdf': 'next-version'}):
            replacement = self.submit()

        self.assertNotEqual(replacement['job_id'], first['job_id'])
        self.assertEqual(replacement['output_version'], 'next-version')

    def test_transient_failure_retries_once(self):
        record = self.submit()

        class FailedJob:
            args = (record['job_id'],)
            id = record['rq_job_id']

        mark_job_failed(FailedJob(), self.redis, TimeoutError, TimeoutError('temporary'), None)
        retried = load_job(record['job_id'], self.redis)

        self.assertEqual(retried['status'], 'queued')
        self.assertEqual(retried['retry_count'], 1)
        self.assertEqual(retried['rq_job_id'], f"{record['job_id']}:1")
        self.assertTrue(self.redis.sismember('plt-converter:user-jobs:user-a', record['job_id']))

        mark_job_failed(FailedJob(), self.redis, TimeoutError, TimeoutError('temporary'), None)
        failed = load_job(record['job_id'], self.redis)
        self.assertEqual(failed['status'], 'failed')
        self.assertFalse(self.redis.sismember('plt-converter:user-jobs:user-a', record['job_id']))

    def test_execute_transient_failure_requeues_without_releasing_slot(self):
        record = self.submit()
        with patch('app.tasks.redis_connection', return_value=self.redis), \
                patch('app.tasks._execute', side_effect=TimeoutError('temporary')):
            result = execute_job(record['job_id'])

        retried = load_job(record['job_id'], self.redis)
        self.assertIsNone(result)
        self.assertEqual(retried['status'], 'queued')
        self.assertEqual(retried['retry_count'], 1)
        self.assertTrue(self.redis.sismember('plt-converter:user-jobs:user-a', record['job_id']))

    def test_execute_retry_enqueue_failure_marks_failed_and_releases_slot(self):
        record = self.submit()
        queue = type('BrokenQueue', (), {
            'enqueue_call': lambda *args, **kwargs: (_ for _ in ()).throw(OSError('redis write failed'))
        })()
        with patch('app.tasks.redis_connection', return_value=self.redis), \
                patch('app.tasks._execute', side_effect=TimeoutError('temporary')), \
                patch('app.tasks.conversion_queue', return_value=queue):
            result = execute_job(record['job_id'])

        failed = load_job(record['job_id'], self.redis)
        self.assertIsNone(result)
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('任务重试入队失败', failed['error'])
        self.assertFalse(self.redis.sismember('plt-converter:user-jobs:user-a', record['job_id']))

    def test_failure_callback_retry_enqueue_failure_marks_failed(self):
        record = self.submit()

        class FailedJob:
            args = (record['job_id'],)
            id = record['rq_job_id']

        queue = type('BrokenQueue', (), {
            'enqueue_call': lambda *args, **kwargs: (_ for _ in ()).throw(OSError('redis write failed'))
        })()
        with patch('app.tasks.conversion_queue', return_value=queue):
            mark_job_failed(FailedJob(), self.redis, TimeoutError, TimeoutError('temporary'), None)

        failed = load_job(record['job_id'], self.redis)
        self.assertEqual(failed['status'], 'failed')
        self.assertFalse(self.redis.sismember('plt-converter:user-jobs:user-a', record['job_id']))

    def test_stopped_callback_finishes_cancellation_and_releases_slot(self):
        record = self.submit()
        from app.job_queue import update_job
        update_job(record['job_id'], self.redis, status='cancelling', cancel_requested=True)

        class StoppedJob:
            args = (record['job_id'],)
            id = record['rq_job_id']

        mark_job_stopped(StoppedJob(), self.redis)

        cancelled = load_job(record['job_id'], self.redis)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertFalse(self.redis.sismember('plt-converter:user-jobs:user-a', record['job_id']))

    def test_stopped_callback_does_not_overwrite_terminal_status(self):
        record = self.submit()
        from app.job_queue import update_job
        update_job(record['job_id'], self.redis, status='done', progress=100)

        class StoppedJob:
            args = (record['job_id'],)
            id = record['rq_job_id']

        mark_job_stopped(StoppedJob(), self.redis)

        completed = load_job(record['job_id'], self.redis)
        self.assertEqual(completed['status'], 'done')
        self.assertEqual(completed['progress'], 100)

    def test_transient_failure_during_cancellation_does_not_retry(self):
        record = self.submit()
        from app.job_queue import update_job
        update_job(record['job_id'], self.redis, status='cancelling', cancel_requested=True)

        class FailedJob:
            args = (record['job_id'],)
            id = record['rq_job_id']

        mark_job_failed(FailedJob(), self.redis, TimeoutError, TimeoutError('stopped'), None)

        cancelled = load_job(record['job_id'], self.redis)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(cancelled['retry_count'], 0)
        self.assertFalse(self.redis.sismember('plt-converter:user-jobs:user-a', record['job_id']))

    def test_completed_preview_is_deduplicated_only_while_images_exist(self):
        from app.job_queue import save_job
        source = b'%PDF preview source'
        first = submit_job('pdf_preview', source, 'sample.pdf', {}, 'user-a', connection=self.redis)
        preview = Path(first['input_path']).parent / 'previews' / f"{first['job_id']}-0.png"
        preview.parent.mkdir(exist_ok=True)
        preview.write_bytes(b'png')
        first['status'] = 'done'
        first['result'] = {'pages': [{'index': 0}]}
        save_job(first, self.redis)

        deduplicated = submit_job('pdf_preview', source, 'sample.pdf', {}, 'user-a', connection=self.redis)
        self.assertEqual(deduplicated['job_id'], first['job_id'])
        self.assertTrue(deduplicated['deduplicated'])

        preview.unlink()
        replacement = submit_job('pdf_preview', source, 'sample.pdf', {}, 'user-a', connection=self.redis)
        self.assertNotEqual(replacement['job_id'], first['job_id'])

    def test_user_capacity_rejects_distinct_active_jobs(self):
        self.submit(source=b'IN;PU0,0;PD1016,1016;')
        self.submit(source=b'IN;PU0,0;PD2032,2032;')
        with self.assertRaises(QueueRejected):
            self.submit(source=b'IN;PU0,0;PD3048,3048;')

    def test_queue_capacity_counts_active_work(self):
        with patch.dict(os.environ, {'PLT_QUEUE_MAX_PENDING': '1'}):
            self.submit(source=b'IN;PU0,0;PD1016,1016;')
            with self.assertRaises(QueueRejected):
                self.submit(user='user-b', source=b'IN;PU0,0;PD2032,2032;')

    def test_rate_limit_rejects_excess_submissions(self):
        with patch.dict(os.environ, {'PLT_RATE_LIMIT_PER_MINUTE': '2'}):
            enforce_rate_limit('rate-user', self.redis)
            enforce_rate_limit('rate-user', self.redis)
            with self.assertRaises(QueueRejected):
                enforce_rate_limit('rate-user', self.redis)

    def test_cleanup_removes_expired_job_folder(self):
        job_folder = Path(self.temp_dir.name) / 'jobs' / 'expired-job'
        job_folder.mkdir(parents=True)
        old_time = time.time() - 400
        os.utime(job_folder, (old_time, old_time))

        cleanup_expired_job_files()

        self.assertFalse(job_folder.exists())

    def test_cleanup_preserves_expired_active_job_folder(self):
        record = self.submit()
        job_folder = Path(record['input_path']).parent
        old_time = time.time() - 400
        os.utime(job_folder, (old_time, old_time))

        cleanup_expired_job_files(self.redis)

        self.assertTrue(job_folder.exists())

    def test_active_job_ttl_covers_queue_wait_and_retry(self):
        with patch.dict(os.environ, {
            'PLT_JOB_RETENTION_SECONDS': '60',
            'PLT_QUEUE_MAX_PENDING': '5',
            'PLT_JOB_TIMEOUT_SECONDS': '10',
        }):
            record = self.submit()
            ttl = self.redis.ttl(f"plt-converter:job:{record['job_id']}")

        self.assertGreaterEqual(ttl, 399)

    def test_active_job_update_refreshes_user_capacity_ttl(self):
        record = self.submit()
        key = 'plt-converter:user-jobs:user-a'
        self.redis.expire(key, 1)
        from app.job_queue import update_job
        update_job(record['job_id'], self.redis, progress=15)

        self.assertGreater(self.redis.ttl(key), 1)

    def test_cleanup_retains_recently_completed_old_folder(self):
        record = self.submit()
        job_folder = Path(record['input_path']).parent
        old_time = time.time() - 400
        os.utime(job_folder, (old_time, old_time))
        from app.job_queue import update_job
        update_job(record['job_id'], self.redis, status='done', finished_at=time.time())

        cleanup_expired_job_files(self.redis)

        self.assertTrue(job_folder.exists())

    def test_worker_completes_and_releases_user_slot(self):
        record = self.submit()
        with patch('app.tasks.redis_connection', return_value=self.redis):
            result = execute_job(record['job_id'])

        completed = load_job(record['job_id'], self.redis)
        self.assertEqual(completed['status'], 'done')
        self.assertTrue(Path(result['result_path']).exists())
        self.assertFalse(self.redis.sismember('plt-converter:user-jobs:user-a', record['job_id']))

    def test_job_and_preview_routes_are_owner_isolated(self):
        record = self.submit()
        completed = load_job(record['job_id'], self.redis)
        completed['job_type'] = 'pdf_preview'
        completed['status'] = 'done'
        completed['result'] = {'pages': [{'index': 0}]}
        preview = Path(completed['input_path']).parent / 'previews' / f"{record['job_id']}-0.png"
        preview.parent.mkdir(exist_ok=True)
        preview.write_bytes(b'png')
        from app.job_queue import save_job
        save_job(completed, self.redis)

        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis):
            client = app.test_client()
            denied = client.get(
                f"/api/v1/pdf/previews/{record['job_id']}/0.png",
                headers={'X-Client-Key': 'user-b'},
            )
            allowed = client.get(
                f"/api/v1/pdf/previews/{record['job_id']}/0.png",
                headers={'X-Client-Key': 'user-a'},
            )
            allowed.close()

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    def test_routes_accept_chinese_upload_filenames(self):
        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.authorize_job', return_value={
                    'user_id': 1,
                    'request_id': 'chinese-name-request',
                }), \
                patch('app.routes.commit_conversion', return_value={'success': True}):
            client = app.test_client()
            plt_response = client.post(
                '/api/v1/plt/jobs',
                headers={'X-Client-Key': 'chinese-name-plt'},
                data={'file': (io.BytesIO(b'IN;PU0,0;PD1016,1016;'), '纸样.plt')},
            )
            pdf_response = client.post(
                '/api/v1/pdf/preview',
                headers={'X-Client-Key': 'chinese-name-pdf'},
                data={'file': (io.BytesIO(b'%PDF'), '纸样.pdf')},
            )

        self.assertEqual(plt_response.status_code, 200)
        self.assertEqual(pdf_response.status_code, 200)

    def test_routes_reject_non_finite_numeric_options(self):
        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.authorize_job', return_value={
                    'user_id': 1,
                    'request_id': 'invalid-number-request',
                }), \
                patch('app.routes.release_conversion'):
            client = app.test_client()
            response = client.post(
                '/api/v1/plt/jobs',
                headers={'X-Client-Key': 'invalid-number'},
                data={
                    'file': (io.BytesIO(b'IN;PU0,0;PD1016,1016;'), 'sample.plt'),
                    'margin_mm': 'NaN',
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn('margin_mm', response.get_json()['error'])

    def test_route_cancels_job_and_forwards_commit_balance_rejection(self):
        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.authorize_job', return_value={
                    'user_id': 42,
                    'request_id': 'commit-rejected-request',
                }), \
                patch('app.routes.commit_conversion', side_effect=BillingRejected(
                    '布豆余额不足',
                    400,
                    {'required_points': 50, 'current_balance': 20},
                )), \
                patch('app.routes.release_conversion') as release:
            client = app.test_client()
            response = client.post(
                '/api/v1/plt/jobs',
                data={'file': (io.BytesIO(b'IN;PU0,0;PD1016,1016;'), 'sample.plt')},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['required_points'], 50)
        self.assertEqual(response.get_json()['current_balance'], 20)
        job_ids = self.redis.keys('plt-converter:job:*')
        self.assertEqual(len(job_ids), 1)
        job_id = job_ids[0].decode().rsplit(':', 1)[-1]
        self.assertEqual(load_job(job_id, self.redis)['status'], 'cancelled')
        release.assert_called_once_with(42, 'commit-rejected-request', job_id)


if __name__ == '__main__':
    unittest.main()
