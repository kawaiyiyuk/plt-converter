import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from redis import Redis
from rq import Queue
from rq.command import send_stop_job_command
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Callback, Job
from rq.registry import StartedJobRegistry


TERMINAL_STATUSES = {'done', 'failed', 'cancelled', 'expired'}
ACTIVE_STATUSES = {'queued', 'processing', 'cancelling'}
JOB_OUTPUT_VERSIONS = {
    'plt_to_pdf': '3-page-clipped',
    'pdf_to_plt': '2-visible-clipped',
    'pdf_preview': '1',
}


def redis_connection(blocking=False):
    return Redis.from_url(
        os.getenv('REDIS_URL', 'redis://redis:6379/0'),
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=None if blocking else 3,
        health_check_interval=30,
    )


def conversion_queue(connection=None):
    connection = connection or redis_connection()
    return Queue(
        os.getenv('PLT_QUEUE_NAME', 'conversions'),
        connection=connection,
        default_timeout=max(10, int(os.getenv('PLT_JOB_TIMEOUT_SECONDS', '90'))),
    )


def job_key(job_id):
    return f'plt-converter:job:{job_id}'


def metric_key():
    return 'plt-converter:metrics'


def acquire_job_lock(job_id, connection):
    lock = connection.lock(
        f'plt-converter:job-lock:{job_id}',
        timeout=30,
        blocking_timeout=5,
    )
    if not lock.acquire(blocking=True):
        raise QueueRejected('任务状态正在更新，请稍后重试', retry_after=2)
    return lock


def load_job(job_id, connection=None):
    connection = connection or redis_connection()
    raw = connection.get(job_key(job_id))
    return json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw) if raw else None


def save_job(record, connection=None):
    connection = connection or redis_connection()
    ttl = job_record_ttl(record)
    connection.setex(job_key(record['job_id']), ttl, json.dumps(record, ensure_ascii=False))
    if record.get('status') in ACTIVE_STATUSES and record.get('user_key'):
        connection.expire(f"plt-converter:user-jobs:{record['user_key']}", ttl)
    return record


def job_record_ttl(record=None):
    retention = max(60, int(os.getenv('PLT_JOB_RETENTION_SECONDS', '1800')))
    if not record or record.get('status') not in ACTIVE_STATUSES:
        return retention
    queue_capacity = max(1, int(os.getenv('PLT_QUEUE_MAX_PENDING', '20')))
    timeout = max(10, int(os.getenv('PLT_JOB_TIMEOUT_SECONDS', '90')))
    maximum_attempts = 2
    return max(retention, queue_capacity * timeout * maximum_attempts + 300)


def update_job(job_id, connection=None, **changes):
    connection = connection or redis_connection()
    record = load_job(job_id, connection)
    if not record:
        return None
    record.update(changes)
    record['updated_at'] = time.time()
    save_job(record, connection)
    return record


def queue_position(job_id, connection=None):
    connection = connection or redis_connection()
    queue = conversion_queue(connection)
    record = load_job(job_id, connection)
    rq_job_id = (record or {}).get('rq_job_id') or job_id
    try:
        return queue.job_ids.index(rq_job_id) + 1
    except ValueError:
        return 0


def queue_load(connection=None):
    connection = connection or redis_connection()
    queue = conversion_queue(connection)
    started = StartedJobRegistry(queue.name, connection=connection)
    return {
        'queued': queue.count,
        'processing': started.count,
        'capacity': max(1, int(os.getenv('PLT_QUEUE_MAX_PENDING', '20'))),
    }


