import json
import math
import os
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file, url_for
from werkzeug.utils import secure_filename

from .billing import BillingRejected, authorize_conversion, commit_conversion, identify_user, release_conversion
from .services.plt_metadata import inspect_plt
from .job_queue import (
    QueueRejected,
    cancel_job,
    confirm_job_billing,
    enforce_rate_limit,
    load_job,
    queue_position,
    redis_connection,
    submit_job,
)
from redis.exceptions import RedisError


plt_bp = Blueprint('plt', __name__, url_prefix='/api/v1/plt')
pdf_bp = Blueprint('pdf', __name__, url_prefix='/api/v1/pdf')
ALLOWED_EXTENSIONS = {'plt', 'hpgl', 'txt'}
PDF_ALLOWED_EXTENSIONS = {'pdf'}


def conversion_request_id():
    return (request.headers.get('X-Conversion-Request-ID') or '').strip()


def conversion_allow_charge():
    return (request.headers.get('X-Conversion-Allow-Charge') or '').lower() == 'true'


def billing_error(error):
    payload = {'error': str(error), 'status': 'billing_rejected'}
    if error.data:
        payload.update(error.data)
    return jsonify(payload), error.status_code


def authorize_job(conversion_type):
    return authorize_conversion(
        request.headers.get('Authorization'),
        conversion_request_id(),
        conversion_type,
        conversion_allow_charge(),
    )


def rollback_conversion_submission(billing, record, billing_confirmed=False):
    if billing_confirmed or not billing:
        return
    job_id = record.get('job_id') if record else None
    if job_id:
        try:
            cancel_job(job_id, f"user:{billing['user_id']}")
        except (RedisError, QueueRejected, PermissionError):
            pass
    release_conversion(billing['user_id'], billing['request_id'], job_id)


def authenticated_user_key():
    return identify_user(request.headers.get('Authorization'))


def owned_job(job_id, expected_type=None):
    user_key = authenticated_user_key()
    record = load_job(job_id)
    if not record or record.get('user_key') != user_key:
        return None
    if expected_type and record.get('job_type') != expected_type:
        return None
    return record


def request_user_key():
    return (
        request.headers.get('X-Client-Key')
        or request.headers.get('X-WX-OpenID')
        or request.remote_addr
        or 'anonymous'
    )[:160]


def enforce_upload_limits():
    connection = redis_connection()
    enforce_rate_limit(
        request_user_key(),
        connection,
        scope='upload-client',
        limit_env='PLT_UPLOAD_RATE_LIMIT_PER_MINUTE',
    )
    remote_address = (request.remote_addr or 'unknown')[:80]
    enforce_rate_limit(
        f'ip:{remote_address}',
        connection,
        scope='upload-ip',
        limit_env='PLT_UPLOAD_IP_RATE_LIMIT_PER_MINUTE',
    )


def queue_error(error):
    response = jsonify({
        'error': str(error),
        'status': 'rejected',
        'retry_after': error.retry_after,
    })
    response.status_code = 429
    response.headers['Retry-After'] = str(error.retry_after)
    return response


def redis_unavailable(_error):
    return jsonify({'error': '任务服务暂时不可用，请稍后重试', 'status': 'unavailable'}), 503


def enforce_upload_entry_limits():
    if request.method != 'POST':
        return None
    try:
        enforce_upload_limits()
    except RedisError as error:
        return redis_unavailable(error)
    except QueueRejected as error:
        return queue_error(error)
    return None


plt_bp.before_request(enforce_upload_entry_limits)
pdf_bp.before_request(enforce_upload_entry_limits)


def job_response(record):
    response = {
        'job_id': record['job_id'],
        'job_type': record.get('job_type'),
        'status': record.get('status'),
        'progress': record.get('progress', 0),
        'created_at': record.get('created_at'),
        'started_at': record.get('started_at'),
        'finished_at': record.get('finished_at'),
        'error': record.get('error'),
        'deduplicated': record.get('deduplicated', False),
    }
    if record.get('status') == 'queued':
        response['queue_position'] = queue_position(record['job_id'])
    result = record.get('result') or {}
    if record.get('status') == 'done':
        response.update({key: value for key, value in result.items() if key not in {'result_path', 'mime_type'}})
        if record.get('job_type') == 'plt_to_pdf':
            response['pdf_path'] = url_for('plt.download_pdf', job_id=record['job_id'])
        elif record.get('job_type') == 'pdf_to_plt':
            response['plt_path'] = url_for('pdf.download_plt', job_id=record['job_id'])
        elif record.get('job_type') == 'pdf_preview':
            preview_id = record['job_id']
            response['status'] = 'preview_ready'
            response['preview_id'] = preview_id
            response['pages'] = [
                {
                    **page,
                    'preview_path': url_for(
                        'pdf.preview_image',
                        preview_id=preview_id,
                        page_index=page['index'],
                    ),
                }
                for page in result.get('pages', [])
            ]
    return response


