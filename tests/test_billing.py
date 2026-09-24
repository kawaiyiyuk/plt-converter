import unittest
from unittest.mock import patch

from app.billing import BillingRejected, authorize_conversion, commit_conversion, identify_user, release_conversion
from app.tasks import commit_successful_conversion_billing, release_failed_conversion_billing


class BillingTest(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
