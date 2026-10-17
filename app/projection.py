"""Authenticated projection HTTP controller; heavy PDF work runs only in supervised children."""
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
import time
from flask import Blueprint, Response, current_app, g, jsonify, request
import unicodedata
from .billing import BillingRejected, identify_user
from .projection_proxy import upload_rate_limit_ip
from .projection_store import MAX_BODY, MAX_SOURCE, ProjectionRejected
from .projection_pdf_runtime import load_fitz

projection_bp = Blueprint('projection', __name__, url_prefix='/api/v1/pdf/projection')
TTL_SECONDS = 3600
PT_PER_MM = 72 / 25.4
ID_PATTERN = re.compile(r'^[0-9a-f]{64}$')


def safe_uploaded_filename(filename):
    # Exact legacy filename contract; no dependency on legacy HTTP routes.
    raw = unicodedata.normalize('NFC', str(filename or '')).replace('\\', '/')
    name = raw.rsplit('/', 1)[-1].strip()
    extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    raw_stem = name.rsplit('.', 1)[0] if extension else name
    stem = re.sub(r'[\x00-\x1f\x7f/:*?"<>|\\]', '_', raw_stem)
    stem = re.sub(r'\s+', ' ', stem).strip(' .')[:60].rstrip(' .') or 'upload'
    safe_extension = re.sub(r'[^a-z0-9]', '', extension)[:10]
    return f'{stem}.{safe_extension}' if safe_extension else stem


def store():
    return current_app.extensions['projection_store']


def owned(document_id, owner):
    if not ID_PATTERN.fullmatch(document_id):
        raise ProjectionRejected('projection_not_found','投影文件不存在或已过期',404,0)
    meta = store().load(document_id, owner)
    if meta is None:
        raise ProjectionRejected('projection_not_found','投影文件不存在或已过期，请重新选择文件',404,0)
    return meta


@projection_bp.errorhandler(BillingRejected)
def identity_error(error):
    return jsonify({'error':str(error)}),error.status_code


