import os
from pathlib import Path


MAX_PDF_PAGES = 200
PREVIEW_WIDTH_PX = 360
RASTER_DPI = 96
MAX_RASTER_SEGMENTS = 60000


def inspect_pdf(source, preview_folder, preview_id):
    """Read PDF pages and create small preview images for the mobile client."""
    fitz = load_fitz()
    if not source:
        raise ValueError('PDF 文件为空')

    document = open_pdf_document(source, fitz)
    try:
        if document.page_count > MAX_PDF_PAGES:
            raise ValueError(f'PDF 页数过多，最多支持 {MAX_PDF_PAGES} 页')
        if document.page_count == 0:
            raise ValueError('PDF 没有可用页面')
        validate_pdf_complexity(document)
        pages = []
        for index in range(document.page_count):
            page = document.load_page(index)
            rect = page.rect
            scale = min(1, PREVIEW_WIDTH_PX / max(rect.width, 1))
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            preview_path = Path(preview_folder) / f'{preview_id}-{index}.png'
            pixmap.save(str(preview_path))
            pages.append({
                'index': index,
                'label': f'{index + 1}',
                'width_mm': round(rect.width * 25.4 / 72, 2),
                'height_mm': round(rect.height * 25.4 / 72, 2),
                'preview_name': preview_path.name,
            })
        return pages
    finally:
        document.close()


def convert_pdf_to_plt(source, options=None):
    """Convert vector PDF paths, or raster page lines as a best-effort fallback, to HPGL."""
    options = options or {}
    fitz = load_fitz()
    units_per_inch = positive_int(options.get('units_per_inch', 1016), 'units_per_inch', 100000)
    line_width_mm = max(0.03, float(options.get('line_width_mm', 0.265)))
    margin_mm = clamp(float(options.get('margin_mm', 0)), 0, 100)

    document = open_pdf_document(source, fitz)
    try:
        if document.page_count > MAX_PDF_PAGES:
            raise ValueError(f'PDF 页数过多，最多支持 {MAX_PDF_PAGES} 页')
        validate_pdf_complexity(document)
        pages = []
        total_segments = 0
        maximum_segments = max(1000, int(os.getenv('PDF_MAX_OUTPUT_SEGMENTS', '300000')))
        for index in range(document.page_count):
            page = _extract_page(
                document.load_page(index),
                units_per_inch,
                fitz,
                maximum_segments - total_segments,
            )
            total_segments += sum(max(len(shape) - 1, 0) for shape in page['shapes'])
            if total_segments > maximum_segments:
                raise ValueError(f'PDF 线条数量过多，最多支持 {maximum_segments} 条')
            pages.append(page)
    finally:
        document.close()

    if not pages:
        raise ValueError('PDF 没有可转换的页面')
    rows = positive_int(options.get('rows', 1), 'rows')
    columns = positive_int(options.get('columns', 1), 'columns')
    order = options.get('order', 'row') if options.get('order', 'row') in {'row', 'column'} else 'row'
    page_slots = options.get('page_slots')
    enabled_pages = options.get('enabled_pages')
    capacity = rows * columns
    if page_slots is not None:
        if len(page_slots) > capacity:
            raise ValueError('page_slots 数量不能超过行列总格数')
        selected_slots = [int(value) for value in page_slots if value is not None]
        if any(value < 0 or value >= len(pages) for value in selected_slots):
            raise ValueError('page_slots 包含无效页面')
        if len(selected_slots) != len(set(selected_slots)):
            raise ValueError('page_slots 不能重复使用同一页面')
        placements = [
            (slot_index, int(page_index))
            for slot_index, page_index in enumerate(page_slots)
            if page_index is not None
        ]
        enabled_pages = [page_index for _, page_index in placements]
    else:
        if enabled_pages is None:
            enabled_pages = list(range(len(pages)))
        enabled_pages = [int(index) for index in enabled_pages]
        if any(index < 0 or index >= len(pages) for index in enabled_pages):
            raise ValueError('enabled_pages 包含无效页面')
        enabled_pages = list(dict.fromkeys(enabled_pages))
        if len(enabled_pages) > capacity:
            raise ValueError('所选页面数量不能超过行列总格数')
        placements = [
            (output_index, page_index)
            for output_index, page_index in enumerate(enabled_pages)
        ]
    if not enabled_pages:
        raise ValueError('至少保留一个 PDF 页面')

    tile_width = max(page['width_units'] for page in pages)
    tile_height = max(page['height_units'] for page in pages)
    gap_units = round(margin_mm * units_per_inch / 25.4)
    output_paths = []
    output_width = columns * tile_width + max(columns - 1, 0) * gap_units
    output_height = rows * tile_height + max(rows - 1, 0) * gap_units
    for slot_index, page_index in placements:
        cell = page_index_to_cell(slot_index, rows, columns, order)
        if cell is None:
            continue
        row, column = cell
        offset_x = column * (tile_width + gap_units)
        offset_y = (rows - row - 1) * (tile_height + gap_units)
        for shape in pages[page_index]['shapes']:
            output_paths.append(offset_shape(shape, offset_x, offset_y))

    if not output_paths:
        raise ValueError('PDF 页面没有可转换的线条内容')
    plt = serialize_hpgl(output_paths, units_per_inch, line_width_mm)
    return plt, {
        'page_count': len(pages),
        'selected_count': len(enabled_pages),
        'empty_cells': max(rows * columns - len(enabled_pages), 0),
        'rows': rows,
        'columns': columns,
        'order': order,
        'margin_mm': margin_mm,
        'width_mm': round(output_width * 25.4 / units_per_inch, 2),
        'height_mm': round(output_height * 25.4 / units_per_inch, 2),
        'source_types': sorted({page['source_type'] for page in pages}),
        'raster_fallback': any(page['source_type'] == 'raster' for page in pages),
    }