@plt_bp.post('/preview')
def preview_plt():
    uploaded = request.files.get('file')
    if uploaded is None or not uploaded.filename:
        return jsonify({'error': '请选择 PLT 文件'}), 400

    filename = safe_uploaded_filename(uploaded.filename)
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if extension not in ALLOWED_EXTENSIONS:
        return jsonify({'error': '只支持 .plt、.hpgl 或 .txt 文件'}), 415

    try:
        enforce_rate_limit(request_user_key(), scope='preview', limit_env='PLT_PREVIEW_RATE_LIMIT_PER_MINUTE')
    except RedisError as error:
        return redis_unavailable(error)
    except QueueRejected as error:
        return queue_error(error)
    source = uploaded.read()
    try:
        metadata = inspect_plt(
            source,
            units_per_inch=int(request.form.get('units_per_inch', 1016)),
        )
    except ValueError as error:
        return jsonify({'error': str(error)}), 422

    return jsonify({
        'status': 'preview_ready',
        'filename': filename,
        'metadata': metadata,
        'temporary': True,
    })


@plt_bp.post('/jobs')
def create_conversion_job():
    uploaded = request.files.get('file')
    validation_error = validate_upload(uploaded)
    if validation_error:
        return validation_error

    billing = None
    record = None
    billing_confirmed = False
    try:
        billing = authorize_job('plt_to_pdf')
        source = uploaded.read()
        record = submit_job(
            'plt_to_pdf',
            source,
            safe_uploaded_filename(uploaded.filename),
            parse_render_options(request.form) | {
                'units_per_inch': parse_units_per_inch(request.form),
            },
            f"user:{billing['user_id']}",
            billing_request_id=billing['request_id'],
        )
        commit_conversion(billing['user_id'], billing['request_id'], record['job_id'])
        billing_confirmed = True
        record = confirm_job_billing(record['job_id'], f"user:{billing['user_id']}")
    except BillingRejected as error:
        rollback_conversion_submission(billing, record, billing_confirmed)
        return billing_error(error)
    except RedisError as error:
        rollback_conversion_submission(billing, record, billing_confirmed)
        return redis_unavailable(error)
    except QueueRejected as error:
        rollback_conversion_submission(billing, record, billing_confirmed)
        return queue_error(error)
    except ValueError as error:
        rollback_conversion_submission(billing, record, billing_confirmed)
        return jsonify({'error': str(error)}), 422
    except Exception:
        rollback_conversion_submission(billing, record, billing_confirmed)
        raise
    return jsonify(job_response(record)), 200


@plt_bp.get('/jobs/<job_id>')
def get_conversion_job(job_id):
    try:
        record = owned_job(job_id, 'plt_to_pdf')
    except BillingRejected as error:
        return billing_error(error)
    if record:
        return jsonify(job_response(record))
    return jsonify({'error': '任务不存在或已过期'}), 404


@plt_bp.delete('/jobs/<job_id>')
def cancel_conversion_job(job_id):
    try:
        user_key = authenticated_user_key()
        record = cancel_job(job_id, user_key)
    except BillingRejected as error:
        return billing_error(error)
    except PermissionError as error:
        return jsonify({'error': str(error)}), 403
    except QueueRejected as error:
        return queue_error(error)
    except RedisError as error:
        return redis_unavailable(error)
    if not record:
        return jsonify({'error': '任务不存在或已过期'}), 404
    return jsonify(job_response(record))


@plt_bp.get('/files/<job_id>.pdf')
def download_pdf(job_id):
    try:
        record = owned_job(job_id, 'plt_to_pdf')
    except BillingRejected as error:
        return billing_error(error)
    if not record:
        return jsonify({'error': '文件不存在或已过期'}), 404
    output_path = Path(record.get('result_path')) if record and record.get('result_path') else None
    if output_path is None or not output_path.exists():
        return jsonify({'error': '文件不存在或已过期'}), 404
    filename = (record.get('result') or {}).get('filename', f'{job_id}.pdf') if record else f'{job_id}.pdf'
    return send_file(output_path, mimetype='application/pdf', as_attachment=True, download_name=filename, max_age=0)


