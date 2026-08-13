import math
import os
import zlib


MM_TO_PT = 72 / 25.4
PAPER_SIZES_MM = {
    'A0': (841, 1189),
    'A1': (594, 841),
    'A2': (420, 594),
    'A3': (297, 420),
    'A4': (210, 297),
    'LETTER': (216, 279),
}


def render_pdf(document, options=None):
    options = options or {}
    metrics = document['metrics']
    shapes = document['shapes']
    margin_mm = max(0, float(options.get('margin_mm', 10)))
    line_width_mm = max(0.03, float(options.get('line_width_mm', 0.265)))
    paper_size = str(options.get('paper_size', 'A4')).upper()
    orientation = str(options.get('orientation', 'portrait')).lower()
    single_page = bool(options.get('single_page_output', False))
    show_page_number = bool(options.get('show_page_number', True))
    enabled_pages = options.get('enabled_pages')
    maximum_pages = max(1, int(os.getenv('PLT_MAX_OUTPUT_PAGES', '80')))
    units_per_inch = float(metrics['units_per_inch'])
    scale = MM_TO_PT / 25.4 * 25.4 / units_per_inch

    if paper_size not in PAPER_SIZES_MM:
        raise ValueError(f'不支持的纸张类型: {paper_size}')
    if orientation not in {'portrait', 'landscape', 'auto'}:
        orientation = 'portrait'

    drawing_width_pt = metrics['width_mm'] * MM_TO_PT
    drawing_height_pt = metrics['height_mm'] * MM_TO_PT
    if enabled_pages is not None and not enabled_pages:
        raise ValueError('至少保留一个输出页面')
    if single_page:
        page_width_pt = drawing_width_pt + margin_mm * MM_TO_PT * 2
        page_height_pt = drawing_height_pt + margin_mm * MM_TO_PT * 2
        pages = [{'row': 0, 'column': 0, 'source_index': 0}]
        layout = {
            'type': 'single',
            'paper_size': None,
            'orientation': 'portrait',
            'page_width_pt': page_width_pt,
            'page_height_pt': page_height_pt,
            'drawing_width_mm': metrics['width_mm'],
            'drawing_height_mm': metrics['height_mm'],
            'columns': 1,
            'rows': 1,
            'page_count': 1,
            'margin_mm': margin_mm,
        }
        page_contents = [
            build_page_content(
                shapes,
                metrics,
                page,
                page_width_pt,
                page_height_pt,
                drawing_width_pt,
                drawing_height_pt,
                drawing_width_pt,
                drawing_height_pt,
                line_width_mm * MM_TO_PT,
                show_page_number,
                1,
                1,
                page_label='1-1',
                paper_label='SINGLE',
            )
            for page in pages
        ]
        return build_pdf_document(page_contents, page_width_pt, page_height_pt), layout

    short_mm, long_mm = PAPER_SIZES_MM[paper_size]
    page_width_mm, page_height_mm = short_mm, long_mm
    if orientation == 'landscape' or (orientation == 'auto' and drawing_width_pt >= drawing_height_pt):
        page_width_mm, page_height_mm = long_mm, short_mm
    page_width_pt = page_width_mm * MM_TO_PT
    page_height_pt = page_height_mm * MM_TO_PT
    margin_pt = margin_mm * MM_TO_PT
    tile_width_pt = max(1, page_width_pt - margin_pt * 2)
    tile_height_pt = max(1, page_height_pt - margin_pt * 2)
    columns = max(1, math.ceil(drawing_width_pt / tile_width_pt))
    rows = max(1, math.ceil(drawing_height_pt / tile_height_pt))
    all_pages = [
        {'row': row, 'column': column, 'source_index': row * columns + column}
        for row in range(rows)
        for column in range(columns)
    ]
    if enabled_pages is not None:
        enabled = {int(value) for value in enabled_pages}
        selected_pages = [page for page in all_pages if page['source_index'] in enabled]
        if not selected_pages:
            raise ValueError('至少保留一个输出页面')
        pages = selected_pages
    else:
        pages = all_pages

    page_count = len(pages)
    if page_count > maximum_pages:
        raise ValueError(f'输出页数超过限制，最多 {maximum_pages} 页')
    layout = {
        'type': 'tiled',
        'paper_size': paper_size,
        'orientation': 'landscape' if page_width_mm > page_height_mm else 'portrait',
        'page_width_pt': page_width_pt,
        'page_height_pt': page_height_pt,
        'tile_width_pt': tile_width_pt,
        'tile_height_pt': tile_height_pt,
        'drawing_width_mm': metrics['width_mm'],
        'drawing_height_mm': metrics['height_mm'],
        'columns': columns,
        'rows': rows,
        'page_count': page_count,
        'margin_mm': margin_mm,
    }
    page_contents = []
    for page_number, page in enumerate(pages, start=1):
        page_contents.append(build_page_content(
            shapes,
            metrics,
            page,
            page_width_pt,
            page_height_pt,
            drawing_width_pt,
            drawing_height_pt,
            tile_width_pt,
            tile_height_pt,
            line_width_mm * MM_TO_PT,
            show_page_number,
            page_number,
            page_count,
            margin_pt=margin_pt,
            page_label=f"{page['row'] + 1}-{page['column'] + 1}",
            paper_label=paper_size,
        ))
    return build_pdf_document(page_contents, page_width_pt, page_height_pt), layout


