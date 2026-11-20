"""Business-server PDF preflight authenticates without calling identity HTTP."""
import hashlib
import hmac
import io
import time
from unittest import TestCase
from unittest.mock import patch

import test_projection as fixtures

PATH = '/api/v1/pdf/projection/validate'
HEADER = 'X-Pattern-PDF-Validation'
KEY = '6f' * 32


def proof(raw, *, issued_at=None, owner=17, key=KEY):
    digest = hashlib.sha256(raw).hexdigest()
    fields = f'v1:{owner}:{int(time.time()) if issued_at is None else issued_at}:{digest}'
    mac = hmac.new(bytes.fromhex(key), f'pattern-pdf-validation:{fields}'.encode(), hashlib.sha256).hexdigest()
    return f'{fields}:{mac}'


class PDFValidationProofTests(TestCase):
    setUp = fixtures.ProjectionTests.setUp
    identity = fixtures.ProjectionTests.identity

    def request_validation(self, raw, headers):
        return self.client.post(PATH, headers=headers, data={'file': (io.BytesIO(raw), 'proof.pdf')})

    def test_valid_proof_can_validate_when_backend_identity_is_unavailable(self):
        self.app.config['PROJECTOR_PDF_VALIDATION_KEY'] = KEY
        document = fixtures.pdf_bytes(2)
        with patch('app.projection.identify_user', side_effect=AssertionError('unexpected identity callback')):
            result = self.request_validation(document, {HEADER: proof(document)})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['page_count'], 2)


    def test_missing_forged_and_stale_proofs_are_rejected_before_reserving_disk(self):
        self.app.config['PROJECTOR_PDF_VALIDATION_KEY'] = KEY
        document = fixtures.pdf_bytes()
        now = int(time.time())
        options = [
            {'Authorization': 'Bearer mobile'},
            {HEADER: proof(document, key='aa' * 32)},
            {HEADER: proof(document, issued_at=now - 61)},
            {HEADER: proof(document, issued_at=now + 6)},
        ]
        with patch('app.projection.time.time', return_value=now), patch.object(self.ledger, 'reserve') as reserve:
            for headers in options:
                result = self.request_validation(document, headers)
                self.assertEqual(result.status_code, 403)
                self.assertEqual(result.json['error_code'], 'projection_pdf_validation_proof_invalid')
            reserve.assert_not_called()

    def test_proof_cannot_be_used_with_different_bytes_or_to_open_projection(self):
        self.app.config['PROJECTOR_PDF_VALIDATION_KEY'] = KEY
        document = fixtures.pdf_bytes()
        header = {HEADER: proof(document)}
        with patch('app.projection_runtime.run') as parse:
            result = self.request_validation(fixtures.pdf_bytes(2), header)
            self.assertEqual(result.status_code, 403, result.json)
            parse.assert_not_called()
        self.assertFalse(list((self.ledger.root / 'staging').iterdir()))
        with self.ledger.transaction() as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 0)
        result = self.client.post('/api/v1/pdf/projection/attachment-documents', headers=header,
                                  data={'file': (io.BytesIO(document), 'proof.pdf')})
        self.assertEqual(result.status_code, 401, result.json)

    def test_missing_server_key_disables_preflight_cleanly(self):
        document = fixtures.pdf_bytes()
        self.app.config['PROJECTOR_PDF_VALIDATION_KEY'] = ''
        result = self.request_validation(document, {HEADER: proof(document)})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json['error_code'], 'projection_pdf_validation_unconfigured')