@pdf_bp.post('/preview')
def preview_pdf():
    uploaded = request.files.get('file')
    validation_error = validate_pdf_upload(uploaded)
    if validation_error:
        return validation_error
    try:
        record = submit_job(
            'pdf_preview',
            uploaded.read(),
            safe_uploaded_filename(uploaded.filename),
            {},
            request_user_key(),
        )
    except RedisError as error:
        return redis_unavailable(error)
    except QueueRejected as error:
        return queue_error(error)
    return jsonify(job_response(record)), 200


@pdf_bp.get('/previews/<preview_id>/<int:page_index>.png')
def preview_image(preview_id, page_index):
    record = load_job(preview_id)
    if not record or record.get('job_type') != 'pdf_preview':
        return jsonify({'error': '预览已过期'}), 404
    if record.get('user_key') != request_user_key():
        return jsonify({'error': '无权访问该预览'}), 403
    pages = (record.get('result') or {}).get('pages') or []
    if page_index < 0 or page_index >= len(pages):
        return jsonify({'error': '预览页不存在'}), 404
    image_path = Path(record.get('input_path', '')).parent / 'previews' / f'{preview_id}-{page_index}.png'
    if not image_path.exists():
        return jsonify({'error': '预览图不存在'}), 404
    return send_file(image_path, mimetype='image/png', max_age=300)


@pdf_bp.post('/jobs')
def create_pdf_to_plt_job():
    uploaded = request.files.get('file')
    validation_error = validate_pdf_upload(uploaded)
    if validation_error:
        return validation_error
    billing = None
    record = None
    billing_confirmed = False
    try:
        billing = authorize_job('pdf_to_plt')
        record = submit_job(
            'pdf_to_plt',
            uploaded.read(),
            safe_uploaded_filename(uploaded.filename),
            parse_pdf_render_options(request.form),
            f"user:{billing['user_id']}",
            billing_request_id=billing['request_id'],
        )
        commit_conversion(billing['user_id'], billing['request_id'], record['job_id'])
        billing_confirmed = True
        record = confirm_job_billing(record['job_id'], f"user:{billing['user_id']}")
    except BillingRejected as error:
        rollback_conversion_submission(billing, record, billing_confirmed)
        return billing_error(error)
    except RedisError as error:
        rollback_conversion_submission(billing, record, billing_confirmed)
        return redis_unavailable(error)
    except QueueRejected as error:
        rollback_conversion_submission(billing, record, billing_confirmed)
        return queue_error(error)
    except ValueError as error:
        rollback_conversion_submission(billing, record, billing_confirmed)
        return jsonify({'error': str(error)}), 422
    except Exception:
        rollback_conversion_submission(billing, record, billing_confirmed)
        raise
    return jsonify(job_response(record)), 200


@pdf_bp.get('/jobs/<job_id>')
def get_pdf_to_plt_job(job_id):
    try:
        record = owned_job(job_id, 'pdf_to_plt')
    except BillingRejected as error:
        return billing_error(error)
    if record:
        return jsonify(job_response(record))
    return jsonify({'error': '任务不存在或已过期'}), 404


@pdf_bp.get('/preview/jobs/<job_id>')
def get_pdf_preview_job(job_id):
    """Return the status and preview pages for a PDF preview task."""
    record = load_job(job_id)
    if record and (
        record.get('job_type') != 'pdf_preview'
        or record.get('user_key') != request_user_key()
    ):
        record = None
    if record:
        return jsonify(job_response(record))
    return jsonify({'error': '任务不存在或已过期'}), 404


@pdf_bp.delete('/jobs/<job_id>')
def cancel_pdf_to_plt_job(job_id):
    try:
        user_key = authenticated_user_key()
        record = cancel_job(job_id, user_key)
    except BillingRejected as error:
        return billing_error(error)
    except PermissionError as error:
        return jsonify({'error': str(error)}), 403
    except QueueRejected as error:
        return queue_error(error)
    except RedisError as error:
        return redis_unavailable(error)
    if not record:
        return jsonify({'error': '任务不存在或已过期'}), 404
    return jsonify(job_response(record))


@pdf_bp.get('/files/<job_id>.plt')
def download_plt(job_id):
    try:
        record = owned_job(job_id, 'pdf_to_plt')
    except BillingRejected as error:
        return billing_error(error)
    if not record:
        return jsonify({'error': 'PLT 文件不存在或已过期'}), 404
    output_path = Path(record.get('result_path')) if record and record.get('result_path') else None
    if output_path is None or not output_path.exists():
        return jsonify({'error': 'PLT 文件不存在或已过期'}), 404
    filename = (record.get('result') or {}).get('filename', f'{job_id}.plt') if record else f'{job_id}.plt'
    return send_file(output_path, mimetype='application/octet-stream', as_attachment=True, download_name=filename, max_age=0)


