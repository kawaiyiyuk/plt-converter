"""Only offline preparation tests; no Docker, network or app initialization."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('production_prepare', ROOT / 'scripts/prepare-projector-production.py')
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)

class ProductionPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.policy = self.folder / 'policy.env'
        self.envpath = self.folder / 'production.env'
        self.values = dict(line.split('=', 1) for line in (ROOT / '.env.projector.example').read_text().splitlines() if line and not line.startswith('#'))
        self.values = {key: value for key, value in self.values.items() if key in prep.ENV_FIELDS}
        self.values.update(PROJECTOR_TRUSTED_PROXY_CIDRS='10.20.0.2/32', PROJECTOR_IMAGE='registry.invalid/projector@sha256:' + 'a'*64,
                           PROJECTOR_REDIS_IMAGE='registry.invalid/redis@sha256:' + 'b'*64,
                           PROJECTOR_POLICY_FILE=str(self.policy), PROJECTOR_PUBLIC_BASE_URL='https://files.invalid',
                           PROJECTOR_WX_BACKEND_URL='https://identity.invalid')
        self.policy_text = (ROOT / '.env.projector.policy.example').read_text()
        self.save()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(subprocess, 'Popen', side_effect=AssertionError('process forbidden')).start()
        patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')).start()
        patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')).start()
        patch.object(os, 'system', side_effect=AssertionError('shell forbidden')).start()

    def save(self):
        self.envpath.write_text(''.join(key+'='+value+'\n' for key,value in self.values.items()))
        self.policy.write_text(self.policy_text)

    def test_service_token_is_not_required_or_allowed(self):
        self.assertNotIn('PROJECTOR_SERVICE_TOKEN', prep.ENV_FIELDS)
        self.assertEqual(prep.prepare(self.envpath)['status'], 'offline-inputs-valid')
        self.values['PROJECTOR_SERVICE_TOKEN'] = 'fixture-never-print'
        self.save()
        with self.assertRaises(prep.Invalid):
            prep.prepare(self.envpath)

    def test_valid_plan_offline_and_redacted(self):
        plan = prep.prepare(self.envpath)
        self.assertEqual(plan['status'], 'offline-inputs-valid')
        self.assertFalse(plan['production_preflight'])
        self.assertEqual(plan['release_gate'], 'closed')
        rendered = json.dumps(plan)
        for value in ('secret-fixture-NEVER-PRINT', 'identity.invalid', str(self.policy), '10.20.0.2'):
            self.assertNotIn(value, rendered)
        self.assertEqual(plan['services'], ['projector-api','projector-layout-worker','projector-redis'])

    def test_incomplete_example_only_field_names(self):
        self.envpath.write_text((ROOT / '.env.projector.production.example').read_text())
        plan = prep.prepare(self.envpath)
        self.assertEqual(set(plan), {'status', 'missing_fields'})
        self.assertNotIn('PROJECTOR_SERVICE_TOKEN', plan['missing_fields'])

    def test_proxy_peers_are_explicit_required_and_redacted(self):
        for value in ('', None):
            if value is None:
                self.values.pop('PROJECTOR_TRUSTED_PROXY_CIDRS', None)
            else:
                self.values['PROJECTOR_TRUSTED_PROXY_CIDRS'] = value
            self.save()
            self.assertIn('PROJECTOR_TRUSTED_PROXY_CIDRS', prep.prepare(self.envpath)['missing_fields'])

    def test_duplicate_env_and_policy(self):
        for path, line in ((self.envpath, 'PROJECTOR_ENABLED=0'), (self.policy,'PROJECTOR_TILE_RATE=3')):
            self.save()
            with path.open('a') as handle:
                handle.write('\n'+line+'\n')
            with self.assertRaises(prep.Invalid):
                prep.prepare(self.envpath)

    def test_policy_injection_and_shell_override(self):
        for key in ('HTTPS_PROXY', 'http_proxy', 'NO_PROXY', 'WX_BACKEND_URL'):
            self.policy_text += '\n'+key+'=secret-fixture-NEVER-PRINT\n'
            self.save()
            with self.assertRaises(prep.Invalid):
                prep.prepare(self.envpath)
            self.policy_text = self.policy_text.rsplit('\n'+key,1)[0]
        self.save()
        with patch.dict(os.environ, {'PROJECTOR_UNLISTED': 'override'}):
            with self.assertRaises(prep.Invalid):
                prep.prepare(self.envpath)

    def test_invalid_values(self):
        cases = {'PROJECTOR_TRUSTED_PROXY_CIDRS':['bad-peer', '0.0.0.0/0', '::/0', '::ffff:0:0/96', '10.20.0.2,,10.20.0.3', '0.0.0.0/1,128.0.0.0/1', '::/1,8000::/1', '::ffff:0:0/97,::ffff:8000:0/97'], 'PROJECTOR_ENABLED':['1'], 'PROJECTOR_BIND_ADDRESS':['0.0.0.0'],
                 'PROJECTOR_BIND_PORT':['8091'], 'PROJECTOR_IMAGE':['registry.invalid/app:latest'],
                 'PROJECTOR_WX_BACKEND_URL':['http://identity.invalid','https://user:secret@identity.invalid','https://identity.invalid/a'],
                 'PROJECTOR_API_CPUS':['NaN','Infinity','-1','0'],
                 'PROJECTOR_API_PIDS':['-1','1.5','0'], 'PROJECTOR_API_MEMORY':['NaN','0','-1g'],
                 'PROJECTOR_HTTP_TIMEOUT':['0','-1','NaN'], 'PROJECTOR_REDIS_MEMORY':['64m']}
        for key, values in cases.items():
            original = self.values[key]
            for value in values:
                with self.subTest(key=key, value=value):
                    self.values[key] = value
                    self.save()
                    with self.assertRaises(prep.Invalid):
                        prep.prepare(self.envpath)
            self.values[key] = original

    def test_policy_values_and_confirmed_limits(self):
        original = self.policy_text
        for old,new in [('PROJECTOR_TILE_RATE=3','PROJECTOR_TILE_RATE=4'),
                        ('PROJECTOR_OWNER_RENDERERS=1','PROJECTOR_OWNER_RENDERERS=NaN'),
                        ('PROJECTOR_PARSE_WALL_SECONDS=90','PROJECTOR_PARSE_WALL_SECONDS=0'),
                        ('PROJECTOR_GLOBAL_RENDERERS=2','PROJECTOR_GLOBAL_RENDERERS=1')]:
            self.policy_text = original.replace(old,new).replace('PROJECTOR_OWNER_RENDERERS=1','PROJECTOR_OWNER_RENDERERS=2') if old.endswith('RENDERERS=2') else original.replace(old,new)
            self.save()
            with self.assertRaises(prep.Invalid):
                prep.prepare(self.envpath)

        self.policy_text = original
        for field in ('PROJECTOR_HTTP_TIMEOUT', 'PROJECTOR_PARSE_WALL_SECONDS', 'PROJECTOR_TILE_WALL_SECONDS'):
            with self.subTest(timeout_field=field):
                saved = self.values.get(field)
                if field == 'PROJECTOR_HTTP_TIMEOUT':
                    self.values[field] = '121'
                else:
                    self.policy_text = original.replace(field + '=' + ('90' if 'PARSE' in field else '30'), field + '=121')
                self.save()
                plan = prep.prepare(self.envpath)
                self.assertEqual(plan['status'], 'offline-inputs-valid')
                self.assertTrue(plan['timeout_review_required'])
                self.assertTrue(plan['warnings'])
                self.assertFalse(plan['production_preflight'])
                output = io.StringIO()
                with patch('sys.argv', ['prepare', '--env-file', str(self.envpath), '--check']), contextlib.redirect_stdout(output):
                    self.assertEqual(prep.main(), 0)
                checked = json.loads(output.getvalue())
                self.assertTrue(checked['timeout_review_required'])
                self.assertFalse(checked['production_preflight'])
                self.assertNotIn('secret-fixture-NEVER-PRINT', output.getvalue())
                if saved is not None:
                    self.values[field] = saved
                self.policy_text = original

    def test_cli_never_echoes_invalid_secret(self):
        self.values['PROJECTOR_SERVICE_TOKEN'] = 'secret fixture NEVER PRINT'
        self.save()
        output = io.StringIO()
        with patch('sys.argv', ['prepare','--env-file',str(self.envpath)]), contextlib.redirect_stdout(output):
            self.assertEqual(prep.main(), 2)
        self.assertNotIn('secret fixture', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'], 'invalid')

    def test_cli_check_and_unknown_key_do_not_leak(self):
        output = io.StringIO()
        with patch('sys.argv', ['prepare','--env-file',str(self.envpath),'--check']), contextlib.redirect_stdout(output):
            self.assertEqual(prep.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {'status':'offline-inputs-valid'})
        with self.envpath.open('a') as handle:
            handle.write('secret-fixture-NEVER-PRINT=oops\n')
        with self.assertRaises(prep.Invalid) as caught:
            prep.prepare(self.envpath)
        self.assertNotIn('secret-fixture', str(caught.exception))

if __name__ == '__main__':
    unittest.main()