@projection_bp.post('/documents')
def upload_document():
    from .projection_runtime import run
    from .projection_layout import discard_analysis_job
    owner = identify_user(request.headers.get('Authorization'))
    ledger = store()
    ledger.reconcile()
    rate_ip = upload_rate_limit_ip(request.remote_addr, request.headers.get('X-Forwarded-For'),
                                   current_app.config.get('PROJECTOR_TRUSTED_PROXY_CIDRS', ()))
    ledger.rate(owner, rate_ip, 'upload')
    if request.content_length is not None and request.content_length > MAX_BODY:
        raise ProjectionRejected('projection_upload_too_large','上传文件不能超过20MB',413,0)
    # One live pattern per account: a new upload retires the owner's previous
    # document before reserving, so stale sessions never keep capacity. A
    # retired document's queued analysis is removed from the RQ queue too.
    # complete_upload retires any document that finished parsing while this
    # upload was in flight and returns those queued analysis ids as well.
    for analysis_id in ledger.delete_all(owner):
        discard_analysis_job(analysis_id)
    lease = ledger.reserve(owner,'upload')
    folder = ledger.lease_path(lease)
    streams = []
    try:
        folder.mkdir(mode=0o700)
        path = folder/'source.pdf'
        g.projection_upload = {'path':path,'parts':0,'streams':streams}
        upload = request.files.get('file')
        if upload is None or not upload.filename:
            return jsonify(error='请选择 PDF、PLT 或 HPGL 文件'),400
        if len(request.files) != 1:
            return jsonify(error='每次只能选择一个 PDF、PLT 或 HPGL 文件'),400
        original_filename = request.form.get('original_filename') or upload.filename
        extension = Path(original_filename).suffix.lower()
        if extension not in ('.pdf', '.plt', '.hpgl'):
            return jsonify(error='只支持 .pdf、.plt 或 .hpgl 文件'),415
        is_plt = extension in ('.plt', '.hpgl')
        source_format = 'plt' if is_plt else 'pdf'
        if request.form.get('source_format') not in (None, '', source_format):
            return jsonify(error='文件格式标记与原始文件名不一致，请重新选择文件'),400
        upload.stream.flush()
        if path.stat().st_size > current_app.config['PLT_MAX_UPLOAD_BYTES']:
            raise ProjectionRejected('projection_upload_too_large','上传文件不能超过20MB',413,0)
        # Body is complete: take the bounded global parser slot now. A busy
        # parser raises here and the finally block releases the lease + file.
        if is_plt:
            ledger.reserve_derived(lease)
        ledger.begin_parse(lease)
        try:
            meta = run(ledger,lease,'plt' if is_plt else 'parse',path,
                       {'derived_max_bytes': ledger.policy.derived_max_bytes} if is_plt else None)
        except ProjectionRejected as error:
            # valid_lease stops a superseded upload's parse child with the generic
            # stale code; report the user-visible "a newer pattern won" reason.
            if error.code == 'projection_result_stale' and ledger.upload_superseded(lease):
                raise ProjectionRejected('projection_upload_superseded','已选择更新的纸样，本次上传已取消',409,0)
            raise
        meta.update(owner=owner,expires_at=time.time()+TTL_SECONDS,
                    filename=safe_uploaded_filename(original_filename))
        # Close every body writer before rename/release (Windows compatibility
        # and parse/413 failure FD accounting).
        upload.close()
        if is_plt:
            path.unlink()  # Original PLT is no longer needed after successful preparation.
            path = folder / 'derived.pdf'
        discarded = []
        document_id = ledger.complete_upload(lease,owner,path,meta,discarded)
        for analysis_id in discarded:
            discard_analysis_job(analysis_id)
        return jsonify({key:meta[key] for key in ('filename','page_count','pages','width_mm','height_mm','requires_metadata_choice')} |
                       {'document_id':document_id,'expires_in':TTL_SECONDS,'rotation_supported':True,
                        'session_summary_supported':True,'source_format':source_format,
                        'page_filter':meta.get('page_filter'),
                        **({'rendering_warnings':meta.get('rendering_warnings',[])} if is_plt else {})}),201
    finally:
        for stream in streams:
            stream.close()
        ledger.release(lease)
        g.pop('projection_upload',None)


def tile_parameters():
    values = {}
    for name in ('x_mm', 'y_mm', 'width_mm', 'height_mm', 'width_px', 'height_px'):
        try:
            value = float(request.args[name])
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError(f'{name} 参数无效') from error
        if not math.isfinite(value):
            raise ValueError(f'{name} 必须是有限数值')
        if name not in ('x_mm', 'y_mm') and value <= 0:
            raise ValueError(f'{name} 必须大于 0')
        if name.endswith('_px') and (not value.is_integer() or value > 2048):
            raise ValueError('像素尺寸必须为 1 至 2048 的整数')
        values[name] = int(value) if name.endswith('_px') else value
    if values['width_px'] * values['height_px'] > 4_000_000:
        raise ValueError('瓦片不能超过 400 万像素')
    scale = values['width_px'] / values['width_mm']
    if not math.isfinite(scale) or abs(values['height_mm'] * scale - values['height_px']) > 1.000001:
        raise ValueError('毫米与像素的宽高比例必须一致（允许 1 像素取整误差）')
    # MuPDF rectangles and matrices use float32. Reject unrepresentable inputs
    # instead of letting their conversion silently yield an infinite coordinate.
    coordinates = [values[name] * PT_PER_MM for name in ('x_mm', 'y_mm', 'width_mm', 'height_mm')]
    coordinates += [coordinates[0] + coordinates[2], coordinates[1] + coordinates[3], scale / PT_PER_MM]
    if any(not math.isfinite(value) or abs(value) > 3.4e38 for value in coordinates):
        raise ValueError('投影坐标超出 PDF 渲染器数值范围')
    values['rotation_deg'] = normalize_rotation(request.args.get('rotation_deg', '0'))
    return values