@plt_bp.get('/metrics')
@pdf_bp.get('/metrics')
def metrics():
    metrics_token = os.getenv('PLT_METRICS_TOKEN')
    if not metrics_token:
        return jsonify({'error': '指标接口未启用'}), 404
    if request.headers.get('X-Metrics-Token') != metrics_token:
        return jsonify({'error': '需要指标访问令牌'}), 401
    try:
        connection = redis_connection()
        connection.ping()
    except RedisError as error:
        return redis_unavailable(error)
    from .job_queue import queue_load
    return jsonify({
        'queue': queue_load(connection),
        'metrics': {
            (key.decode('utf-8') if isinstance(key, bytes) else key): int(value)
            for key, value in connection.hgetall('plt-converter:metrics').items()
        },
    })


def validate_upload(uploaded):
    if uploaded is None or not uploaded.filename:
        return jsonify({'error': '请选择 PLT 文件'}), 400
    extension = uploaded.filename.rsplit('.', 1)[-1].lower() if '.' in uploaded.filename else ''
    if extension not in ALLOWED_EXTENSIONS:
        return jsonify({'error': '只支持 .plt、.hpgl 或 .txt 文件'}), 415
    return None


def validate_pdf_upload(uploaded):
    if uploaded is None or not uploaded.filename:
        return jsonify({'error': '请选择 PDF 文件'}), 400
    extension = uploaded.filename.rsplit('.', 1)[-1].lower() if '.' in uploaded.filename else ''
    if extension not in PDF_ALLOWED_EXTENSIONS:
        return jsonify({'error': '只支持 .pdf 文件'}), 415
    return None


def safe_uploaded_filename(filename):
    raw = str(filename or '')
    extension = raw.rsplit('.', 1)[-1].lower() if '.' in raw else ''
    raw_stem = raw.rsplit('.', 1)[0] if extension else raw
    stem = secure_filename(raw_stem) or 'upload'
    return f'{stem}.{extension}' if extension else stem


def parse_units_per_inch(form):
    try:
        value = int(form.get('units_per_inch', 1016))
    except (TypeError, ValueError):
        value = 1016
    if value <= 0:
        raise ValueError('units_per_inch 必须大于 0')
    return value


def parse_render_options(form):
    def number(name, default):
        try:
            value = float(form.get(name, default))
        except (TypeError, ValueError):
            return default
        if not math.isfinite(value):
            raise ValueError(f'{name} 参数无效')
        return value

    def boolean(name, default=False):
        value = str(form.get(name, '')).lower()
        if not value:
            return default
        return value in {'1', 'true', 'yes', 'on'}

    raw_enabled_pages = form.get('enabled_pages', '')
    enabled_pages = None
    if raw_enabled_pages:
        try:
            enabled_pages = [int(value) for value in json.loads(raw_enabled_pages)]
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError('enabled_pages 参数无效')
    return {
        'units_per_inch': parse_units_per_inch(form),
        'paper_size': str(form.get('paper_size', 'A4')).upper(),
        'orientation': str(form.get('orientation', 'portrait')).lower(),
        'margin_mm': number('margin_mm', 10),
        'line_width_mm': number('line_width_mm', 0.265),
        'single_page_output': boolean('single_page_output'),
        'show_page_number': boolean('show_page_number', True),
        'enabled_pages': enabled_pages,
    }


def parse_pdf_render_options(form):
    def number(name, default):
        try:
            value = float(form.get(name, default))
        except (TypeError, ValueError):
            return default
        if not math.isfinite(value):
            raise ValueError(f'{name} 参数无效')
        return value

    raw_enabled_pages = form.get('enabled_pages', '')
    enabled_pages = None
    if raw_enabled_pages:
        try:
            enabled_pages = [int(value) for value in json.loads(raw_enabled_pages)]
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError('enabled_pages 参数无效') from error
    raw_page_slots = form.get('page_slots', '')
    page_slots = None
    if raw_page_slots:
        try:
            page_slots = [
                None if value is None else int(value)
                for value in json.loads(raw_page_slots)
            ]
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError('page_slots 参数无效') from error
    return {
        'units_per_inch': parse_units_per_inch(form),
        'rows': max(1, min(24, int(number('rows', 1)))),
        'columns': max(1, min(24, int(number('columns', 1)))),
        'order': str(form.get('order', 'row')).lower(),
        'margin_mm': number('margin_mm', 0),
        'line_width_mm': number('line_width_mm', 0.265),
        'enabled_pages': enabled_pages,
        'page_slots': page_slots,
    }