def load_fitz():
    try:
        import pymupdf as fitz
    except ImportError as error:
        try:
            import fitz
        except ImportError:
            raise RuntimeError('PDF 转换服务缺少 PyMuPDF 依赖') from error
    return fitz


def open_pdf_document(source, fitz):
    try:
        return fitz.open(stream=source, filetype='pdf')
    except Exception as error:
        file_data_error = getattr(fitz, 'FileDataError', None)
        if file_data_error and isinstance(error, file_data_error):
            raise ValueError('PDF 文件无法读取或已损坏') from error
        raise


def validate_pdf_complexity(document):
    maximum_pixels = max(1_000_000, int(os.getenv('PDF_MAX_TOTAL_PIXELS', '200000000')))
    maximum_drawings = max(1000, int(os.getenv('PDF_MAX_DRAWINGS', '250000')))
    total_pixels = 0
    total_drawings = 0
    for index in range(document.page_count):
        page = document.load_page(index)
        total_pixels += max(1, round(page.rect.width)) * max(1, round(page.rect.height))
        if total_pixels > maximum_pixels:
            raise ValueError('PDF 页面总像素量超过服务器限制')
        total_drawings += len(page.get_drawings())
        if total_drawings > maximum_drawings:
            raise ValueError('PDF 矢量路径数量超过服务器限制')


def _extract_page(page, units_per_inch, fitz, remaining_segments):
    page_width = page.rect.width * units_per_inch / 72
    page_height = page.rect.height * units_per_inch / 72
    shapes = []
    for drawing in page.get_drawings():
        for item in drawing.get('items', []):
            points = drawing_item_points(item, fitz)
            for visible_points in clip_page_polyline(points, page.rect.width, page.rect.height, fitz):
                if len(visible_points) - 1 > remaining_segments:
                    raise ValueError('PDF 线条数量超过服务器限制')
                shapes.append([
                    {
                        'x': point.x * units_per_inch / 72,
                        'y': (page.rect.height - point.y) * units_per_inch / 72,
                    }
                    for point in visible_points
                ])
                remaining_segments -= len(visible_points) - 1
    if shapes:
        return {
            'width_units': page_width,
            'height_units': page_height,
            'shapes': shapes,
            'source_type': 'vector',
        }
    return {
        'width_units': page_width,
        'height_units': page_height,
        'shapes': rasterize_page(page, units_per_inch, fitz, remaining_segments),
        'source_type': 'raster',
    }


def drawing_item_points(item, fitz):
    kind = item[0]
    if kind == 'l':
        return [item[1], item[2]]
    if kind == 're':
        rect = item[1]
        return [rect.tl, rect.tr, rect.br, rect.bl, rect.tl]
    if kind == 'qu':
        quad = item[1]
        return [quad.ul, quad.ur, quad.lr, quad.ll, quad.ul]
    if kind == 'c':
        return cubic_bezier(item[1], item[2], item[3], item[4], fitz)
    return []