ROTATION_BLOCK_PX = 256
ROTATION_HALO_PX = 2
FLOAT32_MAX = 3.4e38


def normalize_rotation(value):
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or not math.isfinite(float(number))
                or number != number.to_integral_value()):
            raise ValueError()
        return int(number) % 360
    except (InvalidOperation, ValueError, TypeError, OverflowError) as error:
        raise ValueError('旋转角度必须是有限整数（1°步进，支持360°循环）') from error


def rotation_components(angle):
    # Cardinal angles must not inherit floating-point sin/cos residue.
    cardinal = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}
    if angle in cardinal:
        return cardinal[angle]
    radians = math.radians(angle)
    return math.cos(radians), math.sin(radians)


def _finite_rotation_values(values):
    if any(not math.isfinite(value) or abs(value) > FLOAT32_MAX for value in values):
        raise ValueError('旋转投影坐标超出 PDF 渲染器数值范围')


def _source_over_block(canvas, output_width, pixmap, block):
    """Composite MuPDF premultiplied RGBA without installing image libraries.

    Work only on the owned output block. Transparent rotated bounding-box
    triangles must never erase a previous page, and opaque rows use C slices.
    """
    output_height = len(canvas) // (output_width * 3)
    left = max(block[0], pixmap.x, 0)
    top = max(block[1], pixmap.y, 0)
    right = min(block[2], pixmap.x + pixmap.width, output_width)
    bottom = min(block[3], pixmap.y + pixmap.height, output_height)
    if right <= left or bottom <= top:
        return
    samples = pixmap.samples
    count = right - left
    for y in range(top, bottom):
        source_start = ((y - pixmap.y) * pixmap.width + left - pixmap.x) * 4
        source_end = source_start + count * 4
        target_start = (y * output_width + left) * 3
        target_end = target_start + count * 3
        alphas = samples[source_start + 3:source_end:4]
        if alphas.count(0) == count:
            continue
        if alphas.count(255) == count:
            for channel in range(3):
                canvas[target_start + channel:target_end:3] = samples[source_start + channel:source_end:4]
            continue
        for offset, alpha in enumerate(alphas):
            if alpha == 0:
                continue
            source_pixel = source_start + offset * 4
            target_pixel = target_start + offset * 3
            if alpha == 255:
                canvas[target_pixel:target_pixel + 3] = samples[source_pixel:source_pixel + 3]
            else:
                inverse_alpha = 255 - alpha
                for channel in range(3):
                    value = samples[source_pixel + channel] + (canvas[target_pixel + channel] * inverse_alpha + 127) // 255
                    canvas[target_pixel + channel] = min(255, max(0, value))


def _opaque_copy(canvas, output_width, pixmap):
    """Blit an opaque RGB pixmap at its own origin onto a bytearray canvas.

    Same pixels as ``fitz.Pixmap.copy`` (which a bytearray cannot host), but
    reused so the finished white canvas stays a single shared surface.
    """
    output_height = len(canvas) // (output_width * 3)
    left = max(0, pixmap.x)
    top = max(0, pixmap.y)
    right = min(output_width, pixmap.x + pixmap.width)
    bottom = min(output_height, pixmap.y + pixmap.height)
    if right <= left or bottom <= top:
        return
    samples = pixmap.samples
    count = right - left
    for y in range(top, bottom):
        source_start = ((y - pixmap.y) * pixmap.width + left - pixmap.x) * 3
        target_start = (y * output_width + left) * 3
        canvas[target_start:target_start + count * 3] = samples[source_start:source_start + count * 3]


