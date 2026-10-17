"""Projection-only layout geometry and independently queued PDF analysis.

The legacy conversion modules are read-only dependencies. No conversion job,
billing hook or shared worker is changed by this module.
"""
import logging
import math
from pathlib import Path
import secrets
import time
from datetime import timezone

from rq import Queue, Worker
from rq.exceptions import NoSuchJobError, InvalidJobOperation
from rq.job import Job

import os
from flask import current_app, has_app_context
from redis import Redis
from redis.exceptions import RedisError
from .projection_store import FIELDS, Policy, Store, ProjectionRejected
from .services.pdf_layout_optimizer import optimize_pdf_layout
from .services.pdf_to_plt import load_fitz

QUEUE_NAME = 'pdf-projection-layout'
ANALYSIS_TIMEOUT = 90
QUEUE_CAPACITY = 20

logger = logging.getLogger(__name__)


def projection_queue(connection=None):
    return Queue(QUEUE_NAME, connection=connection if connection is not None else projection_connection(), default_timeout=ANALYSIS_TIMEOUT)


class ProjectionWorkerUnavailable(ProjectionRejected):
    def __init__(self,message):
        super().__init__('projection_worker_unavailable',message,503)


def discard_analysis_job(job_id, connection=None):
    """Best-effort cancel + dequeue of superseded analysis work.

    Without this a metadata_mode switch leaves the old job counted by
    queue.count until a worker drains it, letting one account occupy the
    global queue. RQ/Redis failures are non-fatal: the job stays guarded by
    its analysis_id/generation and simply discards itself when it runs.
    """
    if not job_id:
        return
    try:
        connection = connection if connection is not None else projection_connection()
        job = Job.fetch(job_id, connection=connection)
        job.cancel()
        projection_queue(connection).remove(job_id)
    except (NoSuchJobError, InvalidJobOperation, RedisError, ProjectionWorkerUnavailable):
        pass


def projection_connection(blocking=False):
    url = current_app.config['PROJECTOR_REDIS_URL'] if has_app_context() else os.environ.get('PROJECTOR_REDIS_URL')
    if not url:
        raise ProjectionWorkerUnavailable('投影Redis尚未配置，原分页保留')
    connection=Redis.from_url(url,socket_connect_timeout=2,socket_timeout=None if blocking else 2)
    # RQ1.16.2 _set_connection fills a None worker timeout with
    # dequeue_timeout+10 (default405+10). Never reuse API's two-second pool.
    # from_url query args normally override kwargs; preserve this explicit
    # API/worker contract even when the configured URI contains timeouts.
    connection.connection_pool.connection_kwargs.update(
        socket_connect_timeout=2,socket_timeout=None if blocking else 2)
    return connection



