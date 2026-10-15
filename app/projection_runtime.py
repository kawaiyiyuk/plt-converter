"""Synchronous supervisor of a *separate* bounded/reapable PDF process.

No preexec_fn in a threaded web server and no cancelable Future pretending to
stop native PDF work. Admission/generation leases stay counted until wait().
"""
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .projection_store import META_BOUND, PNG_BOUND, ProjectionRejected

POLL_SECONDS = 0.05
TERM_GRACE_SECONDS = 0.25

logger = logging.getLogger(__name__)


def _public_child_error(operation, payload):
    # The child marks deliberate, user-facing ValueError messages with
    # 'public': True. Internal RuntimeError/MuPDF text (or the legacy format
    # without the flag) is replaced by a fixed Chinese message.
    if payload.get('public') is True:
        return str(payload.get('error') or '')[:500]
    if operation == 'analysis':
        return '排版分析失败，请重试或手动排版'
    if operation == 'plt':
        return 'PLT 无法解析或生成投影画面，请重试或更换文件'
    if operation == 'tile':
        return 'PDF 画面渲染失败，请重试或更换文件'
    return 'PDF 无法解析，请重试或更换文件'

# The parse/tile/analysis child needs only the interpreter/locale plumbing and
# the PDF limit knobs it actually reads. Secrets (CONVERSION_SERVICE_TOKEN) and
# the identity/Redis addresses (WX_BACKEND_URL, PROJECTOR_REDIS_URL) must never
# reach a supervised child that only turns PDF bytes into pixels/layout.
_CHILD_ENV_EXACT = frozenset({
    'PATH', 'LANG',
    'PDF_MAX_TOTAL_PIXELS', 'PDF_MAX_DRAWINGS', 'PLT_MAX_DIMENSION_MM',
    'PLT_MAX_COMMANDS', 'PLT_MAX_POINTS', 'PLT_MAX_PATHS', 'PLT_MAX_TEXT_CHARS',
})
_CHILD_ENV_PREFIXES = ('LC_', 'PYTHON', 'PDF_LAYOUT_OPTIMIZER_')


def _child_environment():
    environment = {}
    for key, value in os.environ.items():
        if key in _CHILD_ENV_EXACT or key.startswith(_CHILD_ENV_PREFIXES):
            environment[key] = value
    return environment


def terminate(process):
    # The process group may still have descendants after its leader exited.
    # Always terminate the whole owned group before returning its reservation.
    try:
        os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:process.wait(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:pass
    try:
        os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()  # No quota/FD release before confirmed leader reap.


def run(store, lease, operation, source, parameters=None, deadline=None):
    policy = store.policy
    folder = store.lease_path(lease)
    folder.mkdir(mode=0o700, exist_ok=True)
    output = folder / ('result.png' if operation == 'tile' else 'result.json')
    spec = {'operation': operation, 'source': str(source), 'output': str(output),
            'parameters': parameters or {}, 'parent': os.getpid(),
            'module_root':str(Path(__file__).absolute().parent.parent),
            'limits': {'memory': policy.child_memory_bytes, 'cpu': policy.child_cpu_seconds,
                       'file': max(META_BOUND, PNG_BOUND, policy.derived_max_bytes if operation == 'plt' else 0)}}
    specification = folder / 'request.json'
    serialized = json.dumps(spec, allow_nan=False).encode()
    if len(serialized) > META_BOUND:
        raise ProjectionRejected('projection_metadata_budget', '投影任务参数超出预算', 503)
    specification.write_bytes(serialized)
    bootstrap = Path(__file__).absolute().parent.parent / 'projection_bootstrap.py'
    process = None
    try:
        store.claim(lease)
        process = subprocess.Popen([sys.executable, str(bootstrap), str(specification)],
                                   cwd=bootstrap.parent, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=_child_environment(),
                                   start_new_session=True)
        store.attach(lease, process.pid)
        timeout = deadline or (policy.tile_wall_seconds if operation == 'tile' else policy.parse_wall_seconds)
        ends = time.monotonic() + timeout
        while process.poll() is None:
            if not store.valid_lease(lease):
                raise ProjectionRejected('projection_result_stale', '投影会话或排版已变化，请重新加载最新画面', 409)
            if time.monotonic() >= ends:
                raise ProjectionRejected('projection_child_timeout', '投影处理超时，请重试，原画面保留', 503)
            time.sleep(POLL_SECONDS)
        process.wait()
        if not store.valid_lease(lease):
            raise ProjectionRejected('projection_result_stale', '投影会话或排版已变化，请重新加载最新画面', 409)
        error_file = folder / 'error.json'
        if error_file.exists():
            error = json.loads(error_file.read_bytes()[:65536])
            logger.warning('projection child %s failed: %s', operation, error.get('error'))
            raise ProjectionRejected('projection_invalid_plt' if operation == 'plt' else 'projection_invalid_pdf',
                                     _public_child_error(operation, error), 400)
        if process.returncode != 0 or not output.is_file():
            raise ProjectionRejected('projection_child_failed', '投影处理进程失败或资源不足，请重试，原画面保留', 503)
        maximum = PNG_BOUND if operation == 'tile' else META_BOUND
        if output.stat().st_size > maximum:
            raise ProjectionRejected('projection_result_budget', '投影结果超过预算，请重试', 503)
        result = output.read_bytes()
        return result if operation == 'tile' else json.loads(result)
    finally:
        if process is not None:
            terminate(process)
        # Only clear pid after wait() definitively confirms exit; reused PID must
        # not hold or incorrectly release this child's reservation.
        with store.transaction() as db:
            db.execute('UPDATE leases SET pid=NULL,identity=NULL WHERE id=?', (lease,))
