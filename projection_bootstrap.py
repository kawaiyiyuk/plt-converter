"""Stdlib-only child entry: enforce OS limits before importing the app/native PDF code."""
import ctypes
import json
import os
from pathlib import Path
import resource
import signal
import sys


def main():
    spec = json.loads(Path(sys.argv[1]).read_text())
    limits = spec['limits']
    parent = int(spec['parent'])
    if sys.platform.startswith('linux'):
        # If the supervisor exits, Linux kills the child even if HTTP vanished.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'PR_SET_PDEATHSIG')
    if os.getppid() != parent:
        return  # Parent died before prctl; do not start any PDF work.
    resource.setrlimit(resource.RLIMIT_AS, (limits['memory'], limits['memory']))
    resource.setrlimit(resource.RLIMIT_CPU, (limits['cpu'], limits['cpu']))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits['file'], limits['file']))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from_start = None
    try:
        stat = Path(f'/proc/{os.getpid()}/stat').read_text()
        from_start = stat[stat.rfind(')')+2:].split()[19]
    except (OSError, IndexError):
        pass
    (Path(sys.argv[1]).parent/'child.json').write_text(json.dumps({'pid':os.getpid(),'identity':from_start}))
    os.environ['TMPDIR'] = str(Path(sys.argv[1]).parent)
    sys.path.insert(0,spec['module_root'])
    from app.projection_child import execute
    execute(spec)

if __name__ == '__main__':
    main()
