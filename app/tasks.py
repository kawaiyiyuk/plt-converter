import os
import shutil
import time
from pathlib import Path
from rq.job import Callback
from rq.timeouts import JobTimeoutException

from .job_queue import (
    acquire_job_lock,
    conversion_queue,
    load_job,
    record_metric,
    redis_connection,
    release_user_job,
    TERMINAL_STATUSES,
    update_job,
)
from .billing import release_conversion
from .services.pdf_renderer import render_pdf
from .services.pdf_to_plt import convert_pdf_to_plt, inspect_pdf
from .services.plt_parser import parse_plt


def execute_job(job_id):
    connection = redis_connection()
    confirmation_deadline = time.monotonic() + max(
        2,
        int(os.getenv('CONVERSION_BILLING_CONFIRM_TIMEOUT_SECONDS', '12')),
    )
    while True:
        pending = load_job(job_id, connection)
        if not pending or pending.get('status') != 'billing_pending':
            break
        if time.monotonic() >= confirmation_deadline:
            lock = acquire_job_lock(job_id, connection)
            try:
                latest = load_job(job_id, connection)
                if latest and latest.get('status') == 'billing_pending':
                    update_job(
                        job_id,
                        connection,
                        status='failed',
                        error='转换计费确认超时',
                        finished_at=time.time(),
                    )
                    release_user_job(load_job(job_id, connection), connection)
                    user_key = latest.get('user_key', '')
                    if user_key.startswith('user:') and latest.get('billing_request_id'):
                        release_conversion(
                            int(user_key.split(':', 1)[1]),
                            latest['billing_request_id'],
                            job_id,
                        )
            finally:
                lock.release()
            return None
        time.sleep(0.05)

    lock = acquire_job_lock(job_id, connection)
    try:
        record = load_job(job_id, connection)
        if not record or record.get('status') in TERMINAL_STATUSES:
            release_user_job(record, connection)
            return None
        if record.get('cancel_requested'):
            update_job(job_id, connection, status='cancelled', finished_at=time.time(), progress=0)
            release_user_job(load_job(job_id, connection), connection)
            return None
        started = time.time()
        update_job(job_id, connection, status='processing', progress=5, started_at=started)
    finally:
        lock.release()
    try:
        result = _execute(record, connection)
        lock = acquire_job_lock(job_id, connection)
        try:
            latest = load_job(job_id, connection)
            if latest and latest.get('cancel_requested'):
                update_job(job_id, connection, status='cancelled', finished_at=time.time(), progress=0)
                release_user_job(load_job(job_id, connection), connection)
                return None
            finished = time.time()
            update_job(
                job_id,
                connection,
                status='done',
                progress=100,
                result=result,
                result_path=result.get('result_path'),
                finished_at=finished,
                duration_ms=round((finished - started) * 1000),
            )
        finally:
            lock.release()
        release_user_job(load_job(job_id, connection), connection)
        record_metric('completed', connection=connection)
        connection.hincrby('plt-converter:metrics', 'duration_ms_total', round((finished - started) * 1000))
        return result
    except (OSError, TimeoutError, JobTimeoutException) as error:
        lock = acquire_job_lock(job_id, connection)
        try:
            latest = load_job(job_id, connection)
            if latest and (latest.get('cancel_requested') or latest.get('status') == 'cancelling'):
                update_job(job_id, connection, status='cancelled', progress=0, finished_at=time.time())
                release_user_job(load_job(job_id, connection), connection)
                record_metric('cancelled', connection=connection)
                return None
            retries = int((latest or {}).get('retry_count', 0))
            if retries < 1:
                enqueue_retry(job_id, connection, retries)
                return None
            finished = time.time()
            update_job(
                job_id,
                connection,
                status='failed',
                progress=0,
                error=str(error) or error.__class__.__name__,
                finished_at=finished,
                duration_ms=round((finished - started) * 1000),
            )
            release_user_job(load_job(job_id, connection), connection)
            record_metric('failed', connection=connection)
        finally:
            lock.release()
        raise
    except Exception as error:
        lock = acquire_job_lock(job_id, connection)
        try:
            latest = load_job(job_id, connection)
            cancelled = latest and (
                latest.get('cancel_requested') or latest.get('status') == 'cancelling'
            )
            finished = time.time()
            update_job(
                job_id,
                connection,
                status='cancelled' if cancelled else 'failed',
                progress=0,
                error=None if cancelled else (str(error) or error.__class__.__name__),
                finished_at=finished,
                duration_ms=round((finished - started) * 1000),
            )
            release_user_job(load_job(job_id, connection), connection)
            record_metric('cancelled' if cancelled else 'failed', connection=connection)
        finally:
            lock.release()
        raise