def _clamp_rect(rect, output_width, output_height):
    """Intersect ``rect`` with the owned canvas, or ``None`` when disjoint.

    MuPDF rectangle origins are float32 and a placement landing on the canvas
    edge can round one pixel outside it. Every consumer of a canvas rectangle
    goes through here so an out-of-bounds strip is dropped instead of writing
    into the neighbouring row / growing the bytearray.
    """
    left = max(0, min(rect[0], output_width))
    top = max(0, min(rect[1], output_height))
    right = max(0, min(rect[2], output_width))
    bottom = max(0, min(rect[3], output_height))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _overlap_cells(bounds, painted, output_width, output_height):
    """Disjoint canvas rectangles where ``bounds`` shares pixels with painted pages.

    Splitting the union along every neighbour edge keeps the strips disjoint, so
    a page is never transparently composited over the same pixel twice even when
    it touches two neighbours at a fractional corner. Every strip is clipped to
    the canvas and empty strips are dropped.
    """
    if not painted:
        return []
    x_values = {bounds[0], bounds[2]}
    y_values = {bounds[1], bounds[3]}
    for other in painted:
        if bounds[0] < other[0] < bounds[2]:
            x_values.add(other[0])
        if bounds[0] < other[2] < bounds[2]:
            x_values.add(other[2])
        if bounds[1] < other[1] < bounds[3]:
            y_values.add(other[1])
        if bounds[1] < other[3] < bounds[3]:
            y_values.add(other[3])
    xs, ys = sorted(x_values), sorted(y_values)
    cells = []
    for column in range(len(xs) - 1):
        for row in range(len(ys) - 1):
            cell = (xs[column], ys[row], xs[column + 1], ys[row + 1])
            if any(cell[0] < other[2] and other[0] < cell[2]
                   and cell[1] < other[3] and other[1] < cell[3] for other in painted):
                clipped = _clamp_rect(cell, output_width, output_height)
                if clipped is not None:
                    cells.append(clipped)
    return cells


def _read_region(canvas, output_width, rect):
    output_height = len(canvas) // (output_width * 3)
    clipped = _clamp_rect(rect, output_width, output_height)
    if clipped is None:
        return []
    left, top, right, bottom = clipped
    width = (right - left) * 3
    return [bytes(canvas[(y * output_width + left) * 3:(y * output_width + left) * 3 + width])
            for y in range(top, bottom)]


def _write_region(canvas, output_width, rect, rows):
    output_height = len(canvas) // (output_width * 3)
    clipped = _clamp_rect(rect, output_width, output_height)
    if clipped is None:
        return
    left, top, right, bottom = clipped
    width = (right - left) * 3
    for offset in range(bottom - top):
        data = rows[offset]
        if len(data) < width:
            continue
        start = ((top + offset) * output_width + left) * 3
        canvas[start:start + width] = data[:width]


