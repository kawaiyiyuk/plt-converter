"""Projection manifest/template checks; never contact a Docker daemon."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {'projector-api', 'projector-layout-worker', 'projector-redis'}

class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='projector-repair-manifest-', dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.policy = self.folder / 'policy.env'
        self.policy_base = (ROOT / '.env.projector.policy.example').read_text()
        self.policy.write_text(self.policy_base)
        self.env = self.folder / 'review.env'
        self.env.write_text((ROOT / '.env.projector.example').read_text().replace('/private/tmp/projector-policy.review.env', str(self.policy)))
        self.env_base = self.env.read_text()

    def run_script(self, *args, expected=0, env=None):
        proc = subprocess.run(['bash', str(ROOT/'scripts/deploy-projector.sh'), '--target-scope', 'local-review', '--env-file', str(self.env), *args], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
        return proc

    def test_dryrun_and_offline_no_daemon_commands(self):
        for mode in ('--dry-run', '--offline'):
            result = self.run_script(mode)
            self.assertIn('No deploy, rollback', result.stdout)
        self.run_script('--execute-production', expected=2)
        self.run_script('--apply-local', '--docker-socket', 'tcp://server:2375', expected=2)
        self.run_script('--apply-local', '--docker-socket', 'ssh://server', expected=2)

    def test_source_and_image_allowlist(self):
        text = (ROOT/'Dockerfile.projection').read_text()
        self.assertNotIn('pip install', text)
        self.assertNotIn('COPY app ./app', text)
        self.assertNotIn('COPY worker.py', text)
        self.assertIn('projection_bootstrap.py', text)
        script = (ROOT/'scripts/deploy-projector.sh').read_text()
        self.assertNotIn(' compose down', script)
        self.assertNotIn('image prune', script)
        self.assertIn('--no-build --pull never --wait', script)
        self.assertIn('docker --host "unix://$socket"', script)

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI unavailable')
    def test_compose_static_two_project_names(self):
        images=[]
        for name in ('fengrenjiyi-projector', 'projector-review-alt'):
            proc = subprocess.run(['docker','compose','-p',name,'--env-file',str(self.env),'-f',str(ROOT/'compose.projector.yaml'),'config','--format','json'], capture_output=True, text=True)
            self.assertEqual(proc.returncode,0,proc.stderr)
            config=json.loads(proc.stdout)
            self.assertEqual(set(config['services']),SERVICES)
            api=config['services']['projector-api']; worker=config['services']['projector-layout-worker']; redis=config['services']['projector-redis']
            self.assertEqual(api['image'],worker['image'])
            images.append(api['image'])
            self.assertEqual(api['environment']['PROJECTOR_ENABLED'],'0')
            self.assertEqual(api['environment']['PROJECTOR_ROOT'],'/var/lib/projector')
            for service in (api, worker):
                self.assertEqual(service['environment'].get('PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS'), '8')
            self.assertNotIn('PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS', redis.get('environment', {}))
            self.assertEqual(api['environment']['PROJECTOR_REDIS_URL'],'redis://projector-redis:6379/0')
            self.assertEqual(set(api['depends_on']),{'projector-redis'})
            self.assertEqual(api['pid'],'service:projector-redis')
            self.assertEqual(worker['pid'],'service:projector-redis')
            self.assertTrue(redis['init'])
            self.assertNotIn('pid',redis)
            self.assertEqual(int(api['mem_limit']),2147483648)
            self.assertEqual(int(worker['mem_limit']),1073741824)
            self.assertEqual(int(redis['mem_limit']),134217728)
            self.assertEqual(set(worker['depends_on']),{'projector-redis'})
            self.assertTrue(api['read_only']); self.assertTrue(worker['read_only'])
            self.assertEqual(api['user'], '10001:10001')
            self.assertEqual(worker['user'], '10001:10001')
            self.assertEqual(api['ports'][0]['published'],'8092')
            self.assertEqual(redis['user'],'redis')
            self.assertTrue(redis['read_only'])
            self.assertEqual(redis['cap_drop'],['ALL'])
            self.assertIn('no-new-privileges:true',redis['security_opt'])
            self.assertEqual(redis['tmpfs'],['/data:size=1m,mode=1777,noexec,nosuid'])
            self.assertEqual(redis['command'],['redis-server','--save','','--appendonly','no','--maxmemory','64mb','--maxmemory-policy','noeviction'])
            self.assertNotIn('converter-temp',config['volumes'])
            self.assertTrue(all(x['pull_policy']=='never' for x in config['services'].values()))
        self.assertEqual(images[0],images[1])

    def test_local_executor_mock_only_new_services(self):
        bind = socket.socket(socket.AF_UNIX)
        sock = self.folder / 'docker.sock'
        self.addCleanup(bind.close); bind.bind(str(sock))
        log = self.folder / 'docker-commands.jsonl'
        fake = self.folder / 'docker'
        fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ['PROJECTOR_TEST_LOG'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
        fake.chmod(0o700)
        self.env.write_text(self.env.read_text().replace('PROJECTOR_ENABLED=0', 'PROJECTOR_ENABLED=1'))
        env = dict(os.environ, PATH=str(self.folder)+os.pathsep+os.environ['PATH'], PROJECTOR_TEST_LOG=str(log), DOCKER_HOST='ssh://forbidden-production')
        proc=subprocess.run(['bash',str(ROOT/'scripts/start-projector-local.sh'),'--start-local','--env-file',str(self.env),'--docker-socket',str(sock)],env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        commands=[json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(commands),4)
        for command in commands:
            self.assertEqual(command[:2],['--host','unix://'+str(sock)])
            self.assertIn('fengrenjiyi-projector',command)
            self.assertFalse(any(x in command for x in ('down','stop','prune','build','pull','restart')))
        up=commands[1]
        self.assertEqual(up[-3:],['projector-redis','projector-layout-worker','projector-api'])
        self.assertIn('--force-recreate',up)
        self.assertIn('--no-build',up); self.assertIn('never',up)

    def test_base_alias_collision_refuses_overwrite(self):
        bind=socket.socket(socket.AF_UNIX); self.addCleanup(bind.close)
        sock=self.folder/'base.sock'; bind.bind(str(sock))
        alias='localhost:1/fengrenjiyi-projector-base:review-60ac62f0cd72-11111111-2222-4333-8444-555555555555'
        self.env.write_text(self.env.read_text().replace('localhost:1/fengrenjiyi-projector-base:review-60ac62f0cd72-CHANGE-TO-UNIQUE-UUID',alias))
        log=self.folder/'base-log'
        fake=self.folder/'docker'
        fake.write_text("#!/usr/bin/env python3\nimport sys,os\nwith open(os.environ['PROJECTOR_TEST_LOG'],'a') as f: f.write(' '.join(sys.argv[1:])+'\\n')\nprint('sha256:60ac62f0cd72b32f5914385cf7ab299f1987b2dc8960b98e34574bbdd1557fc3' if 'sha256:60ac62f0cd72b32f5914385cf7ab299f1987b2dc8960b98e34574bbdd1557fc3' in sys.argv else 'sha256:mismatch')\n")
        fake.chmod(0o700)
        env=dict(os.environ,PATH=str(self.folder)+os.pathsep+os.environ['PATH'],PROJECTOR_TEST_LOG=str(log))
        proc=subprocess.run(['bash',str(ROOT/'scripts/deploy-projector.sh'),'--prepare-base-local','--target-scope','local-review','--env-file',str(self.env),'--docker-socket',str(sock)],env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,2,proc.stdout+proc.stderr)
        self.assertIn('Refusing to overwrite',proc.stderr)
        self.assertNotIn('image tag',log.read_text())

    def test_approved_base_only(self):
        self.env.write_text(self.env.read_text().replace('PROJECTOR_BASE_ID=sha256:60ac62f0cd72b32f5914385cf7ab299f1987b2dc8960b98e34574bbdd1557fc3','PROJECTOR_BASE_ID=python:latest'))
        self.run_script('--offline',expected=1)

    def test_non_root_container_and_volume_ownership(self):
        compose = (ROOT/'compose.projector.yaml').read_text()
        self.assertIn('user: "10001:10001"', compose)
        dockerfile = (ROOT/'Dockerfile.projection').read_text()
        self.assertIn('COPY --chown=10001:10001 docker/projector-data/', dockerfile)
        self.assertNotIn('RUN', dockerfile)
        script = (ROOT/'scripts/deploy-projector.sh').read_text()
        self.assertIn('docker/projector-data/.keep', script)
        self.assertTrue((ROOT/'docker/projector-data/.keep').is_file())

    def test_b4_shell_projector_variable_bypass_is_rejected(self):
        result = self.run_script('--offline', expected=1,
                                 env=dict(os.environ, PROJECTOR_WX_BACKEND_URL='https://good.internal.example'))
        self.assertIn('Shell environment', result.stdout + result.stderr)
        # A variable the env file omits (compose default) must not be injectable either.
        self.env.write_text('\n'.join(line for line in self.env_base.splitlines()
                                       if not line.startswith('PROJECTOR_BIND_ADDRESS=')) + '\n')
        result = self.run_script('--offline', expected=1,
                                 env=dict(os.environ, PROJECTOR_BIND_ADDRESS='0.0.0.0'))
        self.assertIn('Shell environment', result.stdout + result.stderr)

    def test_b4_wx_backend_url_must_be_http_and_non_production(self):
        for bad in ('https://api.fengrenjiyi.com', 'http://fengrenjiyi.com',
                    'https://sub.fengrenjiyi.com', 'not-a-url', 'ftp://internal'):
            with self.subTest(bad=bad):
                self.env.write_text(self.env_base.replace(
                    'PROJECTOR_WX_BACKEND_URL=http://host.docker.internal:5001',
                    'PROJECTOR_WX_BACKEND_URL=' + bad))
                self.run_script('--offline', expected=1)

    def test_e1_wx_backend_url_allowlist_rejects_aliases_and_public_hosts(self):
        for bad in ('http://api.fengrenjiyi%2Ecom', 'http://api.fengrenjiyi。com',
                    'http://8.8.8.8:5001', 'http://example.com:5001', 'http://169.254.169.254',
                    'http://134744072:5001', 'http://0x08080808:5001', 'http://2130706433:5001'):
            with self.subTest(bad=bad):
                self.env.write_text(self.env_base.replace(
                    'PROJECTOR_WX_BACKEND_URL=http://host.docker.internal:5001',
                    'PROJECTOR_WX_BACKEND_URL=' + bad))
                self.run_script('--offline', expected=1)
        for good in ('http://192.168.1.5:5001', 'http://127.0.0.1:5001',
                     'http://wx-server:5001', 'http://host.docker.internal:5001'):
            with self.subTest(good=good):
                self.env.write_text(self.env_base.replace(
                    'PROJECTOR_WX_BACKEND_URL=http://host.docker.internal:5001',
                    'PROJECTOR_WX_BACKEND_URL=' + good))
                self.run_script('--offline')

    def test_b4_bind_address_must_be_loopback_or_private_ipv4(self):
        for bad in ('0.0.0.0', '8.8.8.8', '::1', '127.0.0.2', 'not-an-ip'):
            with self.subTest(bad=bad):
                self.env.write_text(self.env_base.replace(
                    'PROJECTOR_BIND_ADDRESS=127.0.0.1',
                    'PROJECTOR_BIND_ADDRESS=' + bad))
                self.run_script('--offline', expected=1)

    def test_b4_prepare_context_rejects_traversal_and_non_tmp(self):
        self.run_script('--offline', '--prepare-context',
                        '/private/tmp/projector-build.review/../evil', expected=1)
        self.run_script('--offline', '--prepare-context',
                        '/etc/projector-build.review', expected=1)
        self.run_script('--offline', '--prepare-context',
                        '/private/tmp/other-build.review', expected=1)

    def test_b4_apply_local_requires_enabled_gate(self):
        bind = socket.socket(socket.AF_UNIX)
        self.addCleanup(bind.close)
        sock = self.folder / 'enabled.sock'
        bind.bind(str(sock))
        result = self.run_script('--apply-local', '--docker-socket', str(sock), expected=2)
        self.assertIn('PROJECTOR_ENABLED=1', result.stderr)

    def test_c4_export_prefix_is_parsed_and_validated(self):
        self.env.write_text(self.env_base.replace(
            'PROJECTOR_BIND_ADDRESS=127.0.0.1', 'export PROJECTOR_BIND_ADDRESS=0.0.0.0'))
        result = self.run_script('--offline', expected=1)
        self.assertIn('PROJECTOR_BIND_ADDRESS', result.stdout + result.stderr)

    def test_c4_duplicate_env_key_is_rejected(self):
        self.env.write_text(self.env_base + 'PROJECTOR_ENABLED=0\n')
        result = self.run_script('--offline', expected=1)
        self.assertIn('Duplicate key', result.stdout + result.stderr)

    def test_c4_line_without_equals_is_rejected(self):
        self.env.write_text(self.env_base + 'this line has no separator\n')
        result = self.run_script('--offline', expected=1)
        self.assertIn('without "="', result.stdout + result.stderr)

    def test_c4_trailing_dot_host_is_rejected(self):
        self.env.write_text(self.env_base.replace(
            'PROJECTOR_WX_BACKEND_URL=http://host.docker.internal:5001',
            'PROJECTOR_WX_BACKEND_URL=http://api.fengrenjiyi.com./'))
        result = self.run_script('--offline', expected=1)
        self.assertIn('fengrenjiyi.com', result.stdout + result.stderr)

    def test_c4_apply_local_still_rejects_disabled_gate(self):
        bind = socket.socket(socket.AF_UNIX)
        self.addCleanup(bind.close)
        sock = self.folder / 'c4-enabled.sock'
        bind.bind(str(sock))
        result = self.run_script('--apply-local', '--docker-socket', str(sock), expected=2)
        self.assertIn('PROJECTOR_ENABLED=1', result.stderr)

    def test_d2_env_comment_smuggling_is_rejected(self):
        self.env.write_text(self.env_base.replace(
            'PROJECTOR_WX_BACKEND_URL=http://host.docker.internal:5001',
            'PROJECTOR_WX_BACKEND_URL=https://api.fengrenjiyi.com # x'))
        result = self.run_script('--offline', expected=1)
        self.assertIn('unsupported character', result.stdout + result.stderr)

    def test_d2_env_variable_expansion_is_rejected(self):
        self.env.write_text(self.env_base.replace(
            'PROJECTOR_WX_BACKEND_URL=http://host.docker.internal:5001',
            'PROJECTOR_WX_BACKEND_URL=https://${H}'))
        result = self.run_script('--offline', expected=1,
                                 env=dict(os.environ, H='api.fengrenjiyi.com'))
        self.assertIn('unsupported character', result.stdout + result.stderr)

    def test_d2_quoted_value_is_rejected(self):
        self.env.write_text(self.env_base.replace(
            'PROJECTOR_BIND_ADDRESS=127.0.0.1',
            'PROJECTOR_BIND_ADDRESS="0.0.0.0"'))
        result = self.run_script('--offline', expected=1)
        self.assertIn('unsupported character', result.stdout + result.stderr)

    def test_f1_policy_file_rejects_proxy_and_foreign_keys(self):
        for bad in ('http_proxy=http://api.fengrenjiyi.com:80',
                    'HTTPS_PROXY=http://127.0.0.1:9',
                    'WX_BACKEND_URL=http://127.0.0.1:5001'):
            with self.subTest(bad=bad):
                self.policy.write_text(self.policy_base + bad + '\n')
                result = self.run_script('--offline', expected=1)
                output = result.stdout + result.stderr
                self.assertIn(bad.split('=', 1)[0], output)
                self.assertIn('not allowed in policy file', output)

    def test_f1_policy_file_rejects_duplicate_key(self):
        self.policy.write_text(self.policy_base + 'PROJECTOR_GLOBAL_PARSERS=1\n')
        result = self.run_script('--offline', expected=1)
        self.assertIn('Duplicate key', result.stdout + result.stderr)

    def test_f1_missing_policy_file_is_rejected(self):
        self.env.write_text(self.env_base.replace(str(self.policy), str(self.folder / 'absent.env')))
        result = self.run_script('--offline', expected=1)
        self.assertIn('PROJECTOR_POLICY_FILE', result.stdout + result.stderr)

    def test_f1_example_policy_still_passes(self):
        self.run_script('--offline')

if __name__ == '__main__': unittest.main()
