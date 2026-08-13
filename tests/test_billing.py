import unittest
from unittest.mock import patch

from app.billing import BillingRejected, authorize_conversion, commit_conversion, identify_user


class BillingTest(unittest.TestCase):
    def test_job_authorization_requires_login(self):
        with self.assertRaises(BillingRejected) as raised:
            authorize_conversion('', 'request-1', 'pdf_to_plt')

        self.assertEqual(raised.exception.status_code, 401)

    @patch('app.billing._json_request')
    def test_charge_required_is_forwarded(self, request):
        request.return_value = (402, {
            'message': '需要付费',
            'data': {
                'charge_required': True,
                'required_points': 50,
                'current_balance': 100,
            },
        })

        with self.assertRaises(BillingRejected) as raised:
            authorize_conversion('Bearer token', 'request-2', 'plt_to_pdf')

        self.assertEqual(raised.exception.status_code, 402)
        self.assertTrue(raised.exception.data['charge_required'])

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

    @patch.dict('os.environ', {'CONVERSION_SERVICE_TOKEN': 'service-token'})
    @patch('app.billing._json_request')
    def test_commit_rejection_forwards_balance_details(self, request):
        request.return_value = (400, {
            'message': '布豆余额不足',
            'data': {'required_points': 50, 'current_balance': 20},
        })

        with self.assertRaises(BillingRejected) as raised:
            commit_conversion(42, 'request-4', 'job-4')

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.data['required_points'], 50)
        self.assertEqual(raised.exception.data['current_balance'], 20)


if __name__ == '__main__':
    unittest.main()
