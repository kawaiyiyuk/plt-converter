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

    @property
    def count(self):
        return len(self.job_ids)

    def enqueue_call(self, *args, **kwargs):
        self.job_ids.append(kwargs.get('job_id'))


def sample_pdf():
    document = pymupdf.open()
    page = document.new_page(width=300, height=300)
    page.draw_line((20, 40), (280, 40))
    data = document.tobytes()
    document.close()
    return data


class PatternPdfSubmissionDisabledTest(unittest.TestCase):
    """产品决定：纸样附件 PDF 自动转换入口暂时关闭，旧客户端必须收到 410。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.redis = fakeredis.FakeRedis()
        self.redis.lock = lambda *args, **kwargs: MemoryLock()
        self.queue = FakeQueue()
        self.enterContext(patch('app.job_queue.conversion_queue', return_value=self.queue))
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
        if authorization == 'Bearer good':
            return 'user:42'
        raise BillingRejected('登录状态验证失败', 401)

    @staticmethod
    def identify_layout_user(token):
        if token == 'layout-good':
            return 'user:42'
        raise BillingRejected('登录状态验证失败', 401)

    def post(self, headers=None):
        return self.client.post(
            '/api/v1/pdf/attachment/jobs',
            data={
                'file': (io.BytesIO(self.pdf), 'pattern.pdf'),
                'original_filename': 'attachment.pdf',
            },
            headers={
                'X-Conversion-Request-ID': 'request-1',
                **(headers or {}),
            },
        )

    def test_disabled_returns_410_before_auth_billing_or_file_processing(self):
        with (
            patch('app.routes.authorize_pattern_pdf') as authorize,
            patch('app.routes.pdf_page_count') as page_count,
            patch('app.job_queue.submit_job') as submit,
            patch('app.routes.release_conversion') as release,
            patch('app.tasks.commit_conversion') as commit,
        ):
            response = self.post({'Authorization': 'Bearer good'})

        self.assertEqual(response.status_code, 410, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['status'], 'disabled')
        self.assertIn('PLT', payload['error'])
        self.assertIn('暂停', payload['error'])
        authorize.assert_not_called()
        page_count.assert_not_called()
        submit.assert_not_called()
        release.assert_not_called()
        commit.assert_not_called()
        self.assertEqual(self.queue.job_ids, [])
        self.assertEqual(self.redis.llen('rq:queue:conversions'), 0)
        self.assertFalse(
            any(b'billing' in key for key in self.redis.keys('plt-converter:*')),
            '关闭入口时不得写入计费账本',
        )

    def test_disabled_even_with_charge_headers_and_layout_token(self):
        response = self.post({
            'X-Layout-Token': 'layout-good',
            'X-Conversion-Allow-Charge': 'true',
        })
        self.assertEqual(response.status_code, 410, response.get_json())
        self.assertEqual(response.get_json()['status'], 'disabled')
        self.assertEqual(self.queue.job_ids, [])

    def test_disabled_response_is_json_not_an_upload_error(self):
        response = self.post({'Authorization': 'Bearer bad'})
        self.assertEqual(response.status_code, 410, response.get_json())
        self.assertEqual(response.get_json()['status'], 'disabled')
        self.assertEqual(self.queue.job_ids, [])

    def save_record(self, status='queued', filename='attachment.pdf'):
        job_id = 'job-transitional'
        record = {
            'job_id': job_id,
            'job_type': 'pattern_pdf_to_plt',
            'user_key': 'user:42',
            'status': status,
            'progress': 40 if status == 'processing' else 0,
            'filename': filename,
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

    def test_transitional_job_can_still_be_queried_by_both_identities(self):
        record = self.save_record(status='processing')
        for headers in (
            {'Authorization': 'Bearer good'},
            {'X-Layout-Token': 'layout-good'},
        ):
            with self.subTest(headers=headers):
                response = self.client.get(
                    f"/api/v1/pdf/attachment/jobs/{record['job_id']}",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 200, response.get_json())
                payload = response.get_json()
                self.assertEqual(payload['job_id'], record['job_id'])
                self.assertEqual(payload['job_type'], 'pattern_pdf_to_plt')
                self.assertEqual(payload['status'], 'processing')

    def test_transitional_job_can_still_be_cancelled(self):
        record = self.save_record()
        response = self.client.delete(
            f"/api/v1/pdf/attachment/jobs/{record['job_id']}",
            headers={'X-Layout-Token': 'layout-good'},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['status'], 'cancelled')

    def test_transitional_finished_job_result_can_still_be_downloaded(self):
        record = self.save_record(status='done')
        result_path = Path(self.directory.name) / 'transitional.plt'
        result_path.write_bytes(b'IN;PU0,0;PD10,10;')
        update_job(
            record['job_id'],
            self.redis,
            status='done',
            progress=100,
            result_path=str(result_path),
            result={'filename': '过渡任务.plt'},
        )
        for headers in (
            {'Authorization': 'Bearer good'},
            {'X-Layout-Token': 'layout-good'},
        ):
            with self.subTest(headers=headers):
                download = self.client.get(
                    f"/api/v1/pdf/attachment/files/{record['job_id']}.plt",
                    headers=headers,
                )
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.data, b'IN;PU0,0;PD10,10;')
                download.close()

    def test_independent_pdf_to_plt_tool_is_not_disabled(self):
        # 独立工具仍走 /api/v1/pdf/jobs；这里让计费拒绝，证明请求已越过关闭分支。
        with patch(
            'app.routes.authorize_job',
            side_effect=BillingRejected('登录状态验证失败', 401),
        ) as authorize:
            response = self.client.post(
                '/api/v1/pdf/jobs',
                data={'file': (io.BytesIO(self.pdf), 'tool.pdf')},
            )
        self.assertEqual(response.status_code, 401, response.get_json())
        self.assertEqual(response.get_json()['status'], 'billing_rejected')
        authorize.assert_called_once()


if __name__ == '__main__':
    unittest.main()
