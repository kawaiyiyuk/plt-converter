#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Explicit --start-local operates only the specified local Unix Docker endpoint.
args=()
for arg in "$@"; do
  if [[ "$arg" == --start-local ]]; then args+=(--apply-local); else args+=("$arg"); fi
done
exec bash "$ROOT/scripts/deploy-projector.sh" --target-scope local-review "${args[@]}"
