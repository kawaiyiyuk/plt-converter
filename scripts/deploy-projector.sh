#!/usr/bin/env bash
# Local-only executor. Production/remote endpoints are deliberately unsupported.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode= envfile= target= context= socket=
while (($#)); do
  case "$1" in
    --dry-run|--offline|--apply-local|--prepare-base-local) mode="$1"; shift ;;
    --env-file|--target-scope|--prepare-context|--docker-socket)
      [[ $# -ge 2 ]] || { echo "Missing option value" >&2; exit 2; }
      case "$1" in
        --env-file) envfile="$2";; --target-scope) target="$2";; --prepare-context) context="$2";; --docker-socket) socket="$2";;
      esac; shift 2 ;;
    *) echo "Unknown or unsupported option: $1" >&2; exit 2;;
  esac
done
[[ -n "$mode" && "$target" == local-review && -f "$envfile" ]] || {
  echo "Require --dry-run/--offline/--apply-local/--prepare-base-local, --target-scope local-review, --env-file PATH" >&2; exit 2;
}
# Validate allowlist independently of daemon, network, SSH and remote Docker contexts.
python3 - "$ROOT" "$envfile" "$context" "$mode" <<'PYTHON'
from pathlib import Path
import ast, ipaddress, os, re, shutil, socket, sys
from urllib.parse import urlparse
root, envpath, context = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]


def parse_env_file(path, source, allowed=None):
    # Shared line parser for --env-file and the policy env_file: blank lines and
    # "#" comments are skipped, an "export " prefix is stripped, and keys must be
    # shell-safe. Values may not contain characters Compose would expand, quote
    # or comment-truncate. When "allowed" is given, any other key is rejected.
    parsed = {}
    for raw in path.read_text().splitlines():
        text = raw.strip()
        if not text or text.startswith('#'):
            continue
        if text.startswith('export '):
            text = text[len('export '):].lstrip()
        if '=' not in text:
            raise SystemExit('Invalid ' + source + ' line without "=": ' + raw.strip())
        key, value = text.split('=', 1)
        key = key.strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise SystemExit('Invalid key in ' + source + ' (must match ^[A-Za-z_][A-Za-z0-9_]*$): ' + repr(key))
        if allowed is not None and key not in allowed:
            raise SystemExit('Key ' + repr(key) + ' is not allowed in policy file')
        value = value.strip()
        # Docker Compose strips an unquoted " #" comment and expands ${VAR}/$VAR,
        # so a value containing these characters would reach the daemon as
        # something other than the reviewed literal. Reject them outright.
        if re.search(r'[\s#$"\'`\\]', value):
            raise SystemExit(
                'unsupported character in ' + source + ' value for ' + key
                + ': whitespace, "#", "$", quotes and backslashes are not allowed '
                + '(Compose would expand, quote-strip or comment-truncate them)')
        if key in parsed:
            raise SystemExit('Duplicate key in ' + source + ': ' + key + ' (each key must appear at most once)')
        parsed[key] = value
    return parsed


def projection_fields(path):
    # Read FIELDS statically so the allowlist tracks app/projection_store.py
    # without importing the app package (which pulls in side effects and deps).
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'FIELDS' for t in node.targets):
            value = node.value
            if isinstance(value, ast.Call):
                value = value.func.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value.split()
            raise SystemExit('Cannot statically read FIELDS from app/projection_store.py')
    raise SystemExit('FIELDS assignment not found in app/projection_store.py')


env = parse_env_file(envpath, '--env-file')
# compose.projector.yaml injects ${PROJECTOR_POLICY_FILE} verbatim as an env_file
# for the API and Worker. A stray proxy variable there (http_proxy/HTTPS_PROXY/...)
# would route urllib identity requests, including the user Bearer, past the WX
# host allowlist, so the policy file may only contain the reviewed policy keys.
allowed_policy_keys = {'PROJECTOR_' + field for field in projection_fields(root / 'app/projection_store.py')}
allowed_policy_keys.update({'PROJECTOR_DERIVED_MAX_BYTES', 'PROJECTOR_OWNER_DERIVED_BYTES'})
policy_path = env.get('PROJECTOR_POLICY_FILE')
if not policy_path:
    raise SystemExit('PROJECTOR_POLICY_FILE is required in --env-file')