def mark_job_failed(job, connection, type_, value, traceback):
    task_id = job.args[0] if job.args else job.id.split(':', 1)[0]
    lock = acquire_job_lock(task_id, connection)
    try:
        record = load_job(task_id, connection)
        if not record:
            return
        if record.get('status') in {'done', 'failed', 'cancelled'}:
            release_user_job(record, connection)
            return
        if record.get('cancel_requested') or record.get('status') == 'cancelling':
            update_job(task_id, connection, status='cancelled', progress=0, finished_at=time.time())
            release_user_job(load_job(task_id, connection), connection)
            record_metric('cancelled', connection=connection)
            return
        transient = isinstance(type_, type) and issubclass(type_, (OSError, TimeoutError, JobTimeoutException))
        retries = int(record.get('retry_count', 0))
        if transient and retries < 1:
            enqueue_retry(task_id, connection, retries)
            return
        update_job(
            task_id,
            connection,
            status='failed',
            progress=0,
            error=f'{type_.__name__}: {value}',
            finished_at=time.time(),
        )
        release_user_job(load_job(task_id, connection), connection)
        record_metric('failed', connection=connection)
    finally:
        lock.release()


def enqueue_retry(task_id, connection, retries):
    rq_job_id = f'{task_id}:{retries + 1}'
    update_job(
        task_id,
        connection,
        status='queued',
        progress=0,
        retry_count=retries + 1,
        rq_job_id=rq_job_id,
        error=None,
    )
    try:
        conversion_queue(connection).enqueue_call(
            'app.tasks.execute_job',
            args=(task_id,),
            job_id=rq_job_id,
            timeout=max(10, int(os.getenv('PLT_JOB_TIMEOUT_SECONDS', '90'))),
            result_ttl=max(60, int(os.getenv('PLT_JOB_RETENTION_SECONDS', '1800'))),
            failure_ttl=max(60, int(os.getenv('PLT_JOB_RETENTION_SECONDS', '1800'))),
            on_failure=Callback(mark_job_failed),
            on_stopped=Callback(mark_job_stopped),
        )
    except Exception as error:
        update_job(
            task_id,
            connection,
            status='failed',
            progress=0,
            error=f'任务重试入队失败: {error}',
            finished_at=time.time(),
        )
        release_user_job(load_job(task_id, connection), connection)
        record_metric('failed', connection=connection)
        return False
    record_metric('retried', connection=connection)
    return True


def mark_job_stopped(job, connection):
    task_id = job.args[0] if job.args else job.id.split(':', 1)[0]
    lock = acquire_job_lock(task_id, connection)
    try:
        record = load_job(task_id, connection)
        if not record:
            return
        if record.get('status') in {'done', 'failed', 'cancelled'}:
            release_user_job(record, connection)
            return
        update_job(
            task_id,
            connection,
            status='cancelled',
            progress=0,
            finished_at=time.time(),
        )
    finally:
        lock.release()
    release_user_job(load_job(task_id, connection), connection)
    record_metric('cancelled', connection=connection)


def _execute(record, connection):
    job_id = record['job_id']
    job_type = record['job_type']
    source = Path(record['input_path']).read_bytes()
    options = record.get('options') or {}
    job_root = Path(record['input_path']).parent
    update_job(job_id, connection, progress=15)

    if job_type == 'plt_to_pdf':
        document = parse_plt(source, int(options.get('units_per_inch', 1016)))
        validate_parsed_plt(document)
        update_job(job_id, connection, progress=45)
        pdf, layout = render_pdf(document, options)
        output_path = job_root / f"{Path(record['filename']).stem}.pdf"
        output_path.write_bytes(pdf)
        return {'result_path': str(output_path), 'filename': output_path.name, 'layout': layout, 'mime_type': 'application/pdf'}

    if job_type == 'pdf_to_plt':
        update_job(job_id, connection, progress=35)
        plt, layout = convert_pdf_to_plt(source, options)
        output_path = job_root / f"{Path(record['filename']).stem}.plt"
        output_path.write_bytes(plt)
        return {'result_path': str(output_path), 'filename': output_path.name, 'layout': layout, 'mime_type': 'application/octet-stream'}

    if job_type == 'pdf_preview':
        preview_folder = job_root / 'previews'
        preview_folder.mkdir(parents=True, exist_ok=True)
        pages = inspect_pdf(source, preview_folder, job_id)
        columns = min(4, max(1, len(pages)))
        return {
            'pages': pages,
            'page_count': len(pages),
            'rows': max(1, (len(pages) + columns - 1) // columns),
            'columns': columns,
        }

    raise ValueError('不支持的任务类型')


def validate_parsed_plt(document):
    metrics = document['metrics']
    maximum_points = max(1000, int(os.getenv('PLT_MAX_POINTS', '500000')))
    maximum_paths = max(100, int(os.getenv('PLT_MAX_PATHS', '100000')))
    maximum_dimension = max(100, float(os.getenv('PLT_MAX_DIMENSION_MM', '10000')))
    if metrics['point_count'] > maximum_points:
        raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
    if metrics['path_count'] > maximum_paths:
        raise ValueError(f'PLT 路径过多，最多支持 {maximum_paths} 条')
    if metrics['width_mm'] > maximum_dimension or metrics['height_mm'] > maximum_dimension:
        raise ValueError(f'PLT 尺寸过大，单边最多支持 {maximum_dimension:g}mm')


def cleanup_job_files(record):
    input_path = record.get('input_path') if record else None
    if input_path:
        shutil.rmtree(Path(input_path).parent, ignore_errors=True)