def render_rotated_tile(source, values, page_index=0, layout=None):
    fitz = load_fitz()
    width_px, height_px = values['width_px'], values['height_px']
    canvas = bytearray(b'\xff') * (width_px * height_px * 3)
    view_x, view_y = values['x_mm'] * PT_PER_MM, values['y_mm'] * PT_PER_MM
    scale = width_px / (values['width_mm'] * PT_PER_MM)
    cosine, sine = rotation_components(values['rotation_deg'])
    _finite_rotation_values((view_x, view_y, scale))
    with fitz.open(stream=source, filetype='pdf') as document:
        if layout is None:
            if not 0 <= page_index < document.page_count:
                raise ValueError('页码超出 PDF 范围')
            page = document[page_index]
            placements = [{'page_index': page_index, 'x_mm': 0, 'y_mm': 0,
                           'width_mm': page.rect.width / PT_PER_MM,
                           'height_mm': page.rect.height / PT_PER_MM}]
            crop = {'left': 0, 'top': 0}
        else:
            placements = layout['placements']
            crop = layout['crop_margins_mm']
        crop_left, crop_top = crop['left'] * PT_PER_MM, crop['top'] * PT_PER_MM
        display_lists = {}
        for block_top in range(0, height_px, ROTATION_BLOCK_PX):
            for block_left in range(0, width_px, ROTATION_BLOCK_PX):
                block = (block_left, block_top, min(width_px, block_left + ROTATION_BLOCK_PX),
                         min(height_px, block_top + ROTATION_BLOCK_PX))
                corners = []
                for px, py in ((block[0] - ROTATION_HALO_PX, block[1] - ROTATION_HALO_PX),
                               (block[2] + ROTATION_HALO_PX, block[1] - ROTATION_HALO_PX),
                               (block[0] - ROTATION_HALO_PX, block[3] + ROTATION_HALO_PX),
                               (block[2] + ROTATION_HALO_PX, block[3] + ROTATION_HALO_PX)):
                    qx, qy = view_x + px / scale, view_y + py / scale
                    corners.append((cosine * qx + sine * qy, -sine * qx + cosine * qy))
                _finite_rotation_values(coordinate for point in corners for coordinate in point)
                world_left = min(point[0] for point in corners)
                world_right = max(point[0] for point in corners)
                world_top = min(point[1] for point in corners)
                world_bottom = max(point[1] for point in corners)
                for placement in placements:
                    crop_left = placement.get('source_x_mm',crop['left']) * PT_PER_MM
                    crop_top = placement.get('source_y_mm',crop['top']) * PT_PER_MM
                    left, top = placement['x_mm'] * PT_PER_MM, placement['y_mm'] * PT_PER_MM
                    right = left + placement['width_mm'] * PT_PER_MM
                    bottom = top + placement['height_mm'] * PT_PER_MM
                    if world_left >= right or world_right <= left or world_top >= bottom or world_bottom <= top:
                        continue
                    clip_coordinates = (crop_left + max(world_left, left) - left,
                                        crop_top + max(world_top, top) - top,
                                        crop_left + min(world_right, right) - left,
                                        crop_top + min(world_bottom, bottom) - top)
                    local_x, local_y = left - crop_left, top - crop_top
                    matrix_values = (cosine * scale, sine * scale, -sine * scale, cosine * scale,
                                     scale * (cosine * local_x - sine * local_y - view_x),
                                     scale * (sine * local_x + cosine * local_y - view_y))
                    _finite_rotation_values((*clip_coordinates, *matrix_values))
                    index = placement['page_index']
                    if index not in display_lists:
                        display_lists[index] = document[index].get_displaylist(annots=True)
                    # Check the predicted allocation before entering MuPDF,
                    # not only after an unexpectedly large pixmap is allocated.
                    transformed = [(matrix_values[0] * px + matrix_values[2] * py + matrix_values[4],
                                    matrix_values[1] * px + matrix_values[3] * py + matrix_values[5])
                                   for px, py in ((clip_coordinates[0], clip_coordinates[1]),
                                                  (clip_coordinates[2], clip_coordinates[1]),
                                                  (clip_coordinates[0], clip_coordinates[3]),
                                                  (clip_coordinates[2], clip_coordinates[3]))]
                    _finite_rotation_values(coordinate for point in transformed for coordinate in point)
                    maximum_side = 2 * (ROTATION_BLOCK_PX + 2 * ROTATION_HALO_PX) + 4
                    predicted_width = math.ceil(max(point[0] for point in transformed)) - math.floor(min(point[0] for point in transformed))
                    predicted_height = math.ceil(max(point[1] for point in transformed)) - math.floor(min(point[1] for point in transformed))
                    if predicted_width > maximum_side or predicted_height > maximum_side:
                        raise ValueError('旋转投影中间图块超过有限渲染预算')
                    pixmap = display_lists[index].get_pixmap(matrix=fitz.Matrix(*matrix_values),
                                clip=fitz.Rect(*clip_coordinates), colorspace=fitz.csRGB, alpha=True)
                    # The inverse block AABB transformed forward is at most
                    # twice the halo block side; rounding may add two pixels.
                    maximum_side = 2 * (ROTATION_BLOCK_PX + 2 * ROTATION_HALO_PX) + 4
                    if pixmap.width > maximum_side or pixmap.height > maximum_side:
                        raise ValueError('旋转投影中间图块超过有限渲染预算')
                    _source_over_block(canvas, width_px, pixmap, block)
    return fitz.Pixmap(fitz.csRGB, width_px, height_px, bytes(canvas), False).tobytes('png')


