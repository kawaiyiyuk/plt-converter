import os
import shutil
import time
import math
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
from .billing import commit_conversion, release_conversion
from .services.pdf_renderer import render_pdf
from .services.pdf_layout_optimizer import optimize_pdf_layout
from .services.pdf_to_plt import (
    convert_pdf_to_plt,
    inspect_pdf,
    read_pdf_layout_metadata,
)
from .services.pdf_to_pdf import convert_pdf_to_pdf
from .services.plt_parser import parse_plt


def release_failed_pdf_to_pdf_billing(record):
    if not record or record.get('job_type') != 'pdf_to_pdf':
        return False
    user_key = str(record.get('user_key') or '')
    request_id = record.get('billing_request_id')
    job_id = record.get('job_id')
    if not user_key.startswith('user:') or not request_id or not job_id:
        return False
    try:
        return bool(release_conversion(int(user_key.split(':', 1)[1]), request_id, job_id))
    except Exception:
        # Preserve the original conversion failure. A reserved usage that cannot be
        # released immediately still expires in the main backend.
        return False


def persist_cancelled_pdf_to_pdf_billing_release(record, connection):
    """Release one cancelled composite reservation and persist the visible result."""
    if not record or record.get('job_type') != 'pdf_to_pdf':
        return record
    billing_released = release_failed_pdf_to_pdf_billing(record)
    return update_job(
        record['job_id'],
        connection,
        billing_released=bool(billing_released),
    )


def commit_successful_pdf_to_pdf_billing(record):
    if not record or record.get('job_type') != 'pdf_to_pdf':
        return False
    user_key = str(record.get('user_key') or '')
    request_id = record.get('billing_request_id')
    job_id = record.get('job_id')
    if not user_key.startswith('user:') or not request_id or not job_id:
        raise ValueError('PDF 纸张转换缺少计费信息')
    commit_conversion(int(user_key.split(':', 1)[1]), request_id, job_id)
    return True


