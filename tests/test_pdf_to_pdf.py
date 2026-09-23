import io
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import fakeredis
import pymupdf

from app import create_app
from app.billing import BillingRejected
from app.job_queue import confirm_job_billing, job_record_ttl, load_job, submit_job, update_job
from app.tasks import _execute, execute_job, mark_job_stopped


class MemoryLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        return None


class AcquiredThreadLock:
    def __init__(self, lock):
        self.lock = lock

    def release(self):
        self.lock.release()


class PdfToPdfTest(unittest.TestCase):
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

    def tearDown(self):
        self.environment.stop()
        self.temp_dir.cleanup()

    def test_inspect_route_returns_page_count_and_reusable_source_id(self):
        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.pdf_page_count', return_value=7):
            response = app.test_client().post(
                '/api/v1/pdf/repage/inspect',
                headers={'X-Client-Key': 'page-count-client'},
                data={'file': (io.BytesIO(b'%PDF-source'), 'sample.pdf')},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['source_page_count'], 7)
        self.assertTrue(body['source_id'])
        record = load_job(body['source_id'], self.redis)
        self.assertEqual(record['job_type'], 'pdf_to_pdf_source')
        self.assertEqual(record['user_key'], 'page-count-client')
        with open(record['input_path'], 'rb') as source_file:
            self.assertEqual(source_file.read(), b'%PDF-source')

    def test_inspect_accepts_a_pdf_at_the_configured_file_size_limit(self):
        with patch.dict(os.environ, {'PLT_MAX_UPLOAD_MB': '1'}):
            app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.pdf_page_count', return_value=1), \
                patch('app.routes.store_source_upload', return_value={'job_id': 'source-at-limit'}):
            response = app.test_client().post(
                '/api/v1/pdf/repage/inspect',
                headers={'X-Client-Key': 'file-size-client'},
                data={'file': (io.BytesIO(b'x' * (1024 * 1024)), 'limit.pdf')},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['source_id'], 'source-at-limit')

    def test_inspect_rejects_a_pdf_above_the_configured_file_size_limit(self):
        with patch.dict(os.environ, {'PLT_MAX_UPLOAD_MB': '1'}):
            app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.pdf_page_count') as page_count:
            response = app.test_client().post(
                '/api/v1/pdf/repage/inspect',
                headers={'X-Client-Key': 'file-size-client'},
                data={'file': (io.BytesIO(b'x' * (1024 * 1024 + 1)), 'too-large.pdf')},
            )

        self.assertEqual(response.status_code, 413)
        self.assertIn('1MB', response.get_json()['error'])
        page_count.assert_not_called()

    def test_new_inspection_replaces_previous_source_for_same_client(self):
        app = create_app()
        headers = {'X-Client-Key': 'replace-source-client'}
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.pdf_page_count', return_value=1):
            first_response = app.test_client().post(
                '/api/v1/pdf/repage/inspect',
                headers=headers,
                data={'file': (io.BytesIO(b'%PDF-first'), 'first.pdf')},
            )
            second_response = app.test_client().post(
                '/api/v1/pdf/repage/inspect',
                headers=headers,
                data={'file': (io.BytesIO(b'%PDF-second'), 'second.pdf')},
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        first_id = first_response.get_json()['source_id']
        second_id = second_response.get_json()['source_id']
        first_record = load_job(first_id, self.redis)
        second_record = load_job(second_id, self.redis)
        self.assertIsNone(first_record)
        self.assertIsNotNone(second_record)
        self.assertFalse(os.path.exists(os.path.join(self.temp_dir.name, 'jobs', first_id)))
        self.assertTrue(os.path.exists(second_record['input_path']))

    def test_repage_route_reuses_inspected_source_without_second_upload(self):
        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.pdf_page_count', return_value=5):
            inspect_response = app.test_client().post(
                '/api/v1/pdf/repage/inspect',
                headers={'X-Client-Key': 'page-count-client'},
                data={'file': (io.BytesIO(b'%PDF-cached'), 'cached.pdf')},
            )
        self.assertEqual(inspect_response.status_code, 200)
        inspected = inspect_response.get_json()

        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.authorize_job', return_value={
                    'user_id': 7,
                    'request_id': 'cached-source-request',
                }):
            response = app.test_client().post(
                '/api/v1/pdf/repage/jobs',
                headers={'X-Client-Key': 'page-count-client'},
                data={
                    'paper_size': 'A3',
                    'source_id': inspected['source_id'],
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['source_page_count'], 5)
        record = load_job(body['job_id'], self.redis)
        self.assertEqual(record['filename'], 'cached.pdf')
        with open(record['input_path'], 'rb') as source_file:
            self.assertEqual(source_file.read(), b'%PDF-cached')

    def test_route_creates_one_reserved_pdf_to_pdf_job_without_charging_early(self):
        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.pdf_page_count', return_value=12), \
                patch('app.routes.authorize_job', return_value={
                    'user_id': 7,
                    'request_id': 'pdf-to-pdf-request',
                }) as authorize, \
                patch('app.routes.commit_conversion', return_value={'success': True}) as commit:
            response = app.test_client().post(
                '/api/v1/pdf/repage/jobs',
                data={
                    'original_filename': '春季纸样.pdf',
                    'paper_size': 'A2',
                    'file': (io.BytesIO(b'%PDF'), 'tmp.pdf'),
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        record = load_job(body['job_id'], self.redis)
        self.assertEqual(record['job_type'], 'pdf_to_pdf')
        self.assertEqual(record['filename'], '春季纸样.pdf')
        self.assertEqual(record['options']['paper_size'], 'A2')
        self.assertEqual(record['options']['source_page_count'], 12)
        self.assertEqual(body['source_page_count'], 12)
        authorize.assert_called_once_with('pdf_to_pdf')
        commit.assert_not_called()

    def test_ad_required_response_includes_source_page_count(self):
        app = create_app()
        rejection = BillingRejected(
            '今日免费额度已用完',
            402,
            {
                'ad_required': True,
            },
        )
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.pdf_page_count', return_value=9), \
                patch('app.routes.authorize_job', side_effect=rejection):
            response = app.test_client().post(
                '/api/v1/pdf/repage/jobs',
                data={
                    'paper_size': 'A4',
                    'file': (io.BytesIO(b'%PDF'), 'sample.pdf'),
                },
            )

        self.assertEqual(response.status_code, 402)
        body = response.get_json()
        self.assertTrue(body['ad_required'])
        self.assertEqual(body['source_page_count'], 9)

    def test_ad_backed_paper_job_starts_attempt_before_worker_runs(self):
        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.pdf_page_count', return_value=1), \
                patch('app.routes.authorize_job', return_value={
                    'user_id': 7,
                    'request_id': 'ad-paper-request',
                    'access_method': 'ad',
                }), \
                patch('app.routes.commit_conversion', return_value={'success': True}) as commit:
            response = app.test_client().post(
                '/api/v1/pdf/repage/jobs',
                data={'paper_size': 'A4', 'file': (io.BytesIO(b'%PDF'), 'sample.pdf')},
            )
        self.assertEqual(response.status_code, 200)
        record = load_job(response.get_json()['job_id'], self.redis)
        self.assertEqual(record['billing_access_method'], 'ad')
        commit.assert_called_once_with(7, 'ad-paper-request', record['job_id'])

    def test_distinct_billing_request_does_not_reuse_same_fingerprint_job(self):
        with patch('app.job_queue.redis_connection', return_value=self.redis):
            first = submit_job(
                'pdf_to_pdf',
                b'%PDF-source',
                'sample.pdf',
                {'paper_size': 'A4', 'source_page_count': 1},
                'user:7',
                billing_request_id='pdf-to-pdf-request-1',
            )
            confirm_job_billing(first['job_id'], 'user:7')
            second = submit_job(
                'pdf_to_pdf',
                b'%PDF-source',
                'sample.pdf',
                {'paper_size': 'A4', 'source_page_count': 1},
                'user:7',
                billing_request_id='pdf-to-pdf-request-2',
            )

        self.assertNotEqual(first['job_id'], second['job_id'])
        self.assertEqual(second['billing_request_id'], 'pdf-to-pdf-request-2')
        self.assertFalse(second['deduplicated'])

    def test_pdf_to_pdf_active_record_ttl_uses_composite_timeout(self):
        record = {'job_type': 'pdf_to_pdf', 'status': 'queued'}
        with patch.dict(os.environ, {
            'PLT_JOB_RETENTION_SECONDS': '1800',
            'PLT_QUEUE_MAX_PENDING': '20',
            'PLT_JOB_TIMEOUT_SECONDS': '90',
            'PDF_TO_PDF_JOB_TIMEOUT_SECONDS': '210',
        }):
            ttl = job_record_ttl(record)

        self.assertEqual(ttl, 20 * 210 * 2 + 300)

    def test_successful_composite_job_commits_its_single_billing_usage(self):
        with patch('app.job_queue.redis_connection', return_value=self.redis):
            record = submit_job(
                'pdf_to_pdf',
                b'%PDF-source',
                'sample.pdf',
                {'paper_size': 'A2'},
                'user:7',
                billing_request_id='pdf-to-pdf-success',
            )
            confirm_job_billing(record['job_id'], 'user:7')

        with patch('app.tasks.redis_connection', return_value=self.redis), \
                patch('app.tasks.convert_pdf_to_pdf', return_value=(
                    b'%PDF-result',
                    {'paper_size': 'A2'},
                )), patch('app.tasks.commit_conversion', return_value={
                    'success': True,
                }) as commit:
            result = execute_job(record['job_id'])

        commit.assert_called_once_with(7, 'pdf-to-pdf-success', record['job_id'], completed=True)
        self.assertEqual(result['filename'], 'sample-A2.pdf')
        self.assertEqual(load_job(record['job_id'], self.redis)['status'], 'done')

    def test_route_only_accepts_a0_through_a4(self):
        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.authorize_job', return_value={
                    'user_id': 7,
                    'request_id': 'pdf-to-pdf-invalid-paper',
                }), \
                patch('app.routes.release_conversion'):
            response = app.test_client().post(
                '/api/v1/pdf/repage/jobs',
                data={
                    'paper_size': 'Letter',
                    'file': (io.BytesIO(b'%PDF'), 'sample.pdf'),
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn('A0', response.get_json()['error'])

    def test_cancelled_job_reports_when_billing_release_failed(self):
        with patch('app.job_queue.redis_connection', return_value=self.redis):
            record = submit_job(
                'pdf_to_pdf',
                b'%PDF-source',
                'sample.pdf',
                {'paper_size': 'A4', 'source_page_count': 1},
                'user:7',
                billing_request_id='pdf-to-pdf-cancel',
            )
            confirm_job_billing(record['job_id'], 'user:7')

        app = create_app()
        with patch('app.routes.redis_connection', return_value=self.redis), \
                patch('app.job_queue.redis_connection', return_value=self.redis), \
                patch('app.routes.authenticated_user_key', return_value='user:7'), \
                patch('app.routes.release_conversion', return_value=False):
            response = app.test_client().delete(
                f"/api/v1/pdf/repage/jobs/{record['job_id']}"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'cancelled')
        self.assertFalse(response.get_json()['billing_released'])

    def test_async_stopped_job_persists_and_exposes_billing_release_result(self):
        with patch('app.job_queue.redis_connection', return_value=self.redis):
            record = submit_job(
                'pdf_to_pdf',
                b'%PDF-source',
                'sample.pdf',
                {'paper_size': 'A4', 'source_page_count': 1},
                'user:7',
                billing_request_id='pdf-to-pdf-stopped',
            )
            confirm_job_billing(record['job_id'], 'user:7')
            update_job(record['job_id'], self.redis, status='processing')

        stopped_job = type('StoppedJob', (), {
            'args': (record['job_id'],),
            'id': f"{record['job_id']}:0",
        })()
        with patch('app.tasks.release_conversion', return_value=False):
            mark_job_stopped(stopped_job, self.redis)

        cancelled = load_job(record['job_id'], self.redis)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIs(cancelled['billing_released'], False)

        app = create_app()
        with patch('app.routes.owned_job', return_value=cancelled):
            response = app.test_client().get(
                f"/api/v1/pdf/repage/jobs/{record['job_id']}"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.get_json()['billing_released'], False)

    def test_cancel_detected_after_conversion_persists_billing_release_result(self):
        with patch('app.job_queue.redis_connection', return_value=self.redis):
            record = submit_job(
                'pdf_to_pdf',
                b'%PDF-source',
                'sample.pdf',
                {'paper_size': 'A4', 'source_page_count': 1},
                'user:7',
                billing_request_id='pdf-to-pdf-cancel-after-work',
            )
            confirm_job_billing(record['job_id'], 'user:7')

        def finish_after_cancel(_source, _options):
            update_job(
                record['job_id'],
                self.redis,
                status='cancelling',
                cancel_requested=True,
            )
            return b'%PDF-result', {'paper_size': 'A4'}

        with patch('app.tasks.redis_connection', return_value=self.redis), \
                patch('app.tasks.convert_pdf_to_pdf', side_effect=finish_after_cancel), \
                patch('app.tasks.release_conversion', return_value=False):
            result = execute_job(record['job_id'])

        self.assertIsNone(result)
        cancelled = load_job(record['job_id'], self.redis)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIs(cancelled['billing_released'], False)

    def test_progress_update_cannot_overwrite_concurrent_cancellation(self):
        with patch('app.job_queue.redis_connection', return_value=self.redis):
            record = submit_job(
                'pdf_to_pdf',
                b'%PDF-source',
                'sample.pdf',
                {'paper_size': 'A4', 'source_page_count': 1},
                'user:7',
                billing_request_id='pdf-to-pdf-progress-race',
            )
            confirm_job_billing(record['job_id'], 'user:7')
            update_job(record['job_id'], self.redis, status='processing', progress=5)

        from app import job_queue

        original_load_job = job_queue.load_job
        original_save_job = job_queue.save_job
        shared_lock = threading.Lock()
        worker_loaded = threading.Event()
        cancel_attempted = threading.Event()
        cancel_written = threading.Event()
        progress_load_blocked = threading.Event()
        thread_errors = []

        def acquire_shared_lock(_job_id, _connection):
            if not shared_lock.acquire(timeout=2):
                raise AssertionError('job lock was not released')
            return AcquiredThreadLock(shared_lock)

        def interleaving_load(job_id, connection=None):
            current = original_load_job(job_id, connection)
            if (
                threading.current_thread().name == 'conversion-worker'
                and not progress_load_blocked.is_set()
            ):
                progress_load_blocked.set()
                worker_loaded.set()
                self.assertTrue(cancel_attempted.wait(1))
                cancel_written.wait(0.1)
            return current

        def cancel_while_progress_is_saving():
            self.assertTrue(worker_loaded.wait(1))
            cancel_attempted.set()
            lock = acquire_shared_lock(record['job_id'], self.redis)
            try:
                latest = original_load_job(record['job_id'], self.redis)
                latest.update(status='cancelling', cancel_requested=True, progress=0)
                original_save_job(latest, self.redis)
                cancel_written.set()
            finally:
                lock.release()

        def converted(_source, _options):
            self.assertTrue(cancel_written.wait(1))
            return b'%PDF-result', {'paper_size': 'A4'}

        def run_in_thread(target):
            try:
                target()
            except BaseException as error:
                thread_errors.append(error)

        worker = threading.Thread(
            target=lambda: run_in_thread(lambda: _execute(record, self.redis)),
            name='conversion-worker',
        )
        canceller = threading.Thread(
            target=lambda: run_in_thread(cancel_while_progress_is_saving),
        )
        with patch('app.job_queue.load_job', side_effect=interleaving_load), \
                patch('app.tasks.acquire_job_lock', side_effect=acquire_shared_lock), \
                patch('app.tasks.convert_pdf_to_pdf', side_effect=converted):
            worker.start()
            canceller.start()
            worker.join(2)
            canceller.join(2)

        self.assertFalse(worker.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertEqual(thread_errors, [])
        latest = original_load_job(record['job_id'], self.redis)
        self.assertEqual(latest['status'], 'cancelling')
        self.assertTrue(latest['cancel_requested'])
        self.assertEqual(latest['progress'], 0)

    def test_composite_conversion_reuses_both_existing_converters_in_memory(self):
        from app.services.pdf_to_pdf import convert_pdf_to_pdf

        suggestion = {
            'confidence': 'high',
            'rows': 2,
            'columns': 2,
            'order': 'column',
            'page_slots': [0, 1, None, 2],
            'crop_margins_mm': {'top': 5, 'right': 5, 'bottom': 5, 'left': 5},
            'output_rotation': 0,
            'source': 'vector_seams',
        }
        parsed = {'metrics': {'units_per_inch': 1016}, 'shapes': []}
        with patch('app.services.pdf_to_pdf.pdf_page_count', return_value=3), \
                patch('app.services.pdf_to_pdf.optimize_pdf_layout', return_value=suggestion), \
                patch('app.services.pdf_to_pdf.convert_pdf_to_plt', return_value=(b'IN;SP0;', {'rows': 2})) as to_plt, \
                patch('app.services.pdf_to_pdf.parse_plt', return_value=parsed) as parse, \
                patch('app.services.pdf_to_pdf.render_pdf', return_value=(b'%PDF-result', {'page_count': 3})) as to_pdf:
            pdf, result = convert_pdf_to_pdf(
                b'%PDF-source',
                {'paper_size': 'A2', 'source_page_count': 3},
            )

        self.assertEqual(pdf, b'%PDF-result')
        source_options = to_plt.call_args.args[1]
        self.assertEqual(source_options['rows'], 2)
        self.assertEqual(source_options['columns'], 2)
        self.assertEqual(source_options['page_slots'], [0, 1, None, 2])
        self.assertEqual(source_options['enabled_pages'], [0, 1, 2])
        self.assertEqual(source_options['crop_left_mm'], 5)
        parse.assert_called_once_with(b'IN;SP0;', 1016)
        target_options = to_pdf.call_args.args[1]
        self.assertEqual(target_options['paper_size'], 'A2')
        self.assertEqual(target_options['orientation'], 'auto')
        self.assertEqual(target_options['margin_mm'], 10)
        self.assertTrue(target_options['show_page_number'])
        self.assertEqual(result['source_layout'], {'rows': 2})
        self.assertEqual(result['target_layout'], {'page_count': 3})
        self.assertEqual(result['source_page_count'], 3)
        self.assertEqual(result['output_page_count'], 3)

    def test_composite_conversion_refuses_uncertain_layout(self):
        from app.services.pdf_to_pdf import convert_pdf_to_pdf

        with patch('app.services.pdf_to_pdf.pdf_page_count', return_value=6), \
                patch('app.services.pdf_to_pdf.optimize_pdf_layout', return_value={
            'confidence': 'low',
            'reason': '接缝证据不足',
        }), patch('app.services.pdf_to_pdf.convert_pdf_to_plt') as to_plt:
            with self.assertRaisesRegex(ValueError, '无法可靠识别'):
                convert_pdf_to_pdf(b'%PDF-source', {'paper_size': 'A4'})

        to_plt.assert_not_called()

    def test_single_page_pdf_does_not_require_seam_analysis(self):
        from app.services.pdf_to_pdf import convert_pdf_to_pdf

        parsed = {'metrics': {'units_per_inch': 1016}, 'shapes': []}
        with patch('app.services.pdf_to_pdf.pdf_page_count', return_value=1), \
                patch('app.services.pdf_to_pdf.optimize_pdf_layout') as optimize, \
                patch('app.services.pdf_to_pdf.convert_pdf_to_plt', return_value=(b'IN;SP0;', {})) as to_plt, \
                patch('app.services.pdf_to_pdf.parse_plt', return_value=parsed), \
                patch('app.services.pdf_to_pdf.render_pdf', return_value=(b'%PDF-result', {})):
            convert_pdf_to_pdf(b'%PDF-source', {'paper_size': 'A4'})

        optimize.assert_not_called()
        source_options = to_plt.call_args.args[1]
        self.assertEqual(source_options['rows'], 1)
        self.assertEqual(source_options['columns'], 1)
        self.assertEqual(source_options['page_slots'], [0])

    def test_failed_composite_job_releases_its_single_billing_usage(self):
        with patch('app.job_queue.redis_connection', return_value=self.redis):
            record = submit_job(
                'pdf_to_pdf',
                b'%PDF-source',
                'sample.pdf',
                {'paper_size': 'A4'},
                'user:7',
                billing_request_id='pdf-to-pdf-failed',
            )
            confirm_job_billing(record['job_id'], 'user:7')

        with patch('app.tasks.redis_connection', return_value=self.redis), \
                patch('app.tasks.convert_pdf_to_pdf', side_effect=ValueError('无法可靠识别')), \
                patch('app.tasks.release_conversion') as release:
            with self.assertRaisesRegex(ValueError, '无法可靠识别'):
                execute_job(record['job_id'])

        release.assert_called_once_with(7, 'pdf-to-pdf-failed', record['job_id'])

    def test_real_single_page_pdf_is_repaged_to_all_supported_sizes(self):
        from app.services.pdf_to_pdf import convert_pdf_to_pdf

        source_document = pymupdf.open()
        source_page = source_document.new_page(width=210 * 72 / 25.4, height=297 * 72 / 25.4)
        source_page.draw_line(
            pymupdf.Point(20 * 72 / 25.4, 20 * 72 / 25.4),
            pymupdf.Point(190 * 72 / 25.4, 277 * 72 / 25.4),
        )
        source = source_document.tobytes()
        source_document.close()

        expected_sizes = {
            'A0': (841, 1189),
            'A1': (594, 841),
            'A2': (420, 594),
            'A3': (297, 420),
            'A4': (210, 297),
        }
        for paper_size, (width_mm, height_mm) in expected_sizes.items():
            with self.subTest(paper_size=paper_size):
                output, result = convert_pdf_to_pdf(source, {'paper_size': paper_size})
                output_document = pymupdf.open(stream=output, filetype='pdf')
                try:
                    self.assertEqual(output_document.page_count, 1)
                    page = output_document.load_page(0)
                    self.assertAlmostEqual(page.rect.width * 25.4 / 72, width_mm, places=1)
                    self.assertAlmostEqual(page.rect.height * 25.4 / 72, height_mm, places=1)
                    self.assertTrue(page.get_drawings())
                finally:
                    output_document.close()
                self.assertEqual(result['paper_size'], paper_size)
                self.assertEqual(result['layout_source'], 'single_page')
                self.assertEqual(result['source_page_count'], 1)
                self.assertEqual(result['output_page_count'], 1)


if __name__ == '__main__':
    unittest.main()
