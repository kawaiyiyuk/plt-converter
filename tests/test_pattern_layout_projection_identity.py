"""Scoped web identity only, no network; use real account-owned projector ledger."""
from unittest.mock import patch
import unittest
import test_projection_identity as fixtures
BASE=fixtures.BASE


class LayoutProjectionIdentityTests(unittest.TestCase):
    setUp=fixtures.ProjectionIdentityTests.setUp
    def test_layout_token_calls_scoped_get_and_cannot_access_other_owner(self):
        with patch('app.projection.identity_request',return_value=(200,{'data':{'user_id':1}})) as identity:
            response=self.client.get(BASE+'/'+self.document,headers={'X-Layout-Token':'web-private'})
            self.assertEqual(response.status_code,200)
            identity.assert_called_once_with('/api/v1/pattern-layout/identity',{}, {'X-Layout-Token':'web-private'},method='GET')
        with patch('app.projection.identity_request',return_value=(200,{'data':{'user_id':2}})):
            self.assertEqual(self.client.get(BASE+'/'+self.document,headers={'X-Layout-Token':'other'}).status_code,404)

    def test_revocation_revalidates_and_upstream_404_is_not_session_expiry(self):
        with patch('app.projection.identity_request',side_effect=[(200,{'data':{'user_id':1}}),(401,{}),(404,{})]) as identity:
            headers={'X-Layout-Token':'web-private'}
            self.assertEqual(self.client.get(BASE+'/'+self.document,headers=headers).status_code,200)
            self.assertEqual(self.client.get(BASE+'/'+self.document,headers=headers).status_code,401)
            self.assertEqual(self.client.get(BASE+'/'+self.document,headers=headers).status_code,503)
            self.assertEqual(identity.call_count,3)
            self.assertIsNotNone(self.store.load(self.document,'user:1'))

    def test_invalid_bearer_never_falls_back_to_layout_token(self):
        with patch('app.projection.identity_request',return_value=(401,{})) as identity:
            response=self.client.get(BASE+'/'+self.document,headers={'Authorization':'Bearer bad','X-Layout-Token':'web-private'})
            self.assertEqual(response.status_code,401)
            self.assertEqual(identity.call_args.args[0],'/api/v1/points/conversion/identity')

    def test_new_owned_document_retires_layout_document(self):
        import time
        lease=self.store.reserve('user:1','upload'); folder=self.store.lease_path(lease);folder.mkdir()
        source=folder/'source.pdf';source.write_bytes(b'%PDF-new-ledger-only')
        new_id=self.store.complete_upload(lease,'user:1',source,{'owner':'user:1','filename':'new.pdf','expires_at':time.time()+3600,'pages':[{'width_mm':100,'height_mm':100}],'page_count':1},discarded=[])
        self.store.release(lease)
        with patch('app.projection.identity_request',return_value=(200,{'data':{'user_id':1}})):
            self.assertEqual(self.client.get(BASE+'/'+self.document,headers={'X-Layout-Token':'web'}).status_code,404)
            self.assertEqual(self.client.get(BASE+'/'+new_id,headers={'X-Layout-Token':'web'}).status_code,200)
