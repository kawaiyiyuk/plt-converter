"""Real HTTP upload limiter regressions; no Redis, identity service or PDF parser."""
import os
import tempfile
import unittest
from unittest.mock import patch
from app.projection_app import create_projection_app
from test_projection import BASE, synthetic_policy

class ProxyUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.configuration = synthetic_policy(self.temp.name)
        self.configuration['PROJECTOR_TRUSTED_PROXY_CIDRS'] = '10.20.0.2/32'
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch('app.projection.identify_user', side_effect=lambda auth: auth).start()
        patch('app.projection_store.time.time', return_value=1800000000).start()
        self.make_app()

    def make_app(self):
        self.app = create_projection_app(self.configuration)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def upload(self, owner, ip='198.51.100.1', peer='10.20.0.2', extra=None):
        headers = {'Authorization': owner, **(extra or {})}
        if ip is not None:
            headers['X-Forwarded-For'] = ip
        return self.client.post(BASE, headers=headers, environ_overrides={'REMOTE_ADDR': peer})

    def test_two_clients_behind_trusted_proxy_have_independent_ip_budgets(self):
        for index in range(30):
            self.assertEqual(self.upload(f'user:{index}').status_code, 400)
        self.assertEqual(self.upload('user:other', ip='198.51.100.2').status_code, 400)

    def test_single_client_ip_thirty_first_is_limited(self):
        for index in range(30):
            self.assertEqual(self.upload(f'user:{index}').status_code, 400)
        response = self.upload('user:next')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()['error_code'], 'projection_rate')
        self.assertEqual(response.headers['Retry-After'], '60')

    def test_account_limit_survives_changes_of_client_ip(self):
        for index in range(5):
            self.assertEqual(self.upload('user:same', ip=f'198.51.100.{index+1}').status_code, 400)
        self.assertEqual(self.upload('user:same', ip='198.51.100.200').status_code, 429)

    def assert_peer_budget_shared(self, peer, headers):
        for index in range(30):
            ip = headers[index % len(headers)]
            self.assertEqual(self.upload(f'user:{index}', ip=ip, peer=peer,
                                        extra={'X-Real-IP': f'203.0.113.{index+1}',
                                               'Forwarded': f'for=203.0.113.{index+1}'}).status_code, 400)
        self.assertEqual(self.upload('user:next', ip=headers[0], peer=peer).status_code, 429)

    def test_untrusted_peer_cannot_rotate_spoofed_headers(self):
        self.assert_peer_budget_shared('10.20.0.3', ['198.51.100.1', '198.51.100.2'])

    def test_no_configuration_ignores_even_valid_forwarding(self):
        self.configuration.pop('PROJECTOR_TRUSTED_PROXY_CIDRS')
        self.make_app()
        self.assert_peer_budget_shared('10.20.0.2', ['198.51.100.1', '198.51.100.2'])

    def test_missing_malformed_and_multivalue_xff_fall_back_to_peer(self):
        self.assert_peer_budget_shared('10.20.0.2', [None, '', 'garbage', '198.51.100.1, 198.51.100.2',
                                                  '198.51.100.1:80', '[2001:db8::1]', 'fe80::1%eth0'])

    def test_ipv6_equivalent_and_mapped_addresses_share_budget(self):
        for index in range(30):
            ip = '::ffff:198.51.100.1' if index % 2 else '198.51.100.1'
            self.assertEqual(self.upload(f'user:{index}', ip=ip, peer='::ffff:10.20.0.2').status_code, 400)
        self.assertEqual(self.upload('user:next').status_code, 429)

    def test_ipv6_trusted_peer_and_client_normalization(self):
        self.configuration['PROJECTOR_TRUSTED_PROXY_CIDRS'] = '2001:db8:1::/64'
        self.make_app()
        for index in range(30):
            ip = '2001:0db8:2:0:0:0:0:1' if index % 2 else '2001:db8:2::1'
            self.assertEqual(self.upload(f'user:{index}', ip=ip, peer='2001:db8:1::a').status_code, 400)
        self.assertEqual(self.upload('user:next', ip='2001:db8:2::1', peer='2001:db8:1::b').status_code, 429)
        self.assertEqual(self.upload('user:new', ip='2001:db8:2::2', peer='2001:db8:1::a').status_code, 400)

    def test_disabled_gate_remains_closed_before_identity_and_rate(self):
        self.configuration['PROJECTOR_ENABLED'] = '0'
        self.make_app()
        with patch('app.projection.identify_user', side_effect=AssertionError('identity must not run')):
            self.assertEqual(self.upload('user:next').status_code, 503)
        self.assertNotIn('projection_store', self.app.extensions)

    def test_invalid_configuration_fails_startup_even_with_gate_closed(self):
        self.configuration['PROJECTOR_ENABLED'] = '0'
        for value in ('0.0.0.0/0', '::/0', 'bad-peer', '10.20.0.2,', '10.20.0.2/24', 'fe80::1%eth0',
                      '0.0.0.0/1,128.0.0.0/1', '::/1,8000::/1',
                      '::ffff:0:0/97,::ffff:8000:0/97'):
            with self.subTest(value=value):
                self.configuration['PROJECTOR_TRUSTED_PROXY_CIDRS'] = value
                with self.assertRaises(ValueError) as error:
                    self.make_app()
                self.assertNotIn(value, str(error.exception))


