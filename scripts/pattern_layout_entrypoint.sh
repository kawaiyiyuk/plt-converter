#!/bin/sh
set -eu
: "${PATTERN_LAYOUT_BIND_HOST:?explicit private bind host required}"
: "${PATTERN_LAYOUT_BIND_PORT:?explicit port required}"
: "${PATTERN_LAYOUT_SERVICE_KEY:?explicit internal bearer key required}"
exec gunicorn --workers 1 --threads 4 --bind "${PATTERN_LAYOUT_BIND_HOST}:${PATTERN_LAYOUT_BIND_PORT}" 'app.pattern_layout_app:create_app()'