def render_tile(source, values, page_index=0, layout=None):
    rotation = normalize_rotation(values.get('rotation_deg', 0))
    if rotation:
        return render_rotated_tile(source, {**values, 'rotation_deg': rotation}, page_index, layout)
    fitz = load_fitz()
    width_px, height_px = values['width_px'], values['height_px']
    # White belongs to the finished canvas only. A placement that shares pixels
    # with an already-painted page (a page boundary landing on a fractional
    # pixel) is composited transparently instead, so its white background can no
    # longer overwrite the neighbour's edge column. A placement that shares no
    # pixels keeps MuPDF's exact opaque output.
    canvas = bytearray(b'\xff') * (width_px * height_px * 3)
    x, y = values['x_mm'] * PT_PER_MM, values['y_mm'] * PT_PER_MM
    width, height = values['width_mm'] * PT_PER_MM, values['height_mm'] * PT_PER_MM
    scale = values['width_px'] / width
    with fitz.open(stream=source, filetype='pdf') as document:
        if layout is None:
            if not 0 <= page_index < document.page_count:
                raise ValueError('页码超出 PDF 范围')
            page = document[page_index]
            placements = [{'page_index': page_index, 'x_mm': 0, 'y_mm': 0,
                           'width_mm': page.rect.width / PT_PER_MM,
                           'height_mm': page.rect.height / PT_PER_MM}]
            crop = {'left': 0, 'top': 0}
        else:
            placements = layout['placements']
            crop = layout['crop_margins_mm']
        painted = []
        for placement in placements:
            left, top = placement['x_mm'] * PT_PER_MM, placement['y_mm'] * PT_PER_MM
            right = left + placement['width_mm'] * PT_PER_MM
            bottom = top + placement['height_mm'] * PT_PER_MM
            if x >= right or y >= bottom or x + width <= left or y + height <= top:
                continue
            page = document[placement['page_index']]
            crop_left = placement.get('source_x_mm',crop['left']) * PT_PER_MM
            crop_top = placement.get('source_y_mm',crop['top']) * PT_PER_MM
            clip = fitz.Rect(crop_left + max(x, left) - left,
                             crop_top + max(y, top) - top,
                             crop_left + min(x + width, right) - left,
                             crop_top + min(y + height, bottom) - top)
            matrix = fitz.Matrix(scale, 0, 0, scale,
                                 (left - crop_left - x) * scale,
                                 (top - crop_top - y) * scale)
            pixmap = page.get_pixmap(matrix=matrix, clip=clip, colorspace=fitz.csRGB,
                                    alpha=False, annots=True)
            bounds = (pixmap.x, pixmap.y, pixmap.x + pixmap.width, pixmap.y + pixmap.height)
            cells = _overlap_cells(bounds, painted, width_px, height_px)
            if cells:
                # Only the thin strips shared with already-painted pages need
                # transparent compositing. Keep their bytes, paste the page
                # opaquely, restore the strips and blend this page over them, so
                # a large tile never runs the per-pixel path for a whole page.
                saved = [(cell, _read_region(canvas, width_px, cell)) for cell in cells]
                _opaque_copy(canvas, width_px, pixmap)
                for cell, rows in saved:
                    _write_region(canvas, width_px, cell, rows)
                transparent = page.get_pixmap(matrix=matrix, clip=clip, colorspace=fitz.csRGB,
                                              alpha=True, annots=True)
                for cell in cells:
                    _source_over_block(canvas, width_px, transparent, cell)
            else:
                _opaque_copy(canvas, width_px, pixmap)
            painted.append(bounds)
    if len(canvas) != width_px * height_px * 3:
        raise RuntimeError('投影画布尺寸异常')
    return fitz.Pixmap(fitz.csRGB, width_px, height_px, bytes(canvas), False).tobytes('png')


