import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pymupdf

from app import create_app
from app.billing import BillingRejected
from app.job_queue import save_job, update_job


class MemoryLock:
    def acquire(self, blocking=True):
        return True

    def release(self):
        pass


class FakeQueue:
    name = 'conversions'

    def __init__(self):
        self.job_ids = []
        self.count = 0

    def enqueue_call(self, *args, **kwargs):
        self.job_ids.append(kwargs.get('job_id'))
        self.count = len(self.job_ids)

    def remove(self, job_id):
        if job_id in self.job_ids:
            self.job_ids.remove(job_id)
        self.count = len(self.job_ids)


def sample_pdf():
    document = pymupdf.open()
    page = document.new_page(width=300, height=300)
    page.draw_line((20, 40), (280, 40))
    data = document.tobytes()
    document.close()
    return data


class PatternPdfRoutesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.redis = fakeredis.FakeRedis()
        self.redis.lock = lambda *args, **kwargs: MemoryLock()
        self.queue = FakeQueue()
        self.enterContext(patch(
            'app.job_queue.conversion_queue', return_value=self.queue
        ))
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
        self.enterContext(patch(
            'app.routes.identify_user', side_effect=self.identify_user
        ))
        self.enterContext(patch(
            'app.routes.identify_layout_user', side_effect=self.identify_layout_user
        ))
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.pdf = sample_pdf()

    @staticmethod
    def identify_user(authorization):
        if authorization in {'Bearer good', 'Bearer second'}:
            return 'user:42' if authorization == 'Bearer good' else 'user:43'
        raise BillingRejected('登录状态验证失败', 401)

    @staticmethod
    def identify_layout_user(token):
        if token == 'layout-good':
            return 'user:42'
        if token == 'layout-second':
            return 'user:43'
        raise BillingRejected('登录状态验证失败', 401)

    def authorize(self, allow_charge=False):
        return {
            'user_id': 42,
            'request_id': 'request-1',
            'access_method': 'points' if allow_charge else 'free',
        }

    def post(self, headers=None, filename='attachment.pdf'):
        return self.client.post(
            '/api/v1/pdf/attachment/jobs',
            data={
                'file': (io.BytesIO(self.pdf), 'pattern.pdf'),
                'original_filename': filename,
            },
            headers={
                'X-Conversion-Request-ID': 'request-1',
                **(headers or {}),
            },
        )

    def save_record(self, user_key='user:42', status='queued'):
        job_id = 'job-attachment'
        record = {
            'job_id': job_id,
            'job_type': 'pattern_pdf_to_plt',
            'user_key': user_key,
            'status': status,
            'progress': 0,
            'filename': 'attachment.pdf',
            'options': {},
            'input_path': str(Path(self.directory.name) / 'input.pdf'),
            'result_path': None,
            'result': None,
            'created_at': 1,
            'updated_at': 1,
            'started_at': None,
            'finished_at': None,
            'rq_job_id': f'{job_id}:0',
        }
        save_job(record, self.redis)
        return record

    def test_bearer_and_layout_token_can_submit_query_and_download(self):
        for headers in (
            {'Authorization': 'Bearer good'},
            {'X-Layout-Token': 'layout-good'},
        ):
            with self.subTest(headers=headers), patch(
                'app.routes.authorize_pattern_pdf', return_value=self.authorize()
            ) as authorize:
                response = self.post(headers)
                self.assertEqual(response.status_code, 200, response.get_json())
                payload = response.get_json()
                self.assertEqual(payload['job_type'], 'pattern_pdf_to_plt')
                self.assertEqual(authorize.call_args.args[2], 'request-1')

            job_id = payload['job_id']
            query = self.client.get(
                f'/api/v1/pdf/attachment/jobs/{job_id}', headers=headers
            )
            self.assertEqual(query.status_code, 200, query.get_json())
            self.assertEqual(query.get_json()['job_id'], job_id)

            result_path = Path(self.directory.name) / 'attachment.plt'
            result_path.write_bytes(b'IN;PU0,0;PD10,10;')
            update_job(
                job_id, self.redis,
                status='done', progress=100,
                result_path=str(result_path),
                result={'filename': '裁片.plt', 'layout': {}, 'review_required': True},
            )
            download = self.client.get(
                f'/api/v1/pdf/attachment/files/{job_id}.plt', headers=headers
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.data, b'IN;PU0,0;PD10,10;')
            self.assertIn('attachment', download.headers['Content-Disposition'])
            download.close()

    def test_done_response_exposes_attachment_plt_path(self):
        record = self.save_record(status='done')
        update_job(
            record['job_id'], self.redis,
            result_path=str(Path(self.directory.name) / 'attachment.plt'),
            result={
                'filename': 'attachment.plt',
                'layout': {'rows': 1, 'columns': 1},
                'layout_source': 'single_page',
                'layout_confidence': 'high',
                'skipped_page_indexes': [],
                'seam_repair': {'crop_delta_mm': 0, 'frames_removed': 0,
                                'welded_joints': 0},
                'review_required': True,
            },
        )
        response = self.client.get(
            f"/api/v1/pdf/attachment/jobs/{record['job_id']}",
            headers={'Authorization': 'Bearer good'},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(
            payload['plt_path'],
            f"/api/v1/pdf/attachment/files/{record['job_id']}.plt",
        )
        self.assertEqual(payload['layout_source'], 'single_page')
        self.assertEqual(payload['seam_repair']['welded_joints'], 0)
        self.assertTrue(payload['review_required'])

    def test_other_users_job_is_hidden(self):
        record = self.save_record(user_key='user:43')
        for headers in (
            {'Authorization': 'Bearer good'},
            {'X-Layout-Token': 'layout-good'},
        ):
            with self.subTest(headers=headers):
                response = self.client.get(
                    f"/api/v1/pdf/attachment/jobs/{record['job_id']}",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 404)
                response = self.client.delete(
                    f"/api/v1/pdf/attachment/jobs/{record['job_id']}",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 404)
                response = self.client.get(
                    f"/api/v1/pdf/attachment/files/{record['job_id']}.plt",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 404)

    def test_invalid_bearer_does_not_fall_back_to_layout_token(self):
        with patch(
            'app.routes.identify_user',
            side_effect=BillingRejected('登录状态验证失败', 401),
        ), patch('app.routes.identify_layout_user') as layout_identity:
            response = self.client.get(
                '/api/v1/pdf/attachment/jobs/missing',
                headers={
                    'Authorization': 'Bearer bad',
                    'X-Layout-Token': 'layout-good',
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['status'], 'billing_rejected')
        layout_identity.assert_not_called()

    def test_billing_rejection_is_forwarded_and_nothing_is_queued(self):
        rejection = BillingRejected('需要确认扣费', 402, {
            'points_required': 50,
            'current_balance': 20,
            'free_remaining': 0,
            'insufficient_points': False,
        })
        with patch(
            'app.routes.authorize_pattern_pdf', side_effect=rejection
        ):
            response = self.post({'Authorization': 'Bearer good'})

        self.assertEqual(response.status_code, 402)
        payload = response.get_json()
        self.assertEqual(payload['status'], 'billing_rejected')
        self.assertEqual(payload['points_required'], 50)
        self.assertEqual(payload['current_balance'], 20)
        self.assertEqual(payload['free_remaining'], 0)
        self.assertIs(payload['insufficient_points'], False)
        self.assertEqual(self.redis.llen('rq:queue:conversions'), 0)

    def test_allow_charge_header_is_passed_to_authorization(self):
        with patch(
            'app.routes.authorize_pattern_pdf', return_value=self.authorize(True)
        ) as authorize:
            response = self.post({
                'X-Layout-Token': 'layout-good',
                'X-Conversion-Allow-Charge': 'true',
            })

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            authorize.call_args.args,
            (None, 'layout-good', 'request-1', True),
        )
        self.assertEqual(
            response.get_json()['job_type'], 'pattern_pdf_to_plt'
        )

    def test_cancel_uses_the_same_ownership_rules(self):
        record = self.save_record()
        response = self.client.delete(
            f"/api/v1/pdf/attachment/jobs/{record['job_id']}",
            headers={'X-Layout-Token': 'layout-good'},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
