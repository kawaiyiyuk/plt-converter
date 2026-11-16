import unittest
from unittest.mock import Mock, patch

from app.billing import BillingRejected, authorize_conversion, commit_conversion, identify_user, release_conversion
from app.routes import rollback_conversion_submission
from app.tasks import (
    commit_successful_conversion_billing,
    release_failed_conversion_billing,
    settle_release_with_conflict_guard,
)


class BillingTest(unittest.TestCase):
    @patch('app.routes.release_conversion')
    @patch('app.routes.cancel_job')
    def test_submit_rollback_does_not_release_ready_result(self, cancel, release):
        for status in ('finalizing', 'done'):
            with self.subTest(status=status):
                cancel.return_value = {'status': status}
                rollback_conversion_submission(
                    {'user_id': 42, 'request_id': 'ad-request'},
                    {'job_id': 'job-1'},
                )
                release.assert_not_called()
        self.assertEqual(cancel.call_count, 2)

        cancel.return_value = {'status': 'cancelled'}
        rollback_conversion_submission(
            {'user_id': 42, 'request_id': 'ad-request'},
            {'job_id': 'job-1'},
        )
        release.assert_called_once_with(42, 'ad-request', 'job-1')

    @patch('app.tasks.release_conversion', return_value=True)
    def test_failed_ad_job_releases_credit_but_old_regular_job_does_not(self, release):
        record = {
            'job_type': 'plt_to_pdf',
            'billing_access_method': 'ad',
            'user_key': 'user:42',
            'billing_request_id': 'ad-request',
            'job_id': 'job-1',
        }
        self.assertTrue(release_failed_conversion_billing(record))
        release.assert_called_once_with(42, 'ad-request', 'job-1')
        release.reset_mock()
        record['billing_access_method'] = 'points'
        self.assertFalse(release_failed_conversion_billing(record))
        release.assert_not_called()

    @patch('app.tasks.commit_conversion', return_value={})
    def test_successful_ad_job_finalizes_credit(self, commit):
        record = {
            'job_type': 'pdf_to_plt',
            'billing_access_method': 'ad',
            'user_key': 'user:42',
            'billing_request_id': 'ad-request',
            'job_id': 'job-1',
        }
        self.assertTrue(commit_successful_conversion_billing(record))
        commit.assert_called_once_with(42, 'ad-request', 'job-1', completed=True)

    def test_job_authorization_requires_login(self):
        with self.assertRaises(BillingRejected) as raised:
            authorize_conversion('', 'request-1', 'pdf_to_plt')

        self.assertEqual(raised.exception.status_code, 401)

    @patch('app.billing._json_request')
    def test_ad_required_is_forwarded(self, request):
        request.return_value = (402, {
            'message': '需要观看广告',
            'data': {'ad_required': True},
        })

        with self.assertRaises(BillingRejected) as raised:
            authorize_conversion('Bearer token', 'request-2', 'plt_to_pdf')

        self.assertEqual(raised.exception.status_code, 402)
        self.assertTrue(raised.exception.data['ad_required'])

    @patch('app.billing._json_request')
    def test_identity_uses_verified_backend_user(self, request):
        request.return_value = (200, {'data': {'user_id': 42}})

        self.assertEqual(identify_user('Bearer token'), 'user:42')

    @patch.dict('os.environ', {'CONVERSION_SERVICE_TOKEN': 'service-token'})
    @patch('app.billing._json_request')
    def test_commit_uses_service_token(self, request):
        request.return_value = (200, {'data': {'success': True}})

        self.assertTrue(commit_conversion(42, 'request-3', 'job-3')['success'])
        self.assertEqual(
            request.call_args.args[2]['X-Conversion-Service-Token'],
            'service-token',
        )
        self.assertFalse(request.call_args.args[1]['completed'])

    @patch.dict('os.environ', {'CONVERSION_SERVICE_TOKEN': 'service-token'})
    @patch('app.billing._json_request')
    def test_commit_rejection_forwards_ad_details(self, request):
        request.return_value = (409, {
            'message': '广告资格已失效',
            'data': {'ad_required': True},
        })

        with self.assertRaises(BillingRejected) as raised:
            commit_conversion(42, 'request-4', 'job-4', completed=True)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(raised.exception.data['ad_required'])
        self.assertTrue(request.call_args.args[1]['completed'])

    @patch.dict('os.environ', {'CONVERSION_SERVICE_TOKEN': 'service-token'})
    @patch('app.billing._json_request')
    def test_release_checks_whether_backend_actually_released_usage(self, request):
        request.return_value = (200, {'data': {'released': False, 'points_refunded': 0}})
        self.assertFalse(release_conversion(42, 'completed-free', 'job-1'))

        request.return_value = (200, {'data': {'released': True, 'points_refunded': 0}})
        self.assertTrue(release_conversion(42, 'failed-free', 'job-2'))

        request.return_value = (200, {'data': {'released': False, 'idempotent': True}})
        self.assertTrue(release_conversion(42, 'already-released', 'job-3'))

        self.assertTrue(release_conversion(
            42, 'lost-output', 'job-4', rollback_completed=True,
        ))
        self.assertTrue(request.call_args.args[1]['rollback_completed'])

    @patch.dict('os.environ', {'CONVERSION_SERVICE_TOKEN': 'service-token'})
    @patch('app.billing._json_request')
    def test_release_conflict_is_durable_but_other_failures_are_retryable(self, request):
        request.return_value = (409, {
            'message': '转换任务编号与计费记录不匹配',
            'data': {'request_id': 'another-request'},
        })

        with self.assertRaises(BillingRejected) as raised:
            release_conversion(42, 'conflicting-request', 'job-5')

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(str(raised.exception), '转换任务编号与计费记录不匹配')
        self.assertEqual(raised.exception.data['request_id'], 'another-request')

        request.return_value = (500, {'message': '计费服务内部错误'})
        self.assertFalse(release_conversion(42, 'retryable-request', 'job-6'))

        request.return_value = (401, {'message': '服务令牌失效'})
        self.assertFalse(release_conversion(42, 'unauthorized-request', 'job-7'))

        request.side_effect = BillingRejected('转换计费服务暂时不可用，请稍后重试', 503)
        self.assertFalse(release_conversion(42, 'unreachable-request', 'job-8'))

    @patch('app.routes.release_conversion', side_effect=BillingRejected('编号不匹配', 409))
    @patch('app.routes.cancel_job', return_value={'status': 'cancelled'})
    def test_submit_rollback_release_conflict_does_not_mask_original_error(self, cancel, release):
        rollback_conversion_submission(
            {'user_id': 42, 'request_id': 'conflict-request'},
            {'job_id': 'job-1'},
        )

        cancel.assert_called_once_with('job-1', 'user:42')
        release.assert_called_once_with(42, 'conflict-request', 'job-1')

    @patch('app.tasks.update_job')
    def test_release_guard_rejects_invalid_identity_without_calling_backend(self, update):
        connection = object()
        release = Mock(return_value=True)
        updated = {'job_id': 'job-1', 'billing_released': False}
        update.return_value = updated
        cases = (
            ('client-42', 'request-1', 'job-1'),
            ('', 'request-1', 'job-1'),
            ('user:42', '', 'job-1'),
            ('user:42', None, 'job-1'),
            ('user:42', 'request-1', None),
            ('user:', 'request-1', 'job-1'),
            ('user:not-a-number', 'request-1', 'job-1'),
        )
        for user_key, request_id, job_id in cases:
            with self.subTest(user_key=user_key, request_id=request_id, job_id=job_id):
                update.reset_mock()
                record = {
                    'user_key': user_key,
                    'billing_request_id': request_id,
                    'job_id': job_id,
                }
                settled, released = settle_release_with_conflict_guard(
                    record, connection, release,
                )
                self.assertIs(released, False)
                release.assert_not_called()
                if job_id:
                    update.assert_called_once_with(
                        job_id, connection, billing_released=False,
                    )
                    self.assertIs(settled, updated)
                else:
                    update.assert_not_called()
                    self.assertIs(settled, record)
        release.assert_not_called()


if __name__ == '__main__':
    unittest.main()