def build_page_content(
    shapes,
    metrics,
    page,
    page_width_pt,
    page_height_pt,
    drawing_width_pt,
    drawing_height_pt,
    tile_width_pt,
    tile_height_pt,
    line_width_pt,
    show_page_number,
    page_number,
    page_count,
    margin_pt=0,
    page_label='1-1',
    paper_label='A4',
):
    content = ['q']
    if margin_pt > 0 and tile_width_pt > 0 and tile_height_pt > 0:
        content.extend([
            f'{fmt(margin_pt)} {fmt(margin_pt)} {fmt(tile_width_pt)} {fmt(tile_height_pt)} re',
            'W',
            'n',
        ])
    content.extend([
        f'{fmt(line_width_pt)} w',
        '1 J',
        '1 j',
        '0 0 0 RG',
        '0 0 0 rg',
    ])

    clip_min_x = margin_pt if margin_pt > 0 else 0
    clip_min_y = margin_pt if margin_pt > 0 else 0
    clip_max_x = clip_min_x + tile_width_pt
    clip_max_y = clip_min_y + tile_height_pt

    path_content = []
    circles = []
    text_shapes = []
    for shape in shapes:
        if shape['type'] == 'path' and len(shape.get('points', [])) > 1:
            points = [
                transform_point(
                    point,
                    metrics,
                    page,
                    drawing_width_pt,
                    drawing_height_pt,
                    tile_width_pt,
                    tile_height_pt,
                    margin_pt,
                )
                for point in shape['points']
            ]
            append_clipped_paths(
                path_content,
                points,
                clip_min_x,
                clip_min_y,
                clip_max_x,
                clip_max_y,
            )
        elif shape['type'] == 'circle':
            circles.append(shape)
        elif shape['type'] == 'text':
            text_shapes.append(shape)
    if path_content:
        content.extend(path_content)
        content.append('S')

    for shape in circles:
        points = [
            transform_point(
                point,
                metrics,
                page,
                drawing_width_pt,
                drawing_height_pt,
                tile_width_pt,
                tile_height_pt,
                margin_pt,
            )
            for point in approximate_circle(shape['center'], shape['radius'], 48)
        ]
        circle_content = []
        append_clipped_paths(
            circle_content,
            points,
            clip_min_x,
            clip_min_y,
            clip_max_x,
            clip_max_y,
        )
        if circle_content:
            content.extend(circle_content)
            content.append('S')

    for shape in text_shapes:
        point = transform_point(shape['point'], metrics, page, drawing_width_pt, drawing_height_pt, tile_width_pt, tile_height_pt, margin_pt)
        if not point_in_rect(point, clip_min_x, clip_min_y, clip_max_x, clip_max_y):
            continue
        text = utf16be_hex(shape.get('text', ''))
        if not text:
            continue
        content.extend([
            '0 0 0 rg',
            'BT',
            '/F1 8 Tf',
            f'{fmt(point[0])} {fmt(point[1])} Td',
            f'<{text}> Tj',
            'ET',
        ])

    content.append('Q')
    append_page_guide(content, margin_pt, tile_width_pt, tile_height_pt)
    if show_page_number:
        append_text(content, page_label, 18, page_height_pt - 78, 48, color=(0.16, 0.24, 0.27))
        footer = f'{paper_label} 100% | {page_label} | {page_number}/{page_count} | plt-guide-v1'
        append_text(content, footer, 28, 12, 8, color=(0.16, 0.24, 0.27))
    if page_number == 1:
        append_scale_marker(content, page_width_pt, page_height_pt, margin_pt)
    return '\n'.join(content)