def enforce_rate_limit(user_key, connection=None, scope='jobs', limit_env='PLT_RATE_LIMIT_PER_MINUTE'):
    connection = connection or redis_connection()
    limit = max(1, int(os.getenv(limit_env, '3')))
    window = int(time.time() // 60)
    key = f'plt-converter:rate:{scope}:{user_key}:{window}'
    count = connection.incr(key)
    if count == 1:
        connection.expire(key, 70)
    if count > limit:
        raise QueueRejected('提交过于频繁，请稍后再试', retry_after=60)


def enforce_user_capacity(user_key, connection=None):
    connection = connection or redis_connection()
    maximum = max(1, int(os.getenv('PLT_USER_MAX_ACTIVE_JOBS', '2')))
    key = f'plt-converter:user-jobs:{user_key}'
    active = []
    for raw_job_id in connection.smembers(key):
        job_id = raw_job_id.decode('utf-8') if isinstance(raw_job_id, bytes) else raw_job_id
        record = load_job(job_id, connection)
        if record and record.get('status') in ACTIVE_STATUSES:
            active.append(job_id)
        else:
            connection.srem(key, job_id)
    if len(active) >= maximum:
        raise QueueRejected('你已有转换任务正在处理，请等待完成后再试', retry_after=5)


def completed_result_available(record):
    if record.get('status') != 'done':
        return False
    if record.get('job_type') == 'pdf_preview':
        pages = (record.get('result') or {}).get('pages') or []
        preview_root = Path(record.get('input_path', '')).parent / 'previews'
        return bool(pages) and all(
            (preview_root / f"{record['job_id']}-{page['index']}.png").exists()
            for page in pages
        )
    result_path = record.get('result_path')
    return bool(result_path and Path(result_path).exists())


def submit_job(job_type, source, filename, options, user_key, connection=None):
    connection = connection or redis_connection()
    cleanup_expired_job_files(connection)
    output_version = JOB_OUTPUT_VERSIONS.get(job_type, '1')
    fingerprint = hashlib.sha256(
        job_type.encode('utf-8')
        + b'\0'
        + output_version.encode('utf-8')
        + b'\0'
        + source
        + b'\0'
        + json.dumps(options, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()
    fingerprint_key = f'plt-converter:fingerprint:{user_key}:{fingerprint}'
    lock = connection.lock('plt-converter:submit-lock', timeout=15, blocking_timeout=5)
    if not lock.acquire(blocking=True):
        raise QueueRejected('服务器正忙，请稍后重试', retry_after=2)
    try:
        existing_id = connection.get(fingerprint_key)
        if isinstance(existing_id, bytes):
            existing_id = existing_id.decode('utf-8')
        existing = load_job(existing_id, connection) if existing_id else None
        if existing and existing.get('status') in ACTIVE_STATUSES | {'done'}:
            if existing.get('status') in ACTIVE_STATUSES or completed_result_available(existing):
                existing['deduplicated'] = True
                connection.hincrby(metric_key(), 'deduplicated', 1)
                return existing

        enforce_rate_limit(user_key, connection)
        enforce_user_capacity(user_key, connection)
        load = queue_load(connection)
        if load['queued'] + load['processing'] >= load['capacity']:
            connection.hincrby(metric_key(), 'rejected_queue_full', 1)
            raise QueueRejected('服务器任务已排满，请稍后重试', retry_after=10)

        job_id = uuid4().hex
        root = Path(os.getenv('PLT_TEMP_FOLDER', '/tmp/plt-converter')) / 'jobs' / job_id
        root.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix.lower() or ('.pdf' if job_type.startswith('pdf_') else '.plt')
        input_path = root / f'input{suffix}'
        input_path.write_bytes(source)
        now = time.time()
        record = {
            'job_id': job_id,
            'job_type': job_type,
            'output_version': output_version,
            'user_key': user_key,
            'status': 'queued',
            'progress': 0,
            'filename': filename,
            'options': options,
            'input_path': str(input_path),
            'result_path': None,
            'result': None,
            'error': None,
            'created_at': now,
            'updated_at': now,
            'started_at': None,
            'finished_at': None,
            'deduplicated': False,
            'retry_count': 0,
            'rq_job_id': f'{job_id}:0',
        }
        save_job(record, connection)
        retention = max(60, int(os.getenv('PLT_JOB_RETENTION_SECONDS', '1800')))
        active_ttl = job_record_ttl(record)
        connection.setex(fingerprint_key, active_ttl, job_id)
        user_jobs_key = f'plt-converter:user-jobs:{user_key}'
        connection.sadd(user_jobs_key, job_id)
        connection.expire(user_jobs_key, active_ttl)

        queue = conversion_queue(connection)
        from .tasks import mark_job_failed, mark_job_stopped

        try:
            queue.enqueue_call(
                'app.tasks.execute_job',
                args=(job_id,),
                job_id=record['rq_job_id'],
                timeout=max(10, int(os.getenv('PLT_JOB_TIMEOUT_SECONDS', '90'))),
                result_ttl=retention,
                failure_ttl=retention,
                on_failure=Callback(mark_job_failed),
                on_stopped=Callback(mark_job_stopped),
            )
        except Exception:
            update_job(job_id, connection, status='failed', error='任务入队失败', finished_at=time.time())
            connection.srem(user_jobs_key, job_id)
            raise
        connection.hincrby(metric_key(), 'submitted', 1)
        return record
    finally:
        lock.release()


def cancel_job(job_id, user_key=None, connection=None):
    connection = connection or redis_connection()
    lock = acquire_job_lock(job_id, connection)
    try:
        record = load_job(job_id, connection)
        if not record:
            return None
        if user_key and record.get('user_key') != user_key:
            raise PermissionError('无权取消该任务')
        if record.get('status') in TERMINAL_STATUSES:
            return record
        rq_job_id = record.get('rq_job_id') or job_id
        try:
            rq_job = Job.fetch(rq_job_id, connection=connection)
        except NoSuchJobError:
            record = update_job(job_id, connection, status='cancelled', finished_at=time.time(), progress=0)
            release_user_job(record, connection)
            return record
        if record.get('status') == 'queued':
            rq_job.cancel()
            conversion_queue(connection).remove(rq_job_id)
            record = update_job(job_id, connection, status='cancelled', finished_at=time.time(), progress=0)
            release_user_job(record, connection)
            connection.hincrby(metric_key(), 'cancelled', 1)
            return record
        previous_status = record.get('status')
        previous_progress = record.get('progress', 0)
        record = update_job(
            job_id,
            connection,
            status='cancelling',
            cancel_requested=True,
            progress=0,
        )
        try:
            send_stop_job_command(connection, rq_job_id)
        except InvalidJobOperation:
            return update_job(
                job_id,
                connection,
                status=previous_status,
                cancel_requested=False,
                progress=previous_progress,
            )
        except Exception:
            update_job(
                job_id,
                connection,
                status=previous_status,
                cancel_requested=False,
                progress=previous_progress,
            )
            raise
        return record
    finally:
        lock.release()


def record_metric(name, amount=1, connection=None):
    connection = connection or redis_connection()
    connection.hincrby(metric_key(), name, amount)


def release_user_job(record, connection=None):
    if not record or not record.get('user_key') or not record.get('job_id'):
        return
    connection = connection or redis_connection()
    connection.srem(f"plt-converter:user-jobs:{record['user_key']}", record['job_id'])


def cleanup_expired_job_files(connection=None):
    retention = max(60, int(os.getenv('PLT_JOB_RETENTION_SECONDS', '1800')))
    jobs_root = Path(os.getenv('PLT_TEMP_FOLDER', '/tmp/plt-converter')) / 'jobs'
    if not jobs_root.exists():
        return
    connection = connection or redis_connection()
    cutoff = time.time() - retention
    for job_folder in jobs_root.iterdir():
        try:
            if not job_folder.is_dir() or job_folder.stat().st_mtime >= cutoff:
                continue
            record = load_job(job_folder.name, connection)
            if record and record.get('status') in ACTIVE_STATUSES:
                continue
            if record:
                retained_from = record.get('finished_at') or record.get('updated_at')
                if retained_from and retained_from >= cutoff:
                    continue
            if job_folder.is_dir():
                import shutil
                shutil.rmtree(job_folder, ignore_errors=True)
        except OSError:
            continue


class QueueRejected(Exception):
    def __init__(self, message, retry_after=10):
        super().__init__(message)
        self.retry_after = retry_after
