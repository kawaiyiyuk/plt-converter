"""Explicit local launcher; container deployment uses one Gunicorn HTTP worker."""
import os
from app.pattern_layout_app import create_app

if __name__ == '__main__':
    host=os.environ.get('PATTERN_LAYOUT_BIND_HOST')
    port=os.environ.get('PATTERN_LAYOUT_BIND_PORT')
    if not host or not port or not os.environ.get('PATTERN_LAYOUT_SERVICE_KEY'):
        raise ValueError('explicit bind host/port and internal service key required')
    create_app().run(host=host,port=int(port),threaded=True,use_reloader=False)
