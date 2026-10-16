#!/usr/bin/env python3
"""Offline production preparation only. Never imports app or calls a service."""
import argparse
import ast
from decimal import Decimal, InvalidOperation
import json
import importlib.util
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FIXED_POLICY = {
    'OWNER_MAX_DOCUMENTS': 2, 'OWNER_SOURCE_BYTES': 41943040,
    'GLOBAL_DISK_BYTES': 2147483648, 'FREE_FLOOR_BYTES': 2147483648,
    'UPLOAD_OWNER_RATE': 5, 'UPLOAD_IP_RATE': 30, 'TILE_RATE': 3, 'TILE_BURST': 5,
}
BUDGETS = ('API_MEMORY API_CPUS API_PIDS WORKER_MEMORY WORKER_CPUS WORKER_PIDS '
           'REDIS_MAXMEMORY REDIS_MEMORY REDIS_CPUS REDIS_PIDS HTTP_THREADS HTTP_TIMEOUT').split()
ENV_FIELDS = {'PROJECTOR_' + name for name in BUDGETS} | {
    'PROJECTOR_TRUSTED_PROXY_CIDRS', 'PROJECTOR_ENABLED', 'PROJECTOR_IMAGE', 'PROJECTOR_REDIS_IMAGE',
    'PROJECTOR_POLICY_FILE', 'PROJECTOR_BIND_ADDRESS', 'PROJECTOR_BIND_PORT',
    'PROJECTOR_WX_BACKEND_URL', 'PROJECTOR_PUBLIC_BASE_URL', 'PROJECTOR_SERVICE_TOKEN',
}

class Invalid(ValueError):
    pass


def parse_file(path, allowed):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise Invalid('Invalid env line (contents redacted)')
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if key not in allowed:
            raise Invalid('Unknown or disallowed field (contents redacted)')
        if key in values:
            raise Invalid('Duplicate field: ' + key)
        if re.search(r"[\s#$\"'`\\]", value):
            raise Invalid('Unsupported literal characters: ' + key)
        values[key] = value
    return values


def schema():
    # Compile only trusted literal assignments and Policy, never app/__init__.py
    # or module top-level code, which may initialize services.
    tree = ast.parse((ROOT / 'app/projection_store.py').read_text())
    names = {'MAX_SOURCE', 'MAX_BODY', 'META_BOUND', 'PNG_BOUND', 'FIELDS'}
    nodes = [node for node in tree.body if
             (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets))
             or (isinstance(node, ast.ClassDef) and node.name == 'Policy')]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<static-policy>', 'exec'), namespace)
    fields = {'PROJECTOR_' + field for field in namespace['FIELDS']}
    fields |= {'PROJECTOR_DERIVED_MAX_BYTES', 'PROJECTOR_OWNER_DERIVED_BYTES'}
    return fields, namespace['Policy']


def pending(value):
    return not value or any(marker in value.lower() for marker in
                            ('replace', 'change-', 'todo', 'pending', '<', '>', 'example', '待填'))


def positive_integer(key, value):
    if not re.fullmatch(r'[1-9][0-9]{0,15}', value) or int(value) > 2**53 - 1:
        raise Invalid('Positive integer required: ' + key)


def byte_size(key, value):
    match = re.fullmatch(r'([1-9][0-9]{0,18})([kmgt]?b?)?', value.lower())
    if not match:
        raise Invalid('Positive byte size required: ' + key)
    unit = (match[2] or '').rstrip('b')
    size = int(match[1]) * 1024 ** ('kmgt'.index(unit) + 1 if unit else 0)
    if size > 2**63 - 1:
        raise Invalid('Byte size exceeds supported range: ' + key)
    return size