def _update_job_progress(job_id, connection, progress):
    """Update progress without racing cancellation or a terminal transition."""
    lock = acquire_job_lock(job_id, connection)
    try:
        record = load_job(job_id, connection)
        if not record or record.get('status') in TERMINAL_STATUSES:
            return record
        if record.get('cancel_requested') or record.get('status') == 'cancelling':
            return record
        return update_job(job_id, connection, progress=progress)
    finally:
        lock.release()


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
            cancelled = update_job(
                job_id,
                connection,
                status='cancelled',
                finished_at=time.time(),
                progress=0,
            )
            persist_cancelled_pdf_to_pdf_billing_release(cancelled, connection)
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
                cancelled = update_job(
                    job_id,
                    connection,
                    status='cancelled',
                    finished_at=time.time(),
                    progress=0,
                )
                persist_cancelled_pdf_to_pdf_billing_release(cancelled, connection)
                release_user_job(load_job(job_id, connection), connection)
                return None
            commit_successful_pdf_to_pdf_billing(latest or record)
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
                cancelled = update_job(
                    job_id,
                    connection,
                    status='cancelled',
                    progress=0,
                    finished_at=time.time(),
                )
                persist_cancelled_pdf_to_pdf_billing_release(cancelled, connection)
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
            release_failed_pdf_to_pdf_billing(latest or record)
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
            if cancelled:
                persist_cancelled_pdf_to_pdf_billing_release(
                    load_job(job_id, connection),
                    connection,
                )
            else:
                release_failed_pdf_to_pdf_billing(latest or record)
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
            cancelled = update_job(
                task_id,
                connection,
                status='cancelled',
                progress=0,
                finished_at=time.time(),
            )
            persist_cancelled_pdf_to_pdf_billing_release(cancelled, connection)
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
        release_failed_pdf_to_pdf_billing(record)
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
    record = load_job(task_id, connection)
    timeout_environment = (
        'PDF_TO_PDF_JOB_TIMEOUT_SECONDS'
        if record and record.get('job_type') == 'pdf_to_pdf'
        else 'PLT_JOB_TIMEOUT_SECONDS'
    )
    default_timeout = '210' if timeout_environment == 'PDF_TO_PDF_JOB_TIMEOUT_SECONDS' else '90'
    try:
        conversion_queue(connection).enqueue_call(
            'app.tasks.execute_job',
            args=(task_id,),
            job_id=rq_job_id,
            timeout=max(10, int(os.getenv(timeout_environment, default_timeout))),
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
        release_failed_pdf_to_pdf_billing(record)
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
        cancelled = update_job(
            task_id,
            connection,
            status='cancelled',
            progress=0,
            finished_at=time.time(),
        )
        persist_cancelled_pdf_to_pdf_billing_release(cancelled, connection)
    finally:
        lock.release()
    release_user_job(load_job(task_id, connection), connection)
    record_metric('cancelled', connection=connection)


def execute_pdf_layout_suggestion(job_id, attempt=None):
    """Analyze PDF seams outside the HTTP worker and cache the suggestion on the preview."""
    connection = redis_connection()
    lock = acquire_job_lock(job_id, connection)
    try:
        record = load_job(job_id, connection)
        if not record or record.get('job_type') != 'pdf_preview':
            return None
        result = dict(record.get('result') or {})
        current_attempt = int(result.get('layout_suggestion_attempt', 0))
        expected_attempt = current_attempt if attempt is None else int(attempt)
        if current_attempt != expected_attempt:
            return None
        if result.get('layout_suggestion'):
            return result['layout_suggestion']
        result.update({
            'layout_suggestion_status': 'processing',
            'layout_suggestion_error': None,
            'layout_suggestion_started_at': time.time(),
        })
        update_job(job_id, connection, result=result)
        input_path = Path(record.get('input_path', ''))
    finally:
        lock.release()

    try:
        if not input_path.exists():
            raise ValueError('PDF 预览已过期，请重新选择文件')
        suggestion = optimize_pdf_layout(input_path.read_bytes())
    except Exception as error:
        _finish_pdf_layout_suggestion(
            job_id,
            connection,
            expected_attempt=expected_attempt,
            status='failed',
            error=str(error) or error.__class__.__name__,
        )
        raise

    _finish_pdf_layout_suggestion(
        job_id,
        connection,
        expected_attempt=expected_attempt,
        status='done',
        suggestion=suggestion,
    )
    return suggestion


def _finish_pdf_layout_suggestion(
    job_id,
    connection,
    status,
    suggestion=None,
    error=None,
    expected_attempt=None,
):
    lock = acquire_job_lock(job_id, connection)
    try:
        record = load_job(job_id, connection)
        if not record:
            return None
        result = dict(record.get('result') or {})
        if expected_attempt is not None and int(
            result.get('layout_suggestion_attempt', 0)
        ) != int(expected_attempt):
            return record
        if result.get('layout_suggestion_status') == 'done' and status != 'done':
            return record
        result.update({
            'layout_suggestion_status': status,
            'layout_suggestion_error': error,
            'layout_suggestion_finished_at': time.time(),
        })
        if suggestion is not None:
            result['layout_suggestion'] = suggestion
        return update_job(job_id, connection, result=result)
    finally:
        lock.release()


def _pdf_layout_job_identity(job):
    job_id = job.args[0] if job.args else job.id.split(':layout-suggestion:', 1)[0]
    if len(job.args) > 1:
        return job_id, int(job.args[1])
    suffix = job.id.rsplit(':layout-suggestion:', 1)
    return job_id, int(suffix[1]) if len(suffix) == 2 else None


def mark_pdf_layout_suggestion_failed(job, connection, type_, value, traceback):
    job_id, attempt = _pdf_layout_job_identity(job)
    _finish_pdf_layout_suggestion(
        job_id,
        connection,
        expected_attempt=attempt,
        status='failed',
        error=f'{type_.__name__}: {value}',
    )


def mark_pdf_layout_suggestion_stopped(job, connection):
    job_id, attempt = _pdf_layout_job_identity(job)
    _finish_pdf_layout_suggestion(
        job_id,
        connection,
        expected_attempt=attempt,
        status='failed',
        error='智能排版分析已停止，请重试',
    )


def _execute(record, connection):
    job_id = record['job_id']
    job_type = record['job_type']
    source = Path(record['input_path']).read_bytes()
    options = record.get('options') or {}
    job_root = Path(record['input_path']).parent
    _update_job_progress(job_id, connection, progress=15)

    if job_type == 'plt_to_pdf':
        document = parse_plt(source, int(options.get('units_per_inch', 1016)))
        validate_parsed_plt(document)
        _update_job_progress(job_id, connection, progress=45)
        pdf, layout = render_pdf(document, options)
        output_path = job_root / f"{Path(record['filename']).stem}.pdf"
        output_path.write_bytes(pdf)
        return {'result_path': str(output_path), 'filename': output_path.name, 'layout': layout, 'mime_type': 'application/pdf'}

    if job_type == 'pdf_to_plt':
        _update_job_progress(job_id, connection, progress=35)
        plt, layout = convert_pdf_to_plt(source, options)
        output_path = job_root / f"{Path(record['filename']).stem}.plt"
        output_path.write_bytes(plt)
        return {'result_path': str(output_path), 'filename': output_path.name, 'layout': layout, 'mime_type': 'application/octet-stream'}

    if job_type == 'pdf_to_pdf':
        _update_job_progress(job_id, connection, progress=25)
        pdf, conversion = convert_pdf_to_pdf(source, options)
        paper_size = str(options.get('paper_size', 'A4')).upper()
        output_path = job_root / f"{Path(record['filename']).stem}-{paper_size}.pdf"
        output_path.write_bytes(pdf)
        return {
            'result_path': str(output_path),
            'filename': output_path.name,
            **conversion,
            'mime_type': 'application/pdf',
        }

    if job_type == 'pdf_preview':
        preview_folder = job_root / 'previews'
        preview_folder.mkdir(parents=True, exist_ok=True)
        pages = inspect_pdf(source, preview_folder, job_id)
        embedded_layout = read_pdf_layout_metadata(source)
        complete_embedded_layout = (
            embedded_layout
            if embedded_layout and embedded_layout.get('complete_layout')
            else None
        )
        if complete_embedded_layout:
            columns = complete_embedded_layout['columns']
            rows = complete_embedded_layout['rows']
        else:
            columns = min(4, max(1, len(pages)))
            rows = max(1, math.ceil(len(pages) / columns))
            if rows > 24:
                columns = min(24, max(columns, math.ceil(len(pages) / 24)))
                rows = max(1, math.ceil(len(pages) / columns))
        result = {
            'pages': pages,
            'page_count': len(pages),
            'rows': rows,
            'columns': columns,
        }
        if complete_embedded_layout:
            result['embedded_layout'] = complete_embedded_layout
        return result

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