if not Path(policy_path).is_file():
    raise SystemExit('PROJECTOR_POLICY_FILE does not exist: ' + policy_path)
parse_env_file(Path(policy_path), 'policy file', allowed_policy_keys)
# Compose interpolation prefers the shell environment over --env-file, so any
# PROJECTOR_* variable already exported by the caller would silently bypass the
# validations below, including ones the env file omits and compose defaults
# (e.g. PROJECTOR_BIND_ADDRESS). Reject every PROJECTOR_* variable that the
# compose file interpolates or the env file defines.
interpolated = set(re.findall(r'\$\{(PROJECTOR_[A-Z0-9_]+)', (root / 'compose.projector.yaml').read_text()))
leaked = sorted(key for key in interpolated | set(env) if key.startswith('PROJECTOR_') and key in os.environ)
if leaked:
    raise SystemExit('Shell environment already defines PROJECTOR_* variable(s) that would override --env-file: ' + ', '.join(leaked) + '; unset them and rerun')
rfc1918 = (ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'), ipaddress.ip_network('192.168.0.0/16'))
loopback = ipaddress.ip_network('127.0.0.0/8')
wx = env.get('PROJECTOR_WX_BACKEND_URL', '')
# urllib decodes %2E and IDNA folds the fullwidth dot into ".", so a decoded or
# homoglyph host could still reach the online backend. Require a plain ASCII URL.
if not wx.isascii() or '%' in wx:
    raise SystemExit('PROJECTOR_WX_BACKEND_URL must be plain ASCII without "%" percent-encoding')
parsed = urlparse(wx)
if parsed.scheme not in ('http', 'https') or not parsed.hostname:
    raise SystemExit('PROJECTOR_WX_BACKEND_URL must be an http/https URL with an explicit host')
host = parsed.hostname.lower().rstrip('.')
if host == 'fengrenjiyi.com' or host.endswith('.fengrenjiyi.com'):
    raise SystemExit('PROJECTOR_WX_BACKEND_URL must not point at fengrenjiyi.com or any subdomain (local verification forbids the online identity backend)')
# Local verification only talks to a locally reachable identity backend, so the
# host must be an explicit allowlist entry, not any public name/IP or alias.
try:
    host_addr = ipaddress.ip_address(host)
except ValueError:
    host_addr = None
private_ipv4 = isinstance(host_addr, ipaddress.IPv4Address) and (host_addr in loopback or any(host_addr in network for network in rfc1918))
single_label = bool(re.fullmatch(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?', host))
if single_label:
    try:
        socket.inet_aton(host)
    except OSError:
        pass  # Not an IPv4 literal; safe to treat as a compose service name.
    else:
        # inet_aton also accepts numeric/hex/partial forms the OS resolves as
        # IPv4 (e.g. 134744072 or 0x08080808 -> 8.8.8.8), so such a single-label
        # value may only pass when it is a standard dotted quad already admitted
        # as loopback/RFC1918; otherwise it is rejected.
        single_label = bool(re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', host)) and private_ipv4
if not (host in ('localhost', 'host.docker.internal') or single_label or private_ipv4):
    raise SystemExit('PROJECTOR_WX_BACKEND_URL host must be localhost, host.docker.internal, a single-label service name, or a loopback/RFC1918 private IPv4 address')
bind = env.get('PROJECTOR_BIND_ADDRESS', '127.0.0.1')
try:
    bind_addr = ipaddress.ip_address(bind)
except ValueError:
    raise SystemExit('PROJECTOR_BIND_ADDRESS must be a single IPv4 address')
if not isinstance(bind_addr, ipaddress.IPv4Address) or (bind != '127.0.0.1' and not any(bind_addr in network for network in rfc1918)):
    raise SystemExit('PROJECTOR_BIND_ADDRESS must be 127.0.0.1 or an RFC1918 private IPv4 address (0.0.0.0 and public addresses are forbidden)')
approved = {'sha256:60ac62f0cd72b32f5914385cf7ab299f1987b2dc8960b98e34574bbdd1557fc3', 'sha256:7801cc5c82877790e857a68151e79ee477105fb5418345e91aaaa9fd1b5c2a96'}
if env.get('PROJECTOR_BASE_ID') not in approved:
    raise SystemExit('BASE_ID must equal approved immutable local image ID; registry supply chain needs separate review')
if not re.fullmatch(r'localhost:1/fengrenjiyi-projector-base:review-[a-zA-Z0-9-]+', env.get('PROJECTOR_BASE_IMAGE', '')):
    raise SystemExit('BASE_IMAGE must be a unique task-only localhost:1 alias; bare image ID cannot be a FROM reference')
if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/:-]*', env.get('PROJECTOR_IMAGE', '')):
    raise SystemExit('Explicit candidate image required')
if env['PROJECTOR_IMAGE'].startswith(('plt-converter-', 'redis:')):
    raise SystemExit('Candidate must not reuse legacy image tags')
if env.get('PROJECTOR_ENABLED', '0') not in ('0', '1'):
    raise SystemExit('PROJECTOR_ENABLED must be explicit 0 or 1')
if sys.argv[4] != '--apply-local' and env.get('PROJECTOR_ENABLED', '0') != '0':
    raise SystemExit('Static review examples must keep the release gate closed')
# With the release gate closed the layout worker never starts, so a later
# --wait would always time out: --apply-local needs an explicit enabled=1.
if sys.argv[4] == '--apply-local' and env.get('PROJECTOR_ENABLED', '0') != '1':
    print('--apply-local requires PROJECTOR_ENABLED=1; with the gate closed the layout worker never starts and --wait would fail', file=sys.stderr)
    raise SystemExit(2)
if sys.argv[4] == '--prepare-base-local' and not re.fullmatch(r'localhost:1/fengrenjiyi-projector-base:review-[a-f0-9]{12}-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', env['PROJECTOR_BASE_IMAGE']):
    raise SystemExit('Preparing base requires a fresh UUID task alias')
files = ['Dockerfile.projection', 'app/__init__.py', 'app/projection.py', 'app/projection_layout.py', 'app/projection_page_filter.py', 'app/projection_numbered_layout.py', 'app/projection_pdf_runtime.py', 'app/projection_app.py', 'app/projection_store.py', 'app/projection_runtime.py', 'app/projection_child.py', 'app/projection_stream.py', 'app/projection_proxy.py', 'run_projection.py', 'projection_bootstrap.py', 'docker/projector-data/.keep', 'app/services/plt_parser.py', 'app/services/pdf_renderer.py', 'app/services/number_tokens.py', 'app/services/pdf_metadata.py', 'app/services/pdf_layout_optimizer.py', 'app/services/pdf_to_plt.py']
if context:
    raw = Path(context)
    if '..' in raw.parts:
        raise SystemExit('Context path must not contain .. (use a literal /private/tmp/projector-build.* path)')
    dest = raw.resolve()
    if dest.parent != Path('/private/tmp') or not dest.name.startswith('projector-build.') or dest.exists():
        raise SystemExit('Context must be a new /private/tmp/projector-build.* directory')
    missing = [name for name in files if not root.joinpath(name).is_file()]
    if missing: raise SystemExit('Backend source not ready: ' + ', '.join(missing))
    for name in files:
        out = dest / name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, out)
    print('Prepared task-only context:', dest)
print('Approved base ID:', env['PROJECTOR_BASE_ID'])
print('Task-only FROM alias:', env['PROJECTOR_BASE_IMAGE'])
print('Shared candidate:', env['PROJECTOR_IMAGE'])
print('Source COPY allowlist:', ', '.join(files))
PYTHON
printf '%s\n' 'Future authorized local operations (printed only):'
printf 'docker compose -p fengrenjiyi-projector --env-file %q -f %q config\n' "$envfile" "$ROOT/compose.projector.yaml"
printf '%s\n' 'Use local default Docker driver, verified task-only alias and --pull=false; build only projector-api using compose.projector.build.yaml + clean context; require base/candidate/Redis images available locally, never fallback pull.'
printf '%s\n' 'Start only: projector-redis projector-layout-worker projector-api; wait readiness and worker live-only-queue; inspect legacy health separately under its own authorization.'
if [[ "$mode" == --prepare-base-local ]]; then
  [[ "$socket" == /* && "$socket" != *$'\n'* && -S "$socket" ]] || {
    echo 'Explicit local absolute Unix Docker socket required' >&2; exit 2;
  }
  # Parse data only; never source shell env files or use the current Docker context.
  base_id="$(python3 - "$envfile" <<'PYBASE'
import sys
for line in open(sys.argv[1]):
    if line.startswith('PROJECTOR_BASE_ID='): print(line.strip().split('=',1)[1])
PYBASE
)"
  base_alias="$(python3 - "$envfile" <<'PYALIAS'
import sys
for line in open(sys.argv[1]):
    if line.startswith('PROJECTOR_BASE_IMAGE='): print(line.strip().split('=',1)[1])
PYALIAS
)"
  docker_local=(docker --host "unix://$socket")
  actual_id="$("${docker_local[@]}" image inspect "$base_id" --format '{{.Id}}')"
  [[ "$actual_id" == "$base_id" ]] || { echo 'Approved base ID mismatch' >&2; exit 2; }
  if existing="$("${docker_local[@]}" image inspect "$base_alias" --format '{{.Id}}' 2>/dev/null)"; then
    [[ "$existing" == "$base_id" ]] || { echo 'Refusing to overwrite mismatched task alias' >&2; exit 2; }
  else
    "${docker_local[@]}" image tag "$base_id" "$base_alias"
  fi
  [[ "$("${docker_local[@]}" image inspect "$base_alias" --format '{{.Id}}')" == "$base_id" ]] || exit 2
  printf '%s\n' 'Verified task-only local base alias; no build/start/service change performed.'
elif [[ "$mode" == --apply-local ]]; then
  [[ "$socket" == /* && "$socket" != *$'\n'* && -S "$socket" ]] || {
    echo 'Explicit local absolute Unix Docker socket required' >&2; exit 2;
  }
  # With the release gate closed the layout worker never starts, so --wait
  # would always time out. The Python validation block above already requires
  # PROJECTOR_ENABLED=1 for --apply-local from the same parsed env file.
  # --host overrides DOCKER_HOST and the active context: no TCP/SSH endpoint.
  compose=(docker --host "unix://$socket" compose -p fengrenjiyi-projector --env-file "$envfile" -f "$ROOT/compose.projector.yaml")
  "${compose[@]}" config --quiet
  # pull_policy never: candidate and reviewed Redis must already exist locally.
  "${compose[@]}" up -d --force-recreate --no-build --pull never --wait --wait-timeout 120 projector-redis projector-layout-worker projector-api
  "${compose[@]}" exec -T projector-layout-worker python -c 'from app.projection_layout import has_live_projection_worker; raise SystemExit(0 if has_live_projection_worker() else 1)'
  "${compose[@]}" exec -T projector-api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1/health/readiness', timeout=3)"
  printf '%s\n' 'Local new-stack health verified; legacy health and gateway remain separate evidence.'
else
  printf '%s\n' 'No deploy, rollback, drain, gateway update, container creation or production connection performed.'
fi