def _utc_seconds(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def has_live_projection_worker(connection=None):
    connection = connection if connection is not None else projection_connection()
    for worker in Worker.all(connection=connection):
        worker.refresh()
        if {queue.name for queue in worker.queues} != {QUEUE_NAME}:
            continue
        heartbeat = worker.last_heartbeat
        if (heartbeat is not None and worker.get_state() in ('idle', 'busy')
                and 0 <= time.time() - _utc_seconds(heartbeat) <= worker.worker_ttl):
            return True
    return False


def _integer(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name} 必须为整数')
    try:
        parsed = int(value)
        if float(value) != parsed:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError(f'{name} 必须为整数') from error


def build_layout(pages, rows, columns, order, crop, page_slots=None, source='manual'):
    rows, columns = _integer(rows, '行数'), _integer(columns, '列数')
    if not (1 <= rows <= 24 and 1 <= columns <= 24) or rows * columns < len(pages):
        raise ValueError('行列必须为 1 至 24，且格数不得少于 PDF 页数')
    if order not in ('row', 'column'):
        raise ValueError('排列顺序只支持 row 或 column')
    margins = {}
    for side in ('left', 'right', 'top', 'bottom'):
        try:
            value = float(crop.get(side, 0))
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError(f'{side} 裁边必须为非负有限数值') from error
        if not math.isfinite(value) or value < 0:
            raise ValueError(f'{side} 裁边必须为非负有限数值')
        margins[side] = value
    capacity = rows * columns
    if page_slots is None:
        slots = list(range(len(pages))) + [None] * (capacity - len(pages))
    else:
        if not isinstance(page_slots, list) or len(page_slots) != capacity:
            raise ValueError('排版槽位数量与网格不一致')
        slots = [None if value is None else _integer(value, '页面索引') for value in page_slots]
    if sorted(index for index in slots if index is not None) != list(range(len(pages))):
        raise ValueError('每页必须出现且仅出现一次，不能遗漏、重复或越界')
    visible_sizes = []
    for page in pages:
        width = float(page['width_mm']) - margins['left'] - margins['right']
        height = float(page['height_mm']) - margins['top'] - margins['bottom']
        if not math.isfinite(width) or not math.isfinite(height) or width < 1 - 1e-9 or height < 1 - 1e-9:
            raise ValueError(f"第 {page['index'] + 1} 页裁边后必须至少保留 1mm × 1mm")
        visible_sizes.append((width, height))
    occupied = []
    column_widths, row_heights = [0.0] * columns, [0.0] * rows
    for slot, index in enumerate(slots):
        row, column = divmod(slot, columns) if order == 'row' else (slot % rows, slot // rows)
        if index is not None:
            width, height = visible_sizes[index]
            column_widths[column] = max(column_widths[column], width)
            row_heights[row] = max(row_heights[row], height)
            occupied.append((index, row, column, width, height))
    # Empty rows/columns retain a real blank cell instead of silently collapsing.
    fallback_width = max(width for width, _ in visible_sizes)
    fallback_height = max(height for _, height in visible_sizes)
    column_widths = [width or fallback_width for width in column_widths]
    row_heights = [height or fallback_height for height in row_heights]
    # Keep the cropped grid (including empty rows / columns) as the seam
    # coordinate system. Only edges without an occupied neighbour extend back
    # to the source boundary; setting all crop to zero would move inner seams.
    occupied_cells = {(row, column) for _, row, column, _, _ in occupied}
    placements = []
    for index, row, column, page_width, page_height in occupied:
        neighbours = {'left': (row, column - 1), 'right': (row, column + 1),
                      'top': (row - 1, column), 'bottom': (row + 1, column)}
        actual_crop = {side: margins[side] if cell in occupied_cells else 0.0
                       for side, cell in neighbours.items()}
        extension = {side: margins[side] - actual_crop[side] for side in margins}
        placements.append({'page_index': index,
                           'x_mm': sum(column_widths[:column]) - extension['left'],
                           'y_mm': sum(row_heights[:row]) - extension['top'],
                           'width_mm': page_width + extension['left'] + extension['right'],
                           'height_mm': page_height + extension['top'] + extension['bottom'],
                           'crop_margins_mm': actual_crop})
    left = min(0.0, *(placement['x_mm'] for placement in placements))
    top = min(0.0, *(placement['y_mm'] for placement in placements))
    right = max(sum(column_widths), *(p['x_mm'] + p['width_mm'] for p in placements))
    bottom = max(sum(row_heights), *(p['y_mm'] + p['height_mm'] for p in placements))
    width, height = right - left, bottom - top
    if not all(math.isfinite(value) and 0 < value < 3.4e38 for value in (width, height)):
        raise ValueError('拼版尺寸超出 PDF 渲染器数值范围')
    for placement in placements:
        placement['x_mm'] -= left
        placement['y_mm'] -= top
    return {'rows': rows, 'columns': columns, 'order': order, 'page_slots': slots,
            'crop_margins_mm': margins, 'width_mm': width, 'height_mm': height,
            'source': source, 'placements': placements, 'preserve_outer_edges': True}


def public_layout(layout):
    return {key: value for key, value in layout.items() if key != 'placements'}


def analysis_response(analysis):
    response = {key: analysis[key] for key in ('analysis_id', 'status', 'suggestion', 'reason', 'error') if key in analysis}
    if analysis.get('status') == 'analyzing':
        response['phase'] = analysis.get('phase', 'queued')
    return response


def resolve_metadata_mode(meta, value):
    if value not in (None, '', 'original', 'current'):
        raise ValueError('元数据处理方式只支持 original 或 current')
    if meta.get('requires_metadata_choice') and not value:
        raise ValueError('检测到排版信息，请明确选择使用原排版或按当前 PDF 内容')
    return value or 'current'


def analyze_source(source, mode, embedded):
    # The deployed legacy engine has no metadata_mode argument. Selecting
    # current means removing Subject only in a temporary analysis document.
    # Original PDF bytes and the tile renderer always remain unchanged.
    if mode == 'original' and embedded and embedded.get('complete_layout'):
        return {**embedded, 'confidence': 'high', 'confidence_score': 1.0, 'source': 'embedded_metadata'}
    if mode == 'current' or embedded:
        fitz = load_fitz()
        with fitz.open(stream=source, filetype='pdf') as document:
            if document.metadata.get('subject'):
                document.set_metadata({**document.metadata, 'subject': ''})
                source = document.tobytes()
    return optimize_pdf_layout(source)


def publish_analysis(ledger, document_id, owner, analysis_id, result, generation):
    # Guard and mutation share the metadata-pointer SQLite transaction. A GET
    # phase update is not a new generation and cannot discard this result.
    return ledger.update_analysis(document_id,owner,analysis_id,generation,result)


def execute_layout_analysis(document_id, owner, root_path, analysis_id, mode, configuration, lease):
    from .projection_runtime import run
    ledger = Store(root_path,Policy(configuration))
    meta = ledger.load(document_id,owner)
    if (meta is None or (meta.get('analysis') or {}).get('analysis_id') != analysis_id
            or not ledger.valid_lease(lease)):
        ledger.release(lease)
        return {'status':'discarded'}
    generation = meta['_generation']
    if not ledger.update_analysis(document_id,owner,analysis_id,generation,{'phase':'analyzing'}):
        ledger.release(lease)
        return {'status':'discarded'}
    # The RQ horse also supervises a resource-limited child; if RQ kills its
    # horse at 90s, Linux PDEATHSIG kills the child, preserving the disk lease.
    with ledger.transaction() as db:
        from .projection_store import process_identity
        db.execute('UPDATE leases SET supervisor=?,supervisor_identity=? WHERE id=?',
                   (os.getpid(),process_identity(os.getpid()),lease))
    try:
        suggestion = run(ledger,lease,'analysis',ledger.source_path(document_id),
                         {'mode':mode,'embedded':meta.get('embedded_layout')},deadline=ANALYSIS_TIMEOUT)
        if suggestion.get('confidence') not in ('medium','high'):
            result = {'status':'insufficient_evidence','reason':suggestion.get('reason') or '无法可靠识别页面接缝，原分页保留'}
        else:
            layout = build_layout(meta['pages'],suggestion['rows'],suggestion['columns'],suggestion['order'],
                                  suggestion.get('crop_margins_mm') or {},suggestion['page_slots'],
                                  suggestion.get('source') or 'vector_seams')
            result = {'status':'ready','suggestion':{**public_layout(layout),
                      'confidence':suggestion['confidence'],'confidence_score':suggestion.get('confidence_score')}}
    except Exception as error:
        # Internal analysis failures must not leak exception text to clients.
        logger.warning('projection layout analysis %s failed: %r', analysis_id, error)
        result = {'status':'failed','error':'排版分析失败，请重试或手动排版'}
    finally:
        ledger.release(lease)
    published = publish_analysis(ledger,document_id,owner,analysis_id,result,generation)
    return result if published else {'status':'discarded'}


def submit_analysis(ledger, document_id, owner, meta, mode):
    previous = meta.get('analysis') or {}
    if previous.get('status') == 'analyzing':
        refresh_analysis_failure(ledger,document_id,owner,meta)
        meta = ledger.load(document_id,owner)
        if meta is None:
            # A concurrent DELETE removed the session between the two reads.
            raise ProjectionRejected('projection_not_found','投影文件不存在或已过期',404,0)
    try:
        connection = projection_connection()
        queue = projection_queue(connection)
        with connection.lock(f'{QUEUE_NAME}:submission-lock',timeout=15,blocking_timeout=2):
            # Recheck after the bounded Redis mutex; never hold a SQLite lock
            # over network I/O. Concurrent layout/delete invalidates publication.
            current = ledger.load(document_id,owner)
            if current is None or current['_generation'] != meta['_generation']:
                raise ProjectionRejected('projection_result_stale','投影会话或排版已变化',409,0)
            active = current.get('analysis') or {}
            if active.get('mode') == mode and active.get('status') in ('analyzing','ready'):
                return analysis_response(active)
            if not has_live_projection_worker(connection):
                raise ProjectionWorkerUnavailable('投影排版Worker暂时不可用，原分页保留，请稍后重试')
            prior_job = active.get('analysis_id') if active.get('status') == 'analyzing' else None
            # A mode replacement takes the queued job's existing slot; only
            # remove it after the SQLite identity CAS actually succeeds.
            replacing_queued = bool(prior_job and prior_job in queue.job_ids)
            if queue.count - int(replacing_queued) >= QUEUE_CAPACITY:
                raise ProjectionRejected('projection_analysis_queue_full','投影排版队列已满，请稍后重试',429,10)
            # At most one in-flight analysis per owner across all documents.
            if ledger.analyzing_documents(owner,exclude=document_id):
                raise ProjectionRejected('projection_analysis_owner_busy',
                    '该账户已有正在进行的排版分析，请稍后重试',429,5)
            lease = ledger.reserve(owner,'analysis',document_id,current['_generation'])
            analysis_id = f'projection-layout-{document_id}-{secrets.token_hex(8)}'
            analysis = {'analysis_id':analysis_id,'status':'analyzing','mode':mode,'phase':'queued','lease':lease}
            configuration = {'PROJECTOR_'+field:str(getattr(ledger.policy,field.lower())) for field in FIELDS}
            try:
                replaced = ledger.replace_analysis(document_id,owner,analysis,current['_generation'],active.get('analysis_id'))
            except BaseException:
                ledger.release(lease)
                raise
            if not replaced:
                ledger.release(lease)
                raise ProjectionRejected('projection_result_stale','投影会话或排版已变化',409,0)
            try:
                # The old lease was already invalidated in the metadata CAS.
                # A live child keeps its budget until its supervisor reaps it;
                # a stale submitter cannot cancel a different winning analysis.
                discard_analysis_job(prior_job,connection)
                if active.get('lease'):
                    ledger.release(active['lease'])
                queue.enqueue_call(execute_layout_analysis,
                    args=(document_id,owner,str(ledger.root),analysis_id,mode,configuration,lease),
                    job_id=analysis_id,timeout=ANALYSIS_TIMEOUT,ttl=3600,result_ttl=3600,failure_ttl=3600)
            except Exception:
                ledger.release(lease)
                ledger.update_analysis(document_id,owner,analysis_id,current['_generation'],
                    {'status':'failed','error':'投影排版任务入队失败，请重试'})
                raise
        return analysis_response(analysis)
    except RedisError as error:
        raise ProjectionWorkerUnavailable('投影排版服务暂时不可用，原分页保留') from error


def refresh_analysis_failure(ledger,document_id,owner,meta):
    analysis = meta.get('analysis') or {}
    if analysis.get('status') != 'analyzing':
        return analysis_response(analysis)
    try:
        job = Job.fetch(analysis['analysis_id'],connection=projection_connection())
        status = job.get_status(refresh=True)
    except NoSuchJobError:
        status = 'missing'
    except RedisError as error:
        raise ProjectionWorkerUnavailable('投影排版状态暂时不可用，原分页保留') from error
    reason = None
    if status in ('failed','stopped','canceled','cancelled','missing','finished'):
        reason = '投影排版任务失败、超时或结果不可用，原分页保留，请重试'
    elif status == 'queued':
        try:
            worker_live = has_live_projection_worker()
        except RedisError as error:
            raise ProjectionWorkerUnavailable('投影排版Worker状态暂时不可用，原分页保留') from error
        if not worker_live:
            if job.origin == QUEUE_NAME:
                try:
                    job.cancel(); projection_queue().remove(job.id)
                except (NoSuchJobError, InvalidJobOperation, RedisError) as error:
                    raise ProjectionWorkerUnavailable('投影排版服务暂时不可用，原分页保留') from error
            reason = '投影排版Worker暂时不可用，原分页保留，请重试'
    elif status == 'started' and job.started_at and time.time()-_utc_seconds(job.started_at)>ANALYSIS_TIMEOUT:
        reason = '投影排版分析已超时，原分页保留，请重试'
    if reason:
        # A failed analysis is analysis state only; it must not invalidate the
        # generation that in-flight tile renders depend on. The analysis CAS
        # still rejects a late phase update, so no stale overwrite is allowed.
        applied=ledger.update_analysis(document_id,owner,analysis['analysis_id'],meta['_generation'],
                                       {'status':'failed','error':reason})
        # A stale GET that lost CAS must not release/cancel the winning result.
        if applied and analysis.get('lease'):
            ledger.release(analysis['lease'])
    elif status in ('queued','started'):
        phase='queued' if status=='queued' else 'analyzing'
        if analysis.get('phase')!=phase:
            ledger.update_analysis(document_id,owner,analysis['analysis_id'],meta['_generation'],{'phase':phase})
    latest=ledger.load(document_id,owner)
    if latest is None:
        raise ProjectionRejected('projection_not_found','投影文件不存在或已过期',404,0)
    return analysis_response(latest.get('analysis') or {})


def worker_main():
    """Use PROJECTOR_* configuration and consume only the projection queue."""
    from .projection_app import create_projection_app
    app = create_projection_app()
    if not app.config['PROJECTOR_ENABLED']:
        raise RuntimeError('projection worker requires explicit complete policy')
    with app.app_context():
        connection = projection_connection(blocking=True)
        Worker([projection_queue(connection)],connection=connection).work()
