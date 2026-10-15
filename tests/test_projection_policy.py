import io
import math
import time
import unittest
from unittest.mock import patch
from app.projection_store import Policy,ProjectionRejected,Store
import test_projection as fixtures
import test_projection_resources as resource_fixtures

class ApprovedPolicyTests(unittest.TestCase):
    setUp=fixtures.ProjectionTests.setUp
    identity=fixtures.ProjectionTests.identity
    upload=fixtures.ProjectionTests.upload
    tile=fixtures.ProjectionTests.tile
    renew=fixtures.ProjectionTests.renew
    metadata=fixtures.ProjectionTests.metadata
    create=resource_fixtures.ResourceTests.create

    def policy_restore(self):self.ledger.policy=Policy(self.configuration)

    def test_error_body_retry_after_matches_header(self):
        with patch.object(self.ledger,'rate',side_effect=ProjectionRejected('projection_rate','fixture',429,17)):
            response=self.upload()
        self.assertEqual(response.get_json()['retry_after'],17)
        self.assertEqual(response.headers['Retry-After'],'17')

    def test_upload_fixed_minute_never_refills_mid_window(self):
        self.policy_restore();start=math.floor(time.time()/60)*60
        with patch('app.projection_store.time.time',return_value=start):
            for _ in range(5):Store.rate(self.ledger,self.owner,'ip','upload')
        with patch('app.projection_store.time.time',return_value=start+59),self.assertRaises(ProjectionRejected) as error:
            Store.rate(self.ledger,self.owner,'ip','upload')
        self.assertEqual(error.exception.retry_after,1)
        with patch('app.projection_store.time.time',return_value=start+60):
            Store.rate(self.ledger,self.owner,'ip','upload')

    def test_ip_window_caps_thirty_even_with_new_owners(self):
        self.policy_restore();start=math.floor(time.time()/60)*60
        with patch('app.projection_store.time.time',return_value=start):
            for index in range(30):Store.rate(self.ledger,f'user:{index}','shared-ip','upload')
            with self.assertRaises(ProjectionRejected):Store.rate(self.ledger,'next-owner','shared-ip','upload')
        with patch('app.projection_store.time.time',return_value=start+60):
            Store.rate(self.ledger,'next-owner','shared-ip','upload')

    def test_upload_cannot_consume_render_pool_and_full_budget_still_renders(self):
        doc=self.create();policy=self.ledger.policy
        self.assertEqual(policy.render_pool_bytes,2*policy.operation_bytes)
        with self.ledger.transaction() as db:
            session=db.execute('SELECT SUM(budget) FROM sessions').fetchone()[0]
        # At exactly reserved capacity, new admission fails; a legitimate
        # existing tile uses the pre-reserved renderer slot without extra bytes.
        policy.global_disk_bytes=policy.system_bytes+session+policy.render_pool_bytes
        with self.assertRaises(ProjectionRejected):self.ledger.reserve('user:new','upload')
        self.assertEqual(self.tile(doc).status_code,200)
        self.assertEqual(self.renew(doc).status_code,200)
        self.assertTrue(self.ledger.source_path(doc).exists())

    def test_tile_rate_remains_three_per_second_burst_five(self):
        self.policy_restore();start=time.time()
        with patch('app.projection_store.time.time',return_value=start):
            for _ in range(5):Store.rate(self.ledger,self.owner,'','tile')
            with self.assertRaises(ProjectionRejected):Store.rate(self.ledger,self.owner,'','tile')
        with patch('app.projection_store.time.time',return_value=start+1):
            for _ in range(3):Store.rate(self.ledger,self.owner,'','tile')
            with self.assertRaises(ProjectionRejected):Store.rate(self.ledger,self.owner,'','tile')