def clip_page_polyline(points, width, height, fitz):
    if len(points) < 2:
        return []
    paths = []
    current = []
    for start, end in zip(points, points[1:]):
        clipped = clip_page_segment(start, end, width, height, fitz)
        if not clipped:
            if len(current) > 1:
                paths.append(current)
            current = []
            continue
        clipped_start, clipped_end = clipped
        if not current or not fitz_points_equal(current[-1], clipped_start):
            if len(current) > 1:
                paths.append(current)
            current = [clipped_start]
        current.append(clipped_end)
    if len(current) > 1:
        paths.append(current)
    return paths


def clip_page_segment(start, end, width, height, fitz):
    x1, y1 = start.x, start.y
    x2, y2 = end.x, end.y
    dx = x2 - x1
    dy = y2 - y1
    lower = 0.0
    upper = 1.0
    for direction, distance in (
        (-dx, x1),
        (dx, width - x1),
        (-dy, y1),
        (dy, height - y1),
    ):
        if abs(direction) < 1e-12:
            if distance < 0:
                return None
            continue
        ratio = distance / direction
        if direction < 0:
            if ratio > upper:
                return None
            lower = max(lower, ratio)
        else:
            if ratio < lower:
                return None
            upper = min(upper, ratio)
    return (
        fitz.Point(x1 + lower * dx, y1 + lower * dy),
        fitz.Point(x1 + upper * dx, y1 + upper * dy),
    )


def fitz_points_equal(first, second):
    return abs(first.x - second.x) < 1e-7 and abs(first.y - second.y) < 1e-7


def cubic_bezier(start, control_a, control_b, end, fitz, steps=12):
    points = []
    for index in range(steps + 1):
        t = index / steps
        inverse = 1 - t
        points.append(fitz.Point(
            inverse ** 3 * start.x
            + 3 * inverse ** 2 * t * control_a.x
            + 3 * inverse * t ** 2 * control_b.x
            + t ** 3 * end.x,
            inverse ** 3 * start.y
            + 3 * inverse ** 2 * t * control_a.y
            + 3 * inverse * t ** 2 * control_b.y
            + t ** 3 * end.y,
        ))
    return points


def rasterize_page(page, units_per_inch, fitz, remaining_segments=MAX_RASTER_SEGMENTS):
    scale = RASTER_DPI / 72
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    channels = pixmap.n
    samples = pixmap.samples
    shapes = []
    pixel_units = units_per_inch / RASTER_DPI
    for y in range(pixmap.height):
        run_start = None
        row_offset = y * pixmap.width * channels
        for x in range(pixmap.width + 1):
            dark = False
            if x < pixmap.width:
                offset = row_offset + x * channels
                red, green, blue = samples[offset:offset + 3]
                dark = (red * 299 + green * 587 + blue * 114) / 1000 < 210
            if dark and run_start is None:
                run_start = x
            elif not dark and run_start is not None:
                if x - run_start >= 2:
                    shapes.append([
                        {'x': run_start * pixel_units, 'y': (pixmap.height - y) * pixel_units},
                        {'x': x * pixel_units, 'y': (pixmap.height - y) * pixel_units},
                    ])
                run_start = None
                if len(shapes) > min(MAX_RASTER_SEGMENTS, remaining_segments):
                    raise ValueError('PDF 线条数量超过服务器限制')
    return shapes


def page_index_to_cell(page_index, rows, columns, order):
    capacity = rows * columns
    if page_index >= capacity:
        return None
    if order == 'column':
        return page_index % rows, page_index // rows
    return page_index // columns, page_index % columns


def offset_shape(shape, offset_x, offset_y):
    return [
        {'x': round(point['x'] + offset_x), 'y': round(point['y'] + offset_y)}
        for point in shape
    ]


def serialize_hpgl(shapes, units_per_inch, line_width_mm):
    commands = ['IN;', 'SP1;']
    width_units = max(1, round(line_width_mm * units_per_inch / 25.4))
    commands.append(f'PW{width_units};')
    for shape in shapes:
        if len(shape) < 2:
            continue
        commands.append(f"PU{format_point(shape[0])};")
        commands.append('PD' + ','.join(format_point(point) for point in shape) + ';')
        commands.append('PU;')
    commands.append('SP0;')
    return ''.join(commands).encode('ascii')


def format_point(point):
    return f"{int(round(point['x']))},{int(round(point['y']))}"


def positive_int(value, name, maximum=24):
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name} 参数无效') from error
    if result <= 0 or result > maximum:
        raise ValueError(f'{name} 必须在 1～{maximum} 之间')
    return result


def clamp(value, minimum, maximum):
    return min(max(value, minimum), maximum)