@projection_bp.get('/documents/<document_id>')
def session_summary(document_id):
    from .projection_layout import public_layout, analysis_response
    owner = identify_user(request.headers.get('Authorization'))
    meta = owned(document_id,owner)
    return jsonify(source_format=meta.get('source_format','pdf'),
                   **({'rendering_warnings':meta.get('rendering_warnings',[])} if meta.get('source_format') == 'plt' else {}),
                   document_id=document_id,pages=meta['pages'],page_count=meta['page_count'],
                   page_filter=meta.get('page_filter'),analysis=analysis_response(meta.get('analysis') or {}),
                   layout=public_layout(meta['layout']) if meta.get('layout') else None,
                   rotation_supported=True,session_summary_supported=True,
                   expires_in=max(0,math.ceil(meta['expires_at']-time.time())))


@projection_bp.get('/documents/<document_id>/tile')
def get_tile(document_id):
    from .projection_runtime import run
    owner = identify_user(request.headers.get('Authorization'))
    meta = owned(document_id,owner)
    view = request.args.get('view','page')
    if view not in ('page','layout'):
        return jsonify(error='显示方式只支持 page 或 layout'),400
    layout = meta.get('layout') if view == 'layout' else None
    if view == 'layout' and layout is None:
        return jsonify(error='尚未应用拼版，原分页仍可显示'),409
    try:
        values = tile_parameters()
        page_index = int(request.args.get('page_index','0'))
        if not 0 <= page_index < meta['page_count']:
            raise ValueError('页码超出 PDF 范围')
    except (ValueError,TypeError) as error:
        return jsonify(error=str(error)),400
    ledger = store()
    ledger.rate(owner,'','tile')
    lease = ledger.reserve(owner,'tile',document_id,meta['_generation'])
    try:
        png = run(ledger,lease,'tile',ledger.source_path(document_id),
                  {'values':values,'page_index':page_index,'layout':layout})
        # Final publication version check. Renewals preserve the generation;
        # deletion/layout changes invalidate it without waiting for renderer.
        if not ledger.valid_lease(lease):
            raise ProjectionRejected('projection_result_stale','投影会话或排版已变化，请加载最新画面',409,0)
        response = Response(png,mimetype='image/png')
        response.headers['X-Projection-Rotation'] = str(values['rotation_deg'])
        return response
    finally:
        ledger.release(lease)


@projection_bp.post('/documents/<document_id>/renew')
def renew_document(document_id):
    owner = identify_user(request.headers.get('Authorization'))
    if not ID_PATTERN.fullmatch(document_id) or not store().renew(document_id,owner):
        raise ProjectionRejected('projection_not_found','投影文件不存在或已过期',404,0)
    return jsonify(document_id=document_id,expires_in=TTL_SECONDS,rotation_supported=True,session_summary_supported=True)


@projection_bp.delete('/documents/<document_id>')
def close_document(document_id):
    from .projection_layout import discard_analysis_job
    owner = identify_user(request.headers.get('Authorization'))
    meta = owned(document_id,owner)
    analysis = meta.get('analysis') or {}
    analysis_id = analysis.get('analysis_id') if analysis.get('status') == 'analyzing' else None
    if not store().delete(document_id,owner):
        raise ProjectionRejected('projection_not_found','投影文件不存在或已过期',404,0)
    if analysis_id:
        discard_analysis_job(analysis_id)
    return Response(status=204)


