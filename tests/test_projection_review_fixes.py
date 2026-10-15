"""Regression tests for the projection review fixes implemented this round.

Covers B0 (one live pattern per owner), B1 (slow uploads do not hold the global
parser slot), B2 (an account cannot occupy the analysis queue / its own
analyzing budget) and B5 (concurrency and Redis failures never surface as 500).
"""
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf as fitz
from redis.exceptions import RedisError

import test_projection as fixtures
import test_projection_resources as resources
from app.projection_layout import projection_queue, submit_analysis
from app.projection_store import ProjectionRejected
from test_projection import BASE


class ReviewFixTests(unittest.TestCase):
    setUp = fixtures.ProjectionTests.setUp
    identity = fixtures.ProjectionTests.identity
    upload = fixtures.ProjectionTests.upload
    tile = fixtures.ProjectionTests.tile
    metadata = fixtures.ProjectionTests.metadata
    renew = fixtures.ProjectionTests.renew
    create = resources.ResourceTests.create
    policy_restore = resources.ResourceTests.policy_restore
    hanging_process = resources.ResourceTests.hanging_process
    queue_environment = fixtures.ProjectionTests.queue_environment
    analyze = fixtures.ProjectionTests.analyze
    analysis_status = fixtures.ProjectionTests.analysis_status
    execute_analysis = fixtures.ProjectionTests.execute_analysis

    # ------------------------------ B0 ------------------------------
    def test_b0_upload_replaces_previous_owner_document_and_frees_capacity(self):
        self.ledger.policy.owner_max_documents = 1
        first = self.upload().get_json()['document_id']
        second = self.upload().get_json()['document_id']
        self.assertNotEqual(first, second)
        self.assertEqual(self.tile(first).status_code, 404)
        self.assertEqual(self.renew(first).status_code, 404)
        self.assertEqual(self.client.get(f'{BASE}/{first}', headers={'Authorization': 'x'}).status_code, 404)
        self.assertEqual(self.client.delete(f'{BASE}/{first}', headers={'Authorization': 'x'}).status_code, 404)
        self.assertIsNone(self.metadata(first))
        self.assertEqual(self.tile(second).status_code, 200)

    def test_b0_upload_reclaims_orphan_session_without_client_response(self):
        # A completed session whose client never received the 201 reply still
        # consumed owner capacity; the next upload must retire it.
        orphan = self.create()
        self.ledger.policy.owner_max_documents = 1
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(self.metadata(orphan))
        self.assertEqual(self.tile(orphan).status_code, 404)

    def test_b0_replacing_document_mid_render_fails_old_operation_without_500(self):
        from app.projection_runtime import run
        doc = self.create()
        lease = self.ledger.reserve(self.owner, 'tile', doc, 0)
        mock, processes = self.hanging_process()
        errors = []

        def render():
            try:
                run(self.ledger, lease, 'tile', self.ledger.source_path(doc), deadline=10)
            except ProjectionRejected as error:
                errors.append(error.code)
            finally:
                self.ledger.release(lease)

        with mock:
            thread = threading.Thread(target=render)
            thread.start()
            start = time.monotonic()
            while not processes and time.monotonic() - start < 1:
                time.sleep(.01)
            self.assertTrue(processes)
            # upload_document uses exactly this call to retire the prior pattern.
            self.ledger.delete_all(self.owner)
            self.assertEqual(self.tile(doc).status_code, 404)
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['projection_result_stale'])
        self.assertIsNone(self.metadata(doc))

    # ------------------------------ B1 ------------------------------
    def test_b1_slow_upload_reserves_without_holding_global_parse_slot(self):
        self.policy_restore()
        self.ledger.policy.global_parsers = 1
        # Two bodies may be arriving at once; neither holds a parser slot yet.
        first = self.ledger.reserve(self.owner, 'upload')
        second = self.ledger.reserve('user:2', 'upload')
        self.ledger.begin_parse(first)
        self.assertTrue(self.ledger.valid_lease(first))
        self.assertEqual(self.ledger.lease_path(first).parent.name, 'staging')
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.begin_parse(second)
        self.assertEqual(error.exception.code, 'projection_parser_busy')
        self.ledger.release(first)
        self.ledger.release(second)

    def test_b1_upload_parser_busy_after_body_releases_staging_and_lease(self):
        self.policy_restore()
        self.ledger.policy.global_parsers = 1
        holder = self.ledger.reserve(self.owner, 'upload')
        self.ledger.begin_parse(holder)
        response = self.upload()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()['error_code'], 'projection_parser_busy')
        self.assertEqual(list((self.ledger.root / 'staging').iterdir()), [])
        self.ledger.release(holder)

    def test_b1_upload_parse_kind_is_valid_and_reconciled_from_staging(self):
        lease = self.ledger.reserve(self.owner, 'upload')
        self.ledger.begin_parse(lease)
        self.assertTrue(self.ledger.valid_lease(lease))
        self.assertEqual(self.ledger.lease_path(lease).parent.name, 'staging')
        with self.ledger.transaction() as db:
            db.execute('UPDATE leases SET supervisor=? WHERE id=?', (99999999, lease))
        self.ledger.reconcile()
        self.assertIsNone(self.ledger.lease(lease))

    # ------------------------------ B2 ------------------------------
    def test_b2_second_document_analysis_rejected_for_owner_busy(self):
        first = self.create()
        second = self.create()
        environment, redis = self.queue_environment()
        with environment:
            started = self.analyze(first)
            self.assertEqual(started.status_code, 202)
            busy = self.analyze(second)
            self.assertEqual(busy.status_code, 429)
            self.assertEqual(busy.get_json()['error_code'], 'projection_analysis_owner_busy')
            self.assertIn('Retry-After', busy.headers)

    def test_b2_mode_switch_cancels_and_removes_previous_queued_job(self):
        doc = self.create()
        environment, redis = self.queue_environment()
        with environment:
            first = self.analyze(doc).get_json()['analysis_id']
            self.assertEqual(projection_queue(redis).count, 1)
            second = self.analyze(doc, 'original').get_json()['analysis_id']
            self.assertNotEqual(first, second)
            self.assertEqual(projection_queue(redis).count, 1)
            self.assertNotIn(first, projection_queue(redis).job_ids)

    # ------------------------------ B5 ------------------------------
    def test_b5_lease_path_after_concurrent_delete_is_409_lease_lost(self):
        doc = self.create()
        lease = self.ledger.reserve(self.owner, 'tile', doc, 0)
        self.assertTrue(self.ledger.delete(doc, self.owner))
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.lease_path(lease)
        self.assertEqual(error.exception.code, 'projection_lease_lost')
        self.assertEqual(error.exception.status, 409)

    def test_b5_reserve_generation_mismatch_is_409_stale_and_missing_is_404(self):
        doc = self.create()
        with self.assertRaises(ProjectionRejected) as stale:
            self.ledger.reserve(self.owner, 'tile', doc, 999)
        self.assertEqual(stale.exception.code, 'projection_result_stale')
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(ProjectionRejected) as missing:
            self.ledger.reserve(self.owner, 'tile', '0' * 64, 0)
        self.assertEqual(missing.exception.code, 'projection_not_found')
        self.assertEqual(missing.exception.status, 404)

    def test_b5_analysis_status_redis_error_is_503_worker_unavailable(self):
        doc = self.create()
        environment, redis = self.queue_environment()
        with environment:
            analysis_id = self.analyze(doc).get_json()['analysis_id']
            job = projection_queue(redis).fetch_job(analysis_id)
            with patch('app.projection_layout.Job.fetch', return_value=job), \
                 patch.object(job, 'get_status', return_value='queued'), \
                 patch('app.projection_layout.has_live_projection_worker', side_effect=RedisError('down')):
                response = self.analysis_status(doc)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['error_code'], 'projection_worker_unavailable')
        self.assertIn('Retry-After', response.headers)

    def test_b5_submit_analysis_concurrent_delete_is_404_not_attribute_error(self):
        doc = self.create()
        meta = self.ledger.load(doc, self.owner)
        meta['analysis'] = {'analysis_id': 'controlled', 'status': 'analyzing',
                            'phase': 'queued', 'mode': 'current'}
        self.assertTrue(self.ledger.update(doc, self.owner, meta, 0))
        stale = self.ledger.load(doc, self.owner)

        def deleted(*args, **kwargs):
            self.ledger.delete(doc, self.owner)
            return {}

        with patch('app.projection_layout.refresh_analysis_failure', side_effect=deleted):
            with self.assertRaises(ProjectionRejected) as error:
                submit_analysis(self.ledger, doc, self.owner, stale, 'original')
        self.assertEqual(error.exception.status, 404)

    def test_b5_app_level_redis_error_is_503_worker_unavailable(self):
        from flask import Blueprint
        probe = Blueprint('review_probe', __name__)

        @probe.get('/review-probe-redis')
        def raise_redis():
            raise RedisError('backend down')

        self.app.register_blueprint(probe)
        response = self.client.get('/review-probe-redis')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['error_code'], 'projection_worker_unavailable')

    # ------------------------------ B2b ------------------------------
    def test_b2b_upload_retiring_document_discards_queued_analysis(self):
        doc = self.create()
        environment, redis = self.queue_environment()
        with environment:
            analysis_id = self.analyze(doc).get_json()['analysis_id']
            queue = projection_queue(redis)
            self.assertEqual(queue.count, 1)
            self.assertIn(analysis_id, queue.job_ids)
            new_doc = self.upload().get_json()['document_id']
            self.assertNotEqual(new_doc, doc)
            self.assertNotIn(analysis_id, queue.job_ids)
            self.assertEqual(queue.count, 0)

    def test_b2b_delete_document_discards_queued_analysis(self):
        doc = self.create()
        environment, redis = self.queue_environment()
        with environment:
            analysis_id = self.analyze(doc).get_json()['analysis_id']
            queue = projection_queue(redis)
            self.assertEqual(queue.count, 1)
            self.assertIn(analysis_id, queue.job_ids)
            response = self.client.delete(f'{BASE}/{doc}', headers={'Authorization': 'x'})
            self.assertEqual(response.status_code, 204)
            self.assertNotIn(analysis_id, queue.job_ids)
            self.assertEqual(queue.count, 0)

    # ------------------------------ B3 ------------------------------
    def test_b3_child_environment_excludes_identity_and_redis_secrets(self):
        from app.projection import PT_PER_MM
        from app.projection_runtime import run
        import subprocess as sp
        doc = self.create()
        lease = self.ledger.reserve(self.owner, 'tile', doc, 0)
        parameters = {'values': {'x_mm': 0, 'y_mm': 0,
                                 'width_mm': 200 / PT_PER_MM, 'height_mm': 300 / PT_PER_MM,
                                 'width_px': 200, 'height_px': 300, 'rotation_deg': 0},
                      'page_index': 0, 'layout': None}
        captured = {}
        real_popen = sp.Popen

        def recording_popen(*args, **kwargs):
            captured['env'] = kwargs.get('env')
            return real_popen(*args, **kwargs)

        secret_env = {
            'CONVERSION_SERVICE_TOKEN': 'super-secret-token',
            'PROJECTOR_REDIS_URL': 'redis://internal:6379/0',
            'WX_BACKEND_URL': 'https://api.fengrenjiyi.com',
            'PDF_MAX_DRAWINGS': '12345',
        }
        with patch.dict(os.environ, secret_env), \
             patch('app.projection_runtime.subprocess.Popen', side_effect=recording_popen):
            png = run(self.ledger, lease, 'tile', self.ledger.source_path(doc), parameters)
        self.ledger.release(lease)
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertIsNotNone(captured.get('env'))
        self.assertEqual(captured['env']['PDF_MAX_DRAWINGS'], '12345')
        self.assertNotIn('CONVERSION_SERVICE_TOKEN', captured['env'])
        self.assertNotIn('PROJECTOR_REDIS_URL', captured['env'])
        self.assertNotIn('WX_BACKEND_URL', captured['env'])

    def test_b3_child_environment_whitelist_passes_needed_limits_only(self):
        from app.projection_runtime import _child_environment
        with patch.dict(os.environ, {
            'PATH': '/usr/bin',
            'LANG': 'en_US.UTF-8',
            'PYTHONDONTWRITEBYTECODE': '1',
            'PDF_MAX_TOTAL_PIXELS': '1000000',
            'PDF_LAYOUT_OPTIMIZER_MAX_CROP_MM': '7',
            'CONVERSION_SERVICE_TOKEN': 'secret',
            'WX_BACKEND_URL': 'https://api.fengrenjiyi.com',
            'PROJECTOR_REDIS_URL': 'redis://internal:6379/0',
        }):
            environment = _child_environment()
        self.assertIn('PATH', environment)
        self.assertIn('LANG', environment)
        self.assertIn('PYTHONDONTWRITEBYTECODE', environment)
        self.assertIn('PDF_MAX_TOTAL_PIXELS', environment)
        self.assertIn('PDF_LAYOUT_OPTIMIZER_MAX_CROP_MM', environment)
        self.assertNotIn('CONVERSION_SERVICE_TOKEN', environment)
        self.assertNotIn('WX_BACKEND_URL', environment)
        self.assertNotIn('PROJECTOR_REDIS_URL', environment)

    # ------------------------------ B6 ------------------------------
    def test_b6_analysis_failure_status_keeps_inflight_tile_generation(self):
        doc = self.create()
        meta = self.ledger.load(doc, self.owner)
        meta['analysis'] = {'analysis_id': 'controlled-analysis', 'status': 'analyzing',
                            'phase': 'queued', 'mode': 'current'}
        self.assertTrue(self.ledger.update(doc, self.owner, meta, meta['_generation']))
        generation = self.ledger.load(doc, self.owner)['_generation']
        lease = self.ledger.reserve(self.owner, 'tile', doc, generation)
        job = SimpleNamespace(get_status=lambda **kw: 'failed', started_at=None)
        with patch('app.projection_layout.projection_connection', return_value=object()), \
             patch('app.projection_layout.Job.fetch', return_value=job):
            response = self.analysis_status(doc)
        self.assertEqual(response.get_json()['status'], 'failed')
        self.assertEqual(self.ledger.load(doc, self.owner)['_generation'], generation)
        self.assertTrue(self.ledger.valid_lease(lease))
        self.ledger.release(lease)
        self.assertEqual(self.tile(doc).status_code, 200)

    def test_b6_mode_switch_keeps_inflight_tile_generation(self):
        doc = self.create()
        environment, redis = self.queue_environment()
        with environment:
            first = self.analyze(doc).get_json()['analysis_id']
            generation = self.ledger.load(doc, self.owner)['_generation']
            lease = self.ledger.reserve(self.owner, 'tile', doc, generation)
            second = self.analyze(doc, 'original').get_json()['analysis_id']
            self.assertNotEqual(first, second)
            self.assertEqual(self.ledger.load(doc, self.owner)['_generation'], generation)
            self.assertTrue(self.ledger.valid_lease(lease))
            self.ledger.release(lease)

    # ------------------------------ B7 ------------------------------
    def _exiting_child_writing(self, folder, payload):
        class ExitingProcess:
            pid = 424242
            returncode = 0
            def poll(self): return 0
            def wait(self, timeout=None): return 0

        def launch(*args, **kwargs):
            (folder / 'error.json').write_text(json.dumps(payload))
            return ExitingProcess()
        return launch

    def _run_child_error(self, operation, payload):
        from app.projection_runtime import run
        doc = self.create()
        lease = self.ledger.reserve(self.owner, operation, doc, 0)
        folder = self.ledger.lease_path(lease)
        launch = self._exiting_child_writing(folder, payload)
        with patch('app.projection_runtime.subprocess.Popen', side_effect=launch), \
             patch('app.projection_runtime.terminate'):
            with self.assertRaises(ProjectionRejected) as error:
                run(self.ledger, lease, operation, self.ledger.source_path(doc))
        self.ledger.release(lease)
        return error.exception

    def test_b7_analysis_internal_error_text_is_not_exposed(self):
        doc = self.create()
        environment, redis = self.queue_environment()
        with environment:
            analysis_id = self.analyze(doc).get_json()['analysis_id']
            with patch('app.projection_runtime.run',
                       side_effect=RuntimeError('internal traceback /srv/secret.py')):
                result = self.execute_analysis(doc, analysis_id, 'current')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], '排版分析失败，请重试或手动排版')
        self.assertNotIn('secret', result['error'])
        self.assertNotIn('traceback', result['error'])

    def test_b7_public_value_error_text_is_returned_verbatim(self):
        message = 'PDF 页数必须在 1 至 200 页之间，不会截断页面'
        error = self._run_child_error('parse', {'error': message, 'public': True})
        self.assertEqual(error.code, 'projection_invalid_pdf')
        self.assertEqual(error.args[0], message)

    def test_b7_public_error_text_is_truncated_to_500_chars(self):
        message = 'PDF 文件无法读取或已损坏 ' + '详' * 600
        error = self._run_child_error('parse', {'error': message, 'public': True})
        self.assertEqual(error.args[0], message[:500])
        self.assertEqual(len(error.args[0]), 500)

    def test_b7_runtime_error_text_is_replaced(self):
        error = self._run_child_error(
            'tile', {'error': 'Traceback internal /srv/secret.py detail', 'public': False})
        self.assertEqual(error.code, 'projection_invalid_pdf')
        self.assertNotIn('Traceback', error.args[0])
        self.assertNotIn('secret', error.args[0])
        self.assertEqual(error.args[0], 'PDF 画面渲染失败，请重试或更换文件')

    def test_b7_legacy_error_json_without_public_is_replaced(self):
        error = self._run_child_error(
            'tile', {'error': 'Traceback internal /srv/secret.py detail'})
        self.assertEqual(error.code, 'projection_invalid_pdf')
        self.assertNotIn('Traceback', error.args[0])
        self.assertNotIn('secret', error.args[0])
        self.assertEqual(error.args[0], 'PDF 画面渲染失败，请重试或更换文件')

    # ------------------------------ B8 ------------------------------
    def test_b8_non_rotated_layout_seam_keeps_edge_column(self):
        from app.projection import PT_PER_MM
        mm = PT_PER_MM
        doc = fitz.open()
        left = doc.new_page(width=100 * mm, height=100 * mm)
        left.draw_rect(fitz.Rect(80 * mm, 0, 100 * mm, 100 * mm), color=None, fill=(0, 0, 0))
        right = doc.new_page(width=100 * mm, height=100 * mm)
        # Dark ink on both pages near the seam, but the later page stays blank in
        # the fractional column so an opaque paste would visibly erase the left
        # page's edge column.
        right.draw_rect(fitz.Rect(5 * mm, 0, 15 * mm, 100 * mm), color=None, fill=(0, 0, 0))
        source = doc.tobytes()
        doc.close()
        doc_id = self.upload(source).get_json()['document_id']
        layout = self.client.post(f'{BASE}/{doc_id}/layout', headers={'Authorization': 'x'},
                                  data={'method': 'manual', 'rows': 1, 'columns': 2, 'order': 'row'})
        self.assertEqual(layout.status_code, 200)
        # 200mm mapped to 199px lands the page boundary at x = 99.5px: the left
        # page owns column 99 and the later page must not erase it.
        response = self.tile(doc_id, view='layout', x_mm=0, y_mm=0, width_mm=200,
                             height_mm=100, width_px=199, height_px=100)
        self.assertEqual(response.status_code, 200)
        pix = fitz.Pixmap(response.data)
        self.assertEqual((pix.width, pix.height), (199, 100))
        self.assertLess(min(pix.pixel(98, 50)), 60)
        self.assertLess(min(pix.pixel(99, 50)), 200)
        # The right page's own ink past the seam must be present too.
        self.assertLess(min(pix.pixel(110, 50)), 60)
        self.assertEqual(pix.pixel(140, 50), (255, 255, 255))

    def test_b8_three_column_layout_keeps_both_fractional_seams(self):
        from app.projection import PT_PER_MM
        mm = PT_PER_MM
        doc = fitz.open()
        # Pages 0 and 1 carry ink flush to their right edge; every later page is
        # blank at its own left edge, so an opaque paste would visibly erase the
        # ink that owns each fractional seam column.
        for dark_right in (True, True, False):
            page = doc.new_page(width=100 * mm, height=100 * mm)
            if dark_right:
                page.draw_rect(fitz.Rect(85 * mm, 0, 100 * mm, 100 * mm), color=None, fill=(0, 0, 0))
        source = doc.tobytes()
        doc.close()
        doc_id = self.upload(source).get_json()['document_id']
        layout = self.client.post(f'{BASE}/{doc_id}/layout', headers={'Authorization': 'x'},
                                  data={'method': 'manual', 'rows': 1, 'columns': 3, 'order': 'row'})
        self.assertEqual(layout.status_code, 200)
        # 300mm mapped to 299px puts both seams (100mm, 200mm) on fractional pixels.
        response = self.tile(doc_id, view='layout', x_mm=0, y_mm=0, width_mm=300,
                             height_mm=100, width_px=299, height_px=100)
        self.assertEqual(response.status_code, 200)
        pix = fitz.Pixmap(response.data)
        self.assertEqual((pix.width, pix.height), (299, 100))
        # Seams land at x = 99.67px and x = 199.33px: the flush dark column before
        # each seam and the seam's own overlapping column must both survive.
        for owner_column, seam_column in ((98, 99), (198, 199)):
            self.assertLess(min(pix.pixel(owner_column, 50)), 60)
            self.assertLess(min(pix.pixel(seam_column, 50)), 200)
        self.assertLess(min(pix.pixel(190, 50)), 60)
        self.assertEqual(pix.pixel(250, 50), (255, 255, 255))
        # Outside the seam strips the page keeps MuPDF's exact opaque bytes.
        scale = 299 / (300 * mm)
        with fitz.open(stream=source, filetype='pdf') as reference_doc:
            reference = reference_doc[0].get_pixmap(
                matrix=fitz.Matrix(scale, 0, 0, scale, 0, 0),
                clip=fitz.Rect(0, 0, 100 * mm, 100 * mm), colorspace=fitz.csRGB, alpha=False)
        for y in range(reference.height):
            for column in range(reference.width - 1):
                offset = (y * reference.width + column) * 3
                self.assertEqual(pix.samples[(y * 299 + column) * 3:(y * 299 + column) * 3 + 3],
                                 reference.samples[offset:offset + 3])


    # ------------------------------ C1 ------------------------------
    def _active_documents(self, owner=None):
        with self.ledger.transaction() as db:
            return sorted(r['id'] for r in db.execute(
                "SELECT id FROM sessions WHERE owner=? AND state='active'", (owner or self.owner,)))

    def _reserved_upload(self, owner=None):
        owner = owner or self.owner
        # Keep one parser slot free so the competing public upload in these
        # tests is rejected only by the supersede rule, not by parser_busy.
        self.ledger.policy.global_parsers = max(self.ledger.policy.global_parsers, 2)
        lease = self.ledger.reserve(owner, 'upload')
        self.ledger.begin_parse(lease)
        staging = self.ledger.lease_path(lease)
        staging.mkdir(mode=0o700)
        source = staging / 'source.pdf'
        source.write_bytes(fixtures.pdf_bytes())
        return lease, staging, source

    def test_c1_later_reserved_upload_supersedes_inflight_earlier(self):
        # A is still parsing (no session yet) when B reserves and completes.
        lease, staging, source = self._reserved_upload()
        second = self.upload()
        self.assertEqual(second.status_code, 201)
        document = second.get_json()['document_id']
        meta = {'owner': self.owner, 'expires_at': time.time() + 3600, 'filename': 'earlier.pdf'}
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.complete_upload(lease, self.owner, source, meta, [])
        self.assertEqual(error.exception.code, 'projection_upload_superseded')
        self.assertEqual(error.exception.status, 409)
        self.ledger.release(lease)
        self.assertFalse(staging.exists())
        self.assertEqual(self._active_documents(), [document])

    def test_c1_earlier_reserved_upload_cannot_replace_completed_newer(self):
        # B completes first; A was reserved before B, so A loses on completion.
        lease, staging, source = self._reserved_upload()
        document = self.upload().get_json()['document_id']
        meta = {'owner': self.owner, 'expires_at': time.time() + 3600, 'filename': 'earlier.pdf'}
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.complete_upload(lease, self.owner, source, meta, [])
        self.assertEqual(error.exception.code, 'projection_upload_superseded')
        self.ledger.release(lease)
        self.assertEqual(self._active_documents(), [document])
        self.assertEqual(self.tile(document).status_code, 200)

    def test_c1_completed_newer_upload_retires_earlier_document(self):
        earlier = self.create()
        lease, staging, source = self._reserved_upload()
        meta = {'owner': self.owner, 'expires_at': time.time() + 3600, 'filename': 'later.pdf'}
        document = self.ledger.complete_upload(lease, self.owner, source, meta, [])
        self.ledger.release(lease)
        self.assertNotEqual(document, earlier)
        self.assertEqual(self._active_documents(), [document])
        self.assertEqual(self.tile(earlier).status_code, 404)
        self.assertIsNone(self.metadata(earlier))

    def test_c1_retired_earlier_document_returns_analysis_id_to_route(self):
        from app.projection_layout import discard_analysis_job
        earlier = self.create()
        environment, redis = self.queue_environment()
        with environment:
            analysis_id = self.analyze(earlier).get_json()['analysis_id']
            queue = projection_queue(redis)
            self.assertIn(analysis_id, queue.job_ids)
            lease, staging, source = self._reserved_upload()
            meta = {'owner': self.owner, 'expires_at': time.time() + 3600, 'filename': 'later.pdf'}
            discarded = []
            document = self.ledger.complete_upload(lease, self.owner, source, meta, discarded)
            self.ledger.release(lease)
            self.assertEqual(discarded, [analysis_id])
            for retired_id in discarded:
                discard_analysis_job(retired_id)
            self.assertNotIn(analysis_id, queue.job_ids)
            self.assertEqual(queue.count, 0)
        self.assertEqual(self._active_documents(), [document])
        self.assertEqual(self.tile(earlier).status_code, 404)

    def test_c1_owner_capacity_counts_parsing_and_two_uploads_leave_one(self):
        self.policy_restore()
        self.ledger.policy.owner_max_documents = 2
        self.ledger.policy.owner_source_bytes = 100 * 1024 * 1024
        self.ledger.policy.global_parsers = 3
        self.upload()
        self.upload()
        self.assertEqual(len(self._active_documents()), 1)
        other = 'user:2'
        # Every reserve supersedes the previous in-flight upload, so the first
        # two are superseded and the third is the only live parser. The fourth
        # reserve then trips the superseded-upload cap (owner_max_documents).
        first, _, _ = self._reserved_upload(other)
        second, _, _ = self._reserved_upload(other)
        third, _, _ = self._reserved_upload(other)
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.reserve(other, 'upload')
        self.assertEqual(error.exception.code, 'projection_owner_capacity')
        self.ledger.release(first)
        self.ledger.release(second)
        self.ledger.release(third)


    # ------------------------------ C2 ------------------------------
    def test_c2_mode_switch_aborts_running_analysis_child(self):
        from app.projection_runtime import run
        doc = self.create()
        environment, redis = self.queue_environment()
        with environment:
            first = self.analyze(doc).get_json()['analysis_id']
            meta = self.ledger.load(doc, self.owner)
            analysis = meta['analysis']
            self.assertEqual(analysis['mode'], 'current')
            lease = analysis['lease']
            generation = meta['_generation']
            # A tile render already in flight on the same document must survive
            # the analysis mode switch: its generation is never bumped.
            tile_lease = self.ledger.reserve(self.owner, 'tile', doc, generation)
            mock, processes = self.hanging_process()
            errors = []

            def analyze():
                try:
                    run(self.ledger, lease, 'analysis', self.ledger.source_path(doc), deadline=10)
                except ProjectionRejected as error:
                    errors.append(error.code)
                finally:
                    self.ledger.release(lease)

            with mock:
                thread = threading.Thread(target=analyze)
                thread.start()
                start = time.monotonic()
                while not processes and time.monotonic() - start < 1:
                    time.sleep(.01)
                self.assertTrue(processes)
                self.assertTrue(self.ledger.valid_lease(lease))
                second = self.analyze(doc, 'original').get_json()['analysis_id']
                self.assertNotEqual(first, second)
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ['projection_result_stale'])
            self.assertIsNone(self.ledger.lease(lease))
            self.assertFalse((self.ledger.root / 'operations' / lease).exists())
            self.assertTrue(self.ledger.valid_lease(tile_lease))
            self.assertEqual(self.ledger.load(doc, self.owner)['_generation'], generation)
            self.ledger.release(tile_lease)


    # ------------------------------ C3 ------------------------------
    @staticmethod
    def _c3_dark_pdf(pages=4, width=100, height=150):
        doc = fitz.open()
        for _ in range(pages):
            page = doc.new_page(width=width, height=height)
            page.draw_rect(page.rect, color=None, fill=(0.1, 0.1, 0.1))
        source = doc.tobytes()
        doc.close()
        return source

    @staticmethod
    def _c3_grid(columns, rows, page_w_mm, page_h_mm):
        placements = []
        for row in range(rows):
            for column in range(columns):
                placements.append({'page_index': row * columns + column,
                                   'x_mm': column * page_w_mm, 'y_mm': row * page_h_mm,
                                   'width_mm': page_w_mm, 'height_mm': page_h_mm})
        return {'placements': placements,
                'crop_margins_mm': {side: 0 for side in ('left', 'right', 'top', 'bottom')}}

    def test_c3_fractional_seams_keep_dark_pixels_on_both_sides(self):
        from app.projection import render_tile
        mm_pt = 72 / 25.4
        page_w_mm, page_h_mm = 100 / mm_pt, 150 / mm_pt
        source = self._c3_dark_pdf(4)
        layout = self._c3_grid(2, 2, page_w_mm, page_h_mm)
        # Odd pixel extents place both seams on a half pixel, so the adjacent
        # placements share one pixel column / row exactly where _overlap_cells
        # builds its composite strip.
        width_px, height_px = 201, 299
        values = {'x_mm': 0, 'y_mm': 0, 'width_mm': 2 * page_w_mm, 'height_mm': 2 * page_h_mm,
                  'width_px': width_px, 'height_px': height_px}
        pix = fitz.Pixmap(render_tile(source, values, 0, layout))
        self.assertEqual((pix.width, pix.height), (width_px, height_px))
        self.assertEqual(len(pix.samples), width_px * height_px * 3)
        for x in (width_px // 2 - 1, width_px // 2, width_px // 2 + 1):
            for y in range(0, height_px, 7):
                self.assertLess(max(pix.pixel(x, y)), 200, f'white seam column at {x},{y}')
        for y in (height_px // 2 - 1, height_px // 2, height_px // 2 + 1):
            for x in range(0, width_px, 7):
                self.assertLess(max(pix.pixel(x, y)), 200, f'white seam row at {x},{y}')

    def test_c3_region_helpers_clip_out_of_bounds_bounds(self):
        from app.projection import _overlap_cells, _read_region, _write_region, _source_over_block
        width, height = 4, 3
        canvas = bytearray(b'\xff') * (width * height * 3)
        drawn = [(x, y, x + 1, y + 1) for y in range(height) for x in range(width)]
        cells = _overlap_cells((-1, -1, width + 1, height + 1), drawn, width, height)
        self.assertTrue(cells)
        for left, top, right, bottom in cells:
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, width)
            self.assertLessEqual(bottom, height)
            self.assertLess(left, right)
            self.assertLess(top, bottom)
        outside = _overlap_cells((width + 1, 0, width + 5, height), [(0, 0, width + 2, height)],
                                 width, height)
        self.assertEqual(outside, [])
        rows = _read_region(canvas, width, (-1, -1, width + 1, height + 1))
        self.assertEqual(len(rows), height)
        self.assertTrue(all(len(row) == width * 3 for row in rows))
        self.assertEqual(_read_region(canvas, width, (width + 1, 0, width + 5, height)), [])
        before = bytes(canvas)
        _write_region(canvas, width, (-1, -1, width + 1, height + 1), rows)
        self.assertEqual(len(canvas), width * height * 3)
        self.assertEqual(bytes(canvas), before)
        doc = fitz.open()
        page = doc.new_page(width=10, height=10)
        page.draw_rect(page.rect, color=None, fill=(0, 0, 0))
        pixmap = page.get_pixmap(colorspace=fitz.csRGB, alpha=True)
        doc.close()
        guarded = bytearray(b'\xff') * (width * height * 3)
        _source_over_block(guarded, width, pixmap, (-2, -1, width + 2, height + 1))
        self.assertEqual(len(guarded), width * height * 3)

    def test_c3_viewport_clipping_bottom_right_page_renders_seam(self):
        from app.projection import render_tile
        mm_pt = 72 / 25.4
        page_w_mm, page_h_mm = 100 / mm_pt, 150 / mm_pt
        source = self._c3_dark_pdf(4)
        layout = self._c3_grid(2, 2, page_w_mm, page_h_mm)
        # A tile over the lower half of the montage: the bottom pages are clipped
        # at the tile's top edge, the fractional vertical seam reaches the tile's
        # bottom edge, and the page pixmaps round one row past that edge.
        width_px, height_px = 201, 75
        values = {'x_mm': 0, 'y_mm': 1.5 * page_h_mm, 'width_mm': 2 * page_w_mm,
                  'height_mm': 0.5 * page_h_mm, 'width_px': width_px, 'height_px': height_px}
        pix = fitz.Pixmap(render_tile(source, values, 0, layout))
        self.assertEqual((pix.width, pix.height), (width_px, height_px))
        self.assertEqual(len(pix.samples), width_px * height_px * 3)
        vertical = round(width_px * page_w_mm / values['width_mm'])
        for x in (vertical - 1, vertical, vertical + 1):
            for y in range(0, height_px, 3):
                self.assertLess(max(pix.pixel(x, y)), 200, f'white seam column at {x},{y}')
        for x in range(0, width_px, 11):
            for y in range(0, height_px, 3):
                self.assertLess(max(pix.pixel(x, y)), 200, f'white pixel at {x},{y}')


    # ------------------------------ C4 ------------------------------
    def test_c4_reconcile_removes_orphan_lease_directories(self):
        doc = self.create()
        lease = self.ledger.reserve(self.owner, 'tile', doc, 0)
        kept = self.ledger.lease_path(lease)
        kept.mkdir(mode=0o700, exist_ok=True)
        # reserve->claim races leave a named directory with no (or a lost) lease
        # row; only 64-hex names without a live lease may be reclaimed.
        operations_orphan = self.ledger.root / 'operations' / ('a' * 64)
        staging_orphan = self.ledger.root / 'staging' / ('b' * 64)
        operations_orphan.mkdir(mode=0o700)
        staging_orphan.mkdir(mode=0o700)
        foreign = self.ledger.root / 'operations' / 'not-a-lease-name'
        foreign.mkdir(mode=0o700)
        self.ledger.reconcile()
        self.assertFalse(operations_orphan.exists())
        self.assertFalse(staging_orphan.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(kept.exists())
        self.assertTrue(self.ledger.valid_lease(lease))
        self.ledger.release(lease)

    def test_c4_child_unicode_decode_error_is_not_public(self):
        from pathlib import Path
        from app import projection_child
        from app.projection_runtime import _public_child_error
        folder = Path(self.temp.name)
        source = folder / 'decoded.pdf'
        source.write_bytes(b'%PDF-1.4')
        spec = {'operation': 'parse', 'source': str(source), 'output': str(folder / 'result.json'),
                'parameters': {}}
        with patch.object(projection_child, 'inspect',
                          side_effect=UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')):
            projection_child.execute(spec)
        payload = json.loads((folder / 'error.json').read_text())
        self.assertFalse(payload['public'])
        message = _public_child_error('parse', payload)
        self.assertEqual(message, 'PDF 无法解析，请重试或更换文件')
        self.assertNotIn('invalid start byte', message)


    # ------------------------------ D1 ------------------------------
    def test_d1_new_upload_supersedes_inflight_parse_and_reclaims_slot(self):
        self.policy_restore()
        lease, staging, source = self._reserved_upload()
        self.ledger.policy.global_parsers = 1
        self.assertTrue(self.ledger.valid_lease(lease))
        # B reserves while A is still parsing: A is marked superseded at once,
        # so its parse child would be terminated on the next valid_lease poll.
        second = self.ledger.reserve(self.owner, 'upload')
        self.assertFalse(self.ledger.valid_lease(lease))
        # A's slot is only yielded when the route's finally releases its lease.
        # E1: begin_parse now waits up to 1.5s while the superseded upload-parse
        # still holds the slot, then reports parser_busy because A is never
        # released in this test.
        with self.assertRaises(ProjectionRejected) as busy:
            self.ledger.begin_parse(second)
        self.assertEqual(busy.exception.code, 'projection_parser_busy')
        # A finishing later must never become the online document.
        meta = {'owner': self.owner, 'expires_at': time.time() + 3600, 'filename': 'a.pdf'}
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.complete_upload(lease, self.owner, source, meta, [])
        self.assertEqual(error.exception.code, 'projection_upload_superseded')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self._active_documents(), [])
        self.ledger.release(lease)
        # With A released the single parser slot is free for B.
        self.ledger.begin_parse(second)
        self.assertTrue(self.ledger.valid_lease(second))
        self.ledger.release(second)

    def test_d1_rejected_newer_upload_still_supersedes_earlier(self):
        self.policy_restore()
        self.ledger.policy.global_parsers = 2
        lease, staging, source = self._reserved_upload()
        # B was reserved (and thus superseded A) but the client failed/was
        # rejected, so B is released without ever completing.
        second = self.ledger.reserve(self.owner, 'upload')
        self.ledger.release(second)
        meta = {'owner': self.owner, 'expires_at': time.time() + 3600, 'filename': 'a.pdf'}
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.complete_upload(lease, self.owner, source, meta, [])
        self.assertEqual(error.exception.code, 'projection_upload_superseded')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self._active_documents(), [])
        self.ledger.release(lease)

    def test_d1_earlier_upload_still_superseded_after_newer_closed(self):
        self.policy_restore()
        self.ledger.policy.global_parsers = 2
        lease, staging, source = self._reserved_upload()
        document = self.upload().get_json()['document_id']
        self.assertEqual(self.client.delete(f'{BASE}/{document}',
                                            headers={'Authorization': 'x'}).status_code, 204)
        # Closing the newer document leaves A superseded: it may not resurrect.
        meta = {'owner': self.owner, 'expires_at': time.time() + 3600, 'filename': 'a.pdf'}
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.complete_upload(lease, self.owner, source, meta, [])
        self.assertEqual(error.exception.code, 'projection_upload_superseded')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self._active_documents(), [])
        self.ledger.release(lease)

    def test_d1_owner_superseded_uploads_are_capped(self):
        self.policy_restore()
        self.ledger.policy.owner_max_documents = 2
        self.ledger.policy.global_parsers = 3
        first = self.ledger.reserve(self.owner, 'upload')
        second = self.ledger.reserve(self.owner, 'upload')
        third = self.ledger.reserve(self.owner, 'upload')
        self.assertFalse(self.ledger.valid_lease(first))
        self.assertFalse(self.ledger.valid_lease(second))
        self.assertTrue(self.ledger.valid_lease(third))
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.reserve(self.owner, 'upload')
        self.assertEqual(error.exception.code, 'projection_owner_capacity')
        for lease in (first, second, third):
            self.ledger.release(lease)

    def test_d1_begin_parse_on_superseded_lease_is_409(self):
        self.policy_restore()
        self.ledger.policy.global_parsers = 2
        first = self.ledger.reserve(self.owner, 'upload')
        second = self.ledger.reserve(self.owner, 'upload')
        with self.assertRaises(ProjectionRejected) as error:
            self.ledger.begin_parse(first)
        self.assertEqual(error.exception.code, 'projection_upload_superseded')
        self.assertEqual(error.exception.status, 409)
        self.ledger.begin_parse(second)
        self.ledger.release(first)
        self.ledger.release(second)

    def test_d1_route_reports_superseded_upload_when_newer_wins(self):
        self.policy_restore()
        self.ledger.policy.global_parsers = 2
        newer = []

        def supersede_and_go_stale(*args, **kwargs):
            newer.append(self.ledger.reserve(self.owner, 'upload'))
            raise ProjectionRejected('projection_result_stale', 'stale', 409, 0)

        with patch('app.projection_runtime.run', side_effect=supersede_and_go_stale):
            response = self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error_code'], 'projection_upload_superseded')
        for lease in newer:
            self.ledger.release(lease)


    # ------------------------------ E1 ------------------------------
    def test_e1_begin_parse_waits_for_superseded_upload_to_release_slot(self):
        self.policy_restore()
        lease, staging, source = self._reserved_upload()
        self.ledger.policy.global_parsers = 1
        # B reserves while A is still parsing, so A is marked superseded and its
        # child would stop within one poll plus the 0.25s grace.
        second = self.ledger.reserve(self.owner, 'upload')
        self.assertFalse(self.ledger.valid_lease(lease))
        timer = threading.Timer(0.2, self.ledger.release, args=(lease,))
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        self.ledger.begin_parse(second)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.ledger.lease(second)['kind'], 'upload-parse')
        self.ledger.release(second)

    def test_e1_live_foreign_parse_rejects_begin_parse_immediately(self):
        self.policy_restore()
        self.ledger.policy.global_parsers = 1
        holder = self.ledger.reserve('user:2', 'upload')
        self.ledger.begin_parse(holder)
        mine = self.ledger.reserve(self.owner, 'upload')
        start = time.monotonic()
        with self.assertRaises(ProjectionRejected) as busy:
            self.ledger.begin_parse(mine)
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertEqual(busy.exception.code, 'projection_parser_busy')
        self.ledger.release(holder)
        self.ledger.release(mine)

    def test_e1_superseded_upload_that_never_releases_times_out_busy(self):
        self.policy_restore()
        lease, staging, source = self._reserved_upload()
        self.ledger.policy.global_parsers = 1
        second = self.ledger.reserve(self.owner, 'upload')
        start = time.monotonic()
        with self.assertRaises(ProjectionRejected) as busy:
            self.ledger.begin_parse(second)
        elapsed = time.monotonic() - start
        self.assertEqual(busy.exception.code, 'projection_parser_busy')
        self.assertGreaterEqual(elapsed, 1.4)
        self.assertLess(elapsed, 2.5)
        self.ledger.release(lease)
        self.ledger.release(second)


    # ------------------------------ D2 ------------------------------
    def _analyzing_document_with_lease(self):
        doc = self.create()
        meta = self.ledger.load(doc, self.owner)
        generation = meta['_generation']
        lease = self.ledger.reserve(self.owner, 'analysis', doc, generation)
        meta['analysis'] = {'analysis_id': 'controlled-d2', 'status': 'analyzing',
                            'phase': 'queued', 'mode': 'current', 'lease': lease}
        self.assertTrue(self.ledger.update(doc, self.owner, meta, generation))
        return doc, lease

    def test_d2_manual_layout_discards_analyzing_job_and_releases_lease(self):
        doc, lease = self._analyzing_document_with_lease()
        self.assertIsNotNone(self.ledger.lease(lease))
        discarded = []
        with patch('app.projection_layout.discard_analysis_job',
                   side_effect=lambda job_id: discarded.append(job_id)):
            response = self.client.post(f'{BASE}/{doc}/layout', headers={'Authorization': 'x'},
                                        data={'method': 'manual', 'rows': 1, 'columns': 2, 'order': 'row'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(discarded, ['controlled-d2'])
        self.assertIsNone(self.ledger.lease(lease))
        self.assertNotIn('analysis', self.metadata(doc))

    def test_d2_failed_update_keeps_analysis_job_and_lease(self):
        doc, lease = self._analyzing_document_with_lease()
        discarded = []
        with patch.object(self.ledger, 'update', return_value=False), \
             patch('app.projection_layout.discard_analysis_job',
                   side_effect=lambda job_id: discarded.append(job_id)):
            response = self.client.post(f'{BASE}/{doc}/layout', headers={'Authorization': 'x'},
                                        data={'method': 'manual', 'rows': 1, 'columns': 2, 'order': 'row'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(discarded, [])
        self.assertIsNotNone(self.ledger.lease(lease))


if __name__ == '__main__':
    unittest.main()
