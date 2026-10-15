"""Independent HTTP factory; no legacy blueprint, queue, billing operations or temp root."""
import os
from pathlib import Path
from flask import Flask, Request, g, jsonify
from redis.exceptions import RedisError
from werkzeug.exceptions import RequestEntityTooLarge
from .projection_store import MAX_BODY, MAX_SOURCE, Policy, ProjectionRejected, Store


class BudgetedRequest(Request):
    # Flask parses only one file and bounded fields; the stream goes directly to
    # the pre-reserved disk directory, never Werkzeug's anonymous OS tmp spool.
    max_form_parts = 4
    max_form_memory_size = 65536

    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        context = getattr(g, 'projection_upload', None)
        if context is None:
            raise ProjectionRejected('projection_multipart_unreserved', '投影上传尚未获得存储预算', 503)
        context['parts'] += 1
        if context['parts'] > 1:
            raise RequestEntityTooLarge('only one file is supported')
        from .projection_stream import LimitedFile
        stream = LimitedFile(context['path'], MAX_SOURCE)
        context['streams'].append(stream)
        return stream


def ensure_disk_root(root):
    # Linux tmpfs/ramfs are forbidden for original PDFs. Container volume
    # topology/read-only root and proxy buffering are enforced by the manifest.
    mountinfo=Path('/proc/self/mountinfo')
    if not mountinfo.exists():return
    directory=str(Path(root).resolve());best=('',None)
    for line in mountinfo.read_text().splitlines():
        fields=line.split();separator=fields.index('-')
        mountpoint=fields[4].replace('\\040',' ').replace('\\134','\\')
        if (directory==mountpoint or directory.startswith(mountpoint.rstrip('/')+'/')) and len(mountpoint)>=len(best[0]):
            best=(mountpoint,fields[separator+1])
    if best[1] in ('tmpfs','ramfs'):
        raise ValueError('PROJECTOR_ROOT must be disk storage, never tmpfs/ramfs')


def create_projection_app(configuration=None):
    app = Flask(__name__)
    app.request_class = BudgetedRequest
    configuration = {**os.environ, **(configuration or {})}
    enabled = str(configuration.get('PROJECTOR_ENABLED', '0'))
    if enabled not in ('0', '1'):
        raise ValueError('PROJECTOR_ENABLED must be 0 or 1')
    app.config.update(MAX_CONTENT_LENGTH=MAX_BODY, PLT_MAX_UPLOAD_BYTES=MAX_SOURCE,
                      PLT_MAX_UPLOAD_MB=20, PROJECTOR_ENABLED=(enabled == '1'))
    if enabled == '1':
        root = configuration.get('PROJECTOR_ROOT')
        if not root or not Path(root).is_absolute():
            raise ValueError('PROJECTOR_ROOT must be an explicit absolute disk-volume path')
        ensure_disk_root(root)
        policy = Policy(configuration)
        app.extensions['projection_store'] = Store(root, policy)
        app.config['PROJECTOR_ROOT'] = str(root)
        # Independently scoped Redis. Never consult old job_queue connection.
        app.config['PROJECTOR_REDIS_URL'] = configuration.get('PROJECTOR_REDIS_URL')
        if not app.config['PROJECTOR_REDIS_URL']:
            raise ValueError('PROJECTOR_REDIS_URL must explicitly select independent Redis')
    from .projection import projection_bp
    app.register_blueprint(projection_bp)

    @app.before_request
    def disabled():
        from flask import request
        if request.path.startswith('/api/v1/pdf/projection') and not app.config['PROJECTOR_ENABLED']:
            return jsonify(error='投影服务尚未启用', error_code='projection_disabled'), 503

    @app.errorhandler(ProjectionRejected)
    def rejected(error):
        retry_after = error.retry_after if error.status in (429,503) else 0
        response = jsonify(error=str(error), error_code=error.code, retry_after=retry_after)
        response.status_code = error.status
        if error.retry_after and error.status in (429, 503):
            response.headers['Retry-After'] = str(error.retry_after)
        return response

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error='上传文件不能超过20MB，且每次只支持一个 PDF、PLT 或 HPGL 文件', error_code='projection_upload_too_large'), 413

    @app.errorhandler(OSError)
    @app.errorhandler(__import__('sqlite3').Error)
    def storage_error(error):
        return jsonify(error='投影存储暂时不可用，请重试',error_code='projection_storage_unavailable'),503

    @app.errorhandler(RedisError)
    def projection_redis_error(error):
        # Any uncaught Redis/RQ failure must degrade to a retryable 503, never
        # a 500 that leaks internal error text to the client.
        response = jsonify(error='投影排版服务暂时不可用，原分页保留',
                           error_code='projection_worker_unavailable', retry_after=1)
        response.status_code = 503
        response.headers['Retry-After'] = '1'
        return response

    @app.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/health/live')
    def live():
        return jsonify(status='alive',service='pdf-projector')

    @app.get('/health')
    @app.get('/health/readiness')
    def readiness():
        if not app.config['PROJECTOR_ENABLED']:
            return jsonify(status='disabled',service='pdf-projector'),503
        from .projection_layout import has_live_projection_worker, projection_connection
        try:
            with app.extensions['projection_store'].transaction() as db:
                db.execute('SELECT 1').fetchone()
            redis_ok = bool(projection_connection().ping())
            worker_ok = has_live_projection_worker()
        except Exception:
            redis_ok = worker_ok = False
        return jsonify(status='healthy' if redis_ok and worker_ok else 'degraded',
                       service='pdf-projector',redis=redis_ok,worker=worker_ok),200 if redis_ok and worker_ok else 503
    return app