class ProxyHelperTests(unittest.TestCase):
    def test_mapped_proxy_network_matches_native_peer(self):
        from app.projection_proxy import parse_trusted_proxy_cidrs, upload_rate_limit_ip
        peers = parse_trusted_proxy_cidrs('::ffff:10.20.0.2/128,2001:db8::1')
        self.assertEqual(upload_rate_limit_ip('10.20.0.2', '198.51.100.7', peers), '198.51.100.7')
        self.assertEqual(upload_rate_limit_ip('2001:db8::1', '::ffff:198.51.100.7', peers), '198.51.100.7')

    def test_invalid_peer_cannot_activate_forwarding(self):
        from app.projection_proxy import parse_trusted_proxy_cidrs, upload_rate_limit_ip
        peers = parse_trusted_proxy_cidrs('10.20.0.2')
        self.assertEqual(upload_rate_limit_ip('invalid-peer', '198.51.100.7', peers), 'invalid-peer')
        self.assertEqual(upload_rate_limit_ip(None, '198.51.100.7', peers), '')

    def test_parser_rejects_mapped_global_and_empty_entries(self):
        from app.projection_proxy import parse_trusted_proxy_cidrs
        for value in ('::ffff:0:0/96', ',10.20.0.2', '10.20.0.2,,10.20.0.3', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_trusted_proxy_cidrs(value)


class ProxyUnionConfigurationTests(unittest.TestCase):
    def test_combined_networks_cannot_trust_whole_address_spaces(self):
        from app.projection_proxy import parse_trusted_proxy_cidrs
        for value in ('0.0.0.0/1,128.0.0.0/1', '::/1,8000::/1',
                      '::ffff:0:0/97,::ffff:8000:0/97'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_trusted_proxy_cidrs(value)

    def test_normal_mixed_networks_still_allow_explicit_peers(self):
        from app.projection_proxy import parse_trusted_proxy_cidrs, upload_rate_limit_ip
        peers = parse_trusted_proxy_cidrs('10.20.0.0/24,2001:db8::/64,::ffff:192.168.5.1/128')
        for peer in ('10.20.0.2', '2001:db8::1', '192.168.5.1'):
            with self.subTest(peer=peer):
                self.assertEqual(upload_rate_limit_ip(peer, '198.51.100.7', peers), '198.51.100.7')
        self.assertEqual(upload_rate_limit_ip('198.51.100.9', '203.0.113.5', peers), '198.51.100.9')