def https_url(key, value):
    try:
        parsed = urlsplit(value)
        valid = (value.isascii() and '%' not in value and parsed.scheme == 'https'
                 and parsed.hostname and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment and parsed.path in ('', '/')
                 and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise Invalid('Explicit HTTPS origin required: ' + key)


def prepare(envpath):
    policy_fields, policy_class = schema()
    env = parse_file(envpath, ENV_FIELDS)
    if any(key.startswith('PROJECTOR_') for key in os.environ):
        raise Invalid('Unset shell PROJECTOR_* variables before offline checking')
    missing = sorted(key for key in ENV_FIELDS if pending(env.get(key)))
    if missing:
        return {'status': 'incomplete', 'missing_fields': missing}
    # Load only the stdlib helper, never the app package or its service setup.
    spec = importlib.util.spec_from_file_location('projector_proxy_offline', ROOT / 'app/projection_proxy.py')
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    try:
        proxy.parse_trusted_proxy_cidrs(env['PROJECTOR_TRUSTED_PROXY_CIDRS'])
    except ValueError:
        raise Invalid('Invalid or unrestricted PROJECTOR_TRUSTED_PROXY_CIDRS (values redacted)') from None
    policy_path = Path(env['PROJECTOR_POLICY_FILE'])
    if not policy_path.is_absolute() or not policy_path.is_file():
        raise Invalid('Existing absolute path required: PROJECTOR_POLICY_FILE')
    policy = parse_file(policy_path, policy_fields)
    missing = sorted(key for key in policy_fields if pending(policy.get(key)))
    if missing:
        return {'status': 'incomplete', 'missing_fields': missing}
    for key, value in policy.items():
        positive_integer(key, value)
    try:
        policy_class(policy)
    except (ValueError, TypeError, OverflowError):
        raise Invalid('Policy consistency check failed (values redacted)') from None
    for field, expected in FIXED_POLICY.items():
        if policy['PROJECTOR_' + field] != str(expected):
            raise Invalid('Confirmed product policy differs: PROJECTOR_' + field)
    if env['PROJECTOR_ENABLED'] != '0':
        raise Invalid('Preparation requires PROJECTOR_ENABLED=0')
    if env['PROJECTOR_BIND_ADDRESS'] != '127.0.0.1' or env['PROJECTOR_BIND_PORT'] != '8092':
        raise Invalid('Preparation requires loopback 127.0.0.1:8092')
    for key in ('PROJECTOR_IMAGE', 'PROJECTOR_REDIS_IMAGE'):
        if not re.fullmatch(r'[a-z0-9][a-z0-9.:-]*/[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}', env[key]):
            raise Invalid('Immutable registry digest required: ' + key)
    for key in ('PROJECTOR_WX_BACKEND_URL', 'PROJECTOR_PUBLIC_BASE_URL'):
        https_url(key, env[key])
    sizes = {}
    for field in BUDGETS:
        key, value = 'PROJECTOR_' + field, env['PROJECTOR_' + field]
        if field.endswith('CPUS'):
            try:
                number = Decimal(value)
            except InvalidOperation:
                raise Invalid('Finite positive CPU budget required: ' + key) from None
            if not number.is_finite() or number <= 0 or number > Decimal(2**31 - 1):
                raise Invalid('Finite positive CPU budget required: ' + key)
        elif field.endswith(('MEMORY', 'MAXMEMORY')):
            sizes[field] = byte_size(key, value)
        else:
            positive_integer(key, value)
    if sizes['REDIS_MEMORY'] <= sizes['REDIS_MAXMEMORY']:
        raise Invalid('Redis container memory must exceed Redis maxmemory')
    timeout_review = any(int(v) > 120 for v in (env['PROJECTOR_HTTP_TIMEOUT'], policy['PROJECTOR_PARSE_WALL_SECONDS'], policy['PROJECTOR_TILE_WALL_SECONDS']))
    return {
        'status': 'offline-inputs-valid', 'production_preflight': False, 'release_gate': 'closed',
        'timeout_review_required': timeout_review,
        'warnings': ['Explicit HTTP/parse/tile budget exceeds this package nginx example 120s inactivity timeout; manually review matching gateway and application budgets. This is not a unified application wall-clock cap.'] if timeout_review else [],
        'disabled_backend': 'PROJECTOR_ENABLED=0: worker does not start and readiness returns 503 by design; no up/wait success claim',
        'project': 'fengrenjiyi-projector',
        'services': ['projector-api', 'projector-layout-worker', 'projector-redis'],
        'bind': '127.0.0.1:8092', 'gateway_prefix': '/api/v1/pdf/projection/',
        'storage': 'independent disk-backed projector-data volume; no filesystem hard quota',
        'redis': 'independent nonpersistent queue; noeviction; no host port',
        'pid': 'api/worker share only projector-redis PID namespace',
        'shared_host': 'physical CPU/RAM/disk remain shared with PDF/PLT stack',
        'limits': '21MiB HTTP body / 20MiB source; existing confirmed policy retained',
        'future_authorized_manual_work': [
            'build/distribute verified immutable images; local approved base IDs are not production provenance',
            'approve host capacity and engineering budgets; match identity token without disclosure',
            'review gateway/TLS and URI/Authorization forwarding; verify exact connection peers and any CDN real-IP trust',
            'coordinate same-version three-service force-recreate, health checks and rollback',
            'analysis may be interrupted by replacement or nonpersistent Redis restart; source originals remain',
            'future authorized private-backend operation needs explicit backend enabled=1; frontend release gates stay closed until acceptance and separate approval',
            'physical ruler acceptance remains separate'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--check', action='store_true', help='report status, missing fields and any required timeout review')
    args = parser.parse_args()
    try:
        plan = prepare(args.env_file)
    except (Invalid, OSError, SyntaxError):
        # Never echo paths, raw values, parser exceptions or credentials.
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, Invalid) else 'Input/source unavailable or invalid (details redacted)'
        print(json.dumps({'status': 'invalid', 'error': message}, ensure_ascii=False))
        return 2
    print(json.dumps({k: v for k, v in plan.items() if k in ('status', 'missing_fields') or (plan.get('timeout_review_required') and k in ('timeout_review_required', 'warnings', 'production_preflight'))}
                     if args.check else plan, ensure_ascii=False, indent=2))
    return 0 if plan['status'] == 'offline-inputs-valid' else 2

if __name__ == '__main__':
    raise SystemExit(main())