@projection_bp.route('/documents/<document_id>/layout-analysis',methods=['POST','GET'])
def layout_analysis(document_id):
    from .projection_layout import resolve_metadata_mode, submit_analysis, refresh_analysis_failure
    owner = identify_user(request.headers.get('Authorization'))
    meta = owned(document_id,owner)
    if meta.get('source_format') == 'plt':
        return jsonify(error='PLT 投影已是单页图形，无需排版分析', error_code='projection_plt_layout_not_needed'),409
    if request.method == 'GET':
        if not meta.get('analysis'):
            return jsonify(error='尚未提交排版分析'),409
        return jsonify(refresh_analysis_failure(store(),document_id,owner,meta))
    try:
        mode = resolve_metadata_mode(meta,request.form.get('metadata_mode'))
        from .projection_page_filter import normalize_selection
        indexes = normalize_selection(request.form.get('included_page_indexes'),meta['page_count'])
    except ValueError as error:
        return jsonify(error=str(error)),422
    result = submit_analysis(store(),document_id,owner,meta,mode,indexes)
    return jsonify(result),202 if result['status'] == 'analyzing' else 200


@projection_bp.post('/documents/<document_id>/layout')
def apply_layout(document_id):
    from .projection_layout import build_layout, public_layout
    owner = identify_user(request.headers.get('Authorization'))
    meta = owned(document_id,owner)
    from .projection_page_filter import normalize_selection, selection_details
    try:
        indexes = normalize_selection(request.form.get('included_page_indexes'),meta['page_count'])
    except ValueError as error:
        return jsonify(error=str(error)),400
    if meta.get('source_format') == 'plt' and indexes != list(range(meta['page_count'])):
        return jsonify(error='PLT 投影保留完整图形'),400
    method = request.form.get('method')
    if method == 'auto':
        analysis = meta.get('analysis') or {}
        if analysis.get('status') != 'ready' or not request.form.get('analysis_id') or request.form['analysis_id'] != analysis.get('analysis_id'):
            return jsonify(error='排版建议已变化或尚未就绪，请重新分析'),409
        if indexes != normalize_selection(analysis.get('included_page_indexes'),meta['page_count']):
            return jsonify(error='参与拼版页已变化，请重新分析'),409
        options = analysis.get('suggestion') or {}
        if options.get('confidence') not in ('medium','high'):
            return jsonify(error='没有可靠排版建议，原分页保留'),409
    elif method == 'manual':
        options = {'rows':request.form.get('rows'),'columns':request.form.get('columns'),
                   'order':request.form.get('order','row'),'source':'manual',
                   'crop_margins_mm':{side:request.form.get(f'crop_{side}_mm',0) for side in ('left','right','top','bottom')}}
    else:
        return jsonify(error='排版方式只支持 auto 或 manual'),400
    try:
        layout = build_layout(meta['pages'],options.get('rows'),options.get('columns'),options.get('order'),
                              options.get('crop_margins_mm') or {},options.get('page_slots'),options.get('source') or method,indexes,options.get('source_rects_mm'))
        layout.update(selection_details(meta,indexes))
    except ValueError as error:
        return jsonify(error=str(error)),400
    expected_analysis_id = (meta.get('analysis') or {}).get('analysis_id')
    meta['layout'] = layout
    prior_analysis = None
    if method == 'manual':
        prior_analysis = meta.pop('analysis',None)
    if not store().update(document_id,owner,meta,meta['_generation'],invalidate=True,
                          expected_analysis_id=expected_analysis_id):
        raise ProjectionRejected('projection_result_stale','投影排版或会话已变化，请重试',409,0)
    # The manual layout replaced any pending analysis, and the update above
    # invalidated the document generation. Only now is it safe to retire the
    # prior job and lease: a 409 means a concurrent writer owns the document
    # and its analysis state must be left untouched.
    if method == 'manual' and prior_analysis:
        from .projection_layout import discard_analysis_job
        analysis_id = prior_analysis.get('analysis_id') if prior_analysis.get('status') == 'analyzing' else None
        if analysis_id:
            discard_analysis_job(analysis_id)
        lease = prior_analysis.get('lease')
        if lease:
            # A live analysis child polls valid_lease and aborts on the bumped
            # generation; release is a no-op for it and reclaims an idle lease.
            store().release(lease)
    return jsonify(layout=public_layout(layout),width_mm=layout['width_mm'],height_mm=layout['height_mm'])