def append_clipped_paths(content, points, min_x, min_y, max_x, max_y):
    previous_end = None
    for start, end in zip(points, points[1:]):
        clipped = clip_segment(start, end, min_x, min_y, max_x, max_y)
        if not clipped:
            previous_end = None
            continue
        clipped_start, clipped_end = clipped
        if previous_end is None or not points_equal(previous_end, clipped_start):
            content.append(f'{fmt(clipped_start[0])} {fmt(clipped_start[1])} m')
        content.append(f'{fmt(clipped_end[0])} {fmt(clipped_end[1])} l')
        previous_end = clipped_end
    if previous_end is not None:
        content.append('')


def clip_segment(start, end, min_x, min_y, max_x, max_y):
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    lower = 0.0
    upper = 1.0
    for direction, distance in (
        (-dx, x1 - min_x),
        (dx, max_x - x1),
        (-dy, y1 - min_y),
        (dy, max_y - y1),
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
        (x1 + lower * dx, y1 + lower * dy),
        (x1 + upper * dx, y1 + upper * dy),
    )


def point_in_rect(point, min_x, min_y, max_x, max_y):
    return min_x <= point[0] <= max_x and min_y <= point[1] <= max_y


def points_equal(first, second):
    return abs(first[0] - second[0]) < 1e-7 and abs(first[1] - second[1]) < 1e-7


def transform_point(point, metrics, page, drawing_width_pt, drawing_height_pt, tile_width_pt, tile_height_pt, margin_pt):
    scale = MM_TO_PT / 25.4
    x_pt = (point['x'] - metrics['min_x']) / metrics['units_per_inch'] * 72
    y_top_pt = (metrics['max_y'] - point['y']) / metrics['units_per_inch'] * 72
    if tile_width_pt == drawing_width_pt and tile_height_pt == drawing_height_pt:
        return margin_pt + x_pt, margin_pt + drawing_height_pt - y_top_pt
    return (
        margin_pt + x_pt - page['column'] * tile_width_pt,
        margin_pt + tile_height_pt + page['row'] * tile_height_pt - y_top_pt,
    )


def approximate_circle(center, radius, steps):
    return [
        {
            'x': center['x'] + math.cos(math.pi * 2 * index / steps) * radius,
            'y': center['y'] + math.sin(math.pi * 2 * index / steps) * radius,
        }
        for index in range(steps + 1)
    ]


def append_page_guide(content, margin_pt, tile_width_pt, tile_height_pt):
    if margin_pt <= 0 or tile_width_pt <= 0 or tile_height_pt <= 0:
        return
    content.extend([
        'q',
        '0 0.55 0.55 RG',
        '0.7 w',
        '0 J',
        '0 j',
        '[3 3] 0 d',
        f'{fmt(margin_pt)} {fmt(margin_pt)} {fmt(tile_width_pt)} {fmt(tile_height_pt)} re',
        'S',
        'Q',
    ])


