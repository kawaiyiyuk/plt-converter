import os
from pathlib import Path

from flask import Flask, jsonify
from redis.exceptions import RedisError

from .job_queue import conversion_queue, queue_load, redis_connection

from .routes import pdf_bp, plt_bp


def create_app():
    app = Flask(__name__)
    max_upload_mb = max(1, int(os.getenv('PLT_MAX_UPLOAD_MB', '20')))
    temp_folder = Path(os.getenv('PLT_TEMP_FOLDER', '/tmp/plt-converter'))
    temp_folder.mkdir(parents=True, exist_ok=True)
    app.config.update(
        MAX_CONTENT_LENGTH=max_upload_mb * 1024 * 1024,
        PLT_MAX_UPLOAD_MB=max_upload_mb,
        PLT_TEMP_FOLDER=str(temp_folder),
        PLT_TEMP_RETENTION_SECONDS=max(60, int(os.getenv('PLT_TEMP_RETENTION_SECONDS', '1800'))),
    )

    app.register_blueprint(plt_bp)
    app.register_blueprint(pdf_bp)

    @app.get('/health')
    def health():
        redis_ok = False
        worker_count = 0
        try:
            connection = redis_connection()
            redis_ok = bool(connection.ping())
            from rq import Worker
            worker_count = len(Worker.all(connection=connection))
        except Exception:
            pass
        healthy = redis_ok and worker_count > 0
        return jsonify({
            'status': 'healthy' if healthy else 'degraded',
            'service': 'plt-converter',
            'conversion_engine': 'python_vector_pdf',
            'redis': 'healthy' if redis_ok else 'unavailable',
            'workers': worker_count,
        }), 200 if healthy else 503

    @app.get('/health/redis')
    def health_redis():
        try:
            redis_connection().ping()
            return jsonify({'status': 'healthy'})
        except Exception:
            return jsonify({'status': 'unavailable'}), 503

    @app.get('/health/worker')
    def health_worker():
        try:
            connection = redis_connection()
            from rq import Worker
            workers = Worker.all(connection=connection)
            if workers:
                return jsonify({'status': 'healthy', 'workers': len(workers), 'queue': queue_load(connection)})
        except Exception:
            pass
        return jsonify({'status': 'unavailable', 'workers': 0}), 503

    @app.errorhandler(413)
    def upload_too_large(_error):
        return jsonify({
            'error': f'上传文件不能超过 {max_upload_mb}MB'
        }), 413

    @app.errorhandler(RedisError)
    def redis_unavailable(_error):
        return jsonify({
            'error': '任务服务暂时不可用，请稍后重试',
            'status': 'unavailable',
        }), 503

    return app