def append_scale_marker(content, page_width_pt, page_height_pt, margin_pt):
    marker_size = 50 * MM_TO_PT
    x = max(12, page_width_pt - max(margin_pt, 12) - marker_size - 12)
    top_y = max(marker_size + 36, page_height_pt - max(margin_pt * 0.55, 18))
    bottom_y = top_y - marker_size
    content.extend([
        'q',
        '1 0 0 RG',
        '1 w',
        f'{fmt(x)} {fmt(bottom_y)} {fmt(marker_size)} {fmt(marker_size)} re',
        'S',
        'Q',
    ])
    text_x = x + marker_size / 2 - 31
    append_text(content, '5 cm / 50 mm', text_x, bottom_y - 18, 10, color=(1, 0, 0))


def append_text(content, text, x, y, font_size, color=(0, 0, 0)):
    encoded_text = utf16be_hex(text)
    if not encoded_text:
        return
    content.extend([
        'q',
        f'{fmt(color[0])} {fmt(color[1])} {fmt(color[2])} rg',
        'BT',
        f'/F1 {fmt(font_size)} Tf',
        f'1 0 0 1 {fmt(x)} {fmt(y)} Tm',
        f'<{encoded_text}> Tj',
        'ET',
        'Q',
    ])


def build_pdf_document(page_contents, page_width_pt, page_height_pt):
    objects = [None]
    cid_font = add_object(
        objects,
        '<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light '
        '/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> /DW 1000 >>',
    )
    font = add_object(
        objects,
        f'<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light '
        f'/Encoding /UniGB-UCS2-H /DescendantFonts [{cid_font} 0 R] >>',
    )
    pages_object = add_object(objects, '')
    page_objects = []
    for content in page_contents:
        content_object = add_object(objects, stream_object(content))
        page_object = add_object(
            objects,
            f'<< /Type /Page /Parent {pages_object} 0 R '
            f'/MediaBox [0 0 {fmt(page_width_pt)} {fmt(page_height_pt)}] '
            f'/Resources << /Font << /F1 {font} 0 R >> >> '
            f'/Contents {content_object} 0 R >>',
        )
        page_objects.append(page_object)
    objects[pages_object] = (
        f'<< /Type /Pages /Kids [{" ".join(f"{item} 0 R" for item in page_objects)}] '
        f'/Count {len(page_objects)} >>'
    )
    catalog = add_object(objects, f'<< /Type /Catalog /Pages {pages_object} 0 R >>')
    return build_pdf(objects, catalog)


def stream_object(content):
    compressed = zlib.compress(content.encode('utf-8'), level=9)
    return (
        f'<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n'.encode('ascii')
        + compressed
        + b'\nendstream'
    )


def add_object(objects, content):
    objects.append(content)
    return len(objects) - 1


def build_pdf(objects, catalog):
    chunks = [b'%PDF-1.4\n']
    offsets = [0]
    offset = len(chunks[0])
    for index in range(1, len(objects)):
        content = objects[index]
        if isinstance(content, str):
            content = content.encode('utf-8')
        body = f'{index} 0 obj\n'.encode('ascii') + content + b'\nendobj\n'
        offsets.append(offset)
        chunks.append(body)
        offset += len(body)
    xref_offset = offset
    xref = f'xref\n0 {len(objects)}\n0000000000 65535 f \n'
    xref += ''.join(f'{item:010d} 00000 n \n' for item in offsets[1:])
    trailer = (
        f'trailer\n<< /Size {len(objects)} /Root {catalog} 0 R >>\n'
        f'startxref\n{xref_offset}\n%%EOF\n'
    )
    return b''.join(chunks + [xref.encode('ascii'), trailer.encode('ascii')])


def utf16be_hex(text):
    return str(text).encode('utf-16-be').hex().upper()


def escape_pdf_string(text):
    return str(text).replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def fmt(value):
    return f'{float(value):.3f}'
