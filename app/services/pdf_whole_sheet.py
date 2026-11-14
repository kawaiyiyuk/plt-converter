"""Read a pattern PDF whose pages each hold the complete sheet, offset by tile.

Some pattern exporters write the whole drawing into every page and only move
the content stream to expose a different tile. Such a PDF does not need page
stitching: page 1 already contains every path, so the whole sheet can be
emitted directly.
"""

import os

from .pdf_to_plt import (
    MAX_PDF_PAGES,
    append_contiguous_shape,
    drawing_item_points,
    load_fitz,
    open_pdf_document,
    read_pdf_layout_metadata_from_document,
)


UNITS_PER_INCH = 1016
UNITS_PER_PT = UNITS_PER_INCH / 72
UNITS_PER_MM = UNITS_PER_INCH / 25.4
MAX_WHOLE_SHEET_CHECKS = 2_000_000
PAGE_SIZE_TOLERANCE_PT = 0.5
OFFSET_TOLERANCE_PT = 0.5
WINDOW_TOLERANCE_PT = 1.0
MIN_OUTSIDE_FIRST_PAGE_RATIO = 0.3
MIN_OFFSET_MATCH_RATIO = 0.95
DEFAULT_MAX_OUTPUT_SEGMENTS = 300000
BIG_PIECE_MIN_EDGE_MM = 5.0
BIG_PIECE_MIN_AREA_CM2 = 100.0
BIG_PIECE_MAX_FILL_RATIO = 0.98


class WholeSheetTooComplex(ValueError):
    """The whole-sheet extraction exceeds the configured segment limit."""


def read_whole_sheet(source):
    """Return whole-sheet geometry, or None when the PDF is not this kind.

    The document is opened without ``validate_pdf_complexity``: these files
    intentionally repeat the complete drawing on every page and would exceed
    the ordinary drawing budget.
    """
    fitz = load_fitz()
    document = open_pdf_document(source, fitz)
    try:
        return _read_whole_sheet_document(document, fitz)
    finally:
        document.close()


def _read_whole_sheet_document(document, fitz):
    page_count = int(document.page_count)
    if page_count < 2 or page_count > MAX_PDF_PAGES:
        return None
    if read_pdf_layout_metadata_from_document(document):
        return None

    pages = [document.load_page(index) for index in range(page_count)]
    first_rect = pages[0].rect
    for page in pages:
        if int(getattr(page, 'rotation', 0)) != 0:
            return None
        if (abs(page.rect.width - first_rect.width) > PAGE_SIZE_TOLERANCE_PT
                or abs(page.rect.height - first_rect.height) > PAGE_SIZE_TOLERANCE_PT):
            return None
    if pages[0].get_images():
        return None

    first_drawings = pages[0].get_drawings()
    drawing_count = len(first_drawings)
    if drawing_count < 1 or page_count * drawing_count > MAX_WHOLE_SHEET_CHECKS:
        return None

    drawings = [first_drawings]
    offsets = [(0.0, 0.0)]
    for page in pages[1:]:
        page_drawings = page.get_drawings()
        if len(page_drawings) != drawing_count:
            return None
        offset = _page_offset(first_drawings, page_drawings)
        if offset is None:
            return None
        drawings.append(page_drawings)
        offsets.append(offset)

    grid = _grid_layout(offsets, pages[0].rect)
    if grid is None:
        return None
    rows, columns, order = grid

    fixed = _fixed_path_indexes(drawings, offsets)
    retained = [index for index in range(drawing_count) if not fixed[index]]
    if not retained:
        return None

    windows = [
        fitz.Rect(
            page.rect.x0 - offset[0],
            page.rect.y0 - offset[1],
            page.rect.x1 - offset[0],
            page.rect.y1 - offset[1],
        )
        for page, offset in zip(pages, offsets)
    ]
    if not all(
        any(_rect_overlaps(first_drawings[index]['rect'], window)
            for window in windows)
        for index in retained
    ):
        return None
    bounds = (
        min(window.x0 for window in windows),
        min(window.y0 for window in windows),
        max(window.x1 for window in windows),
        max(window.y1 for window in windows),
    )
    if not all(
        _rect_within_bounds(first_drawings[index]['rect'], bounds)
        for index in retained
    ):
        return None

    outside_count = sum(
        1 for index in retained
        if _rect_completely_outside(first_drawings[index]['rect'], first_rect)
    )
    if outside_count / len(retained) < MIN_OUTSIDE_FIRST_PAGE_RATIO:
        return None

    shapes, width_units, height_units = _extract_shapes(
        first_drawings, retained, fitz
    )
    if not shapes:
        return None
    return {
        'shapes': shapes,
        'width_units': width_units,
        'height_units': height_units,
        'page_count': page_count,
        'rows': rows,
        'columns': columns,
        'order': order,
        'fixed_paths_removed': sum(1 for value in fixed if value),
    }


def _page_offset(first_drawings, page_drawings):
    deltas = [
        (
            page_drawings[index]['rect'].x0 - first_drawings[index]['rect'].x0,
            page_drawings[index]['rect'].y0 - first_drawings[index]['rect'].y0,
        )
        for index in range(len(first_drawings))
    ]
    dx = _median([delta[0] for delta in deltas])
    dy = _median([delta[1] for delta in deltas])
    if abs(dx) <= OFFSET_TOLERANCE_PT and abs(dy) <= OFFSET_TOLERANCE_PT:
        return None
    matching = sum(
        1 for delta_x, delta_y in deltas
        if abs(delta_x - dx) <= OFFSET_TOLERANCE_PT
        and abs(delta_y - dy) <= OFFSET_TOLERANCE_PT
    )
    if matching / len(deltas) < MIN_OFFSET_MATCH_RATIO:
        return None
    return dx, dy


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _grid_layout(offsets, page_rect):
    x_centers = _axis_centers([offset[0] for offset in offsets], page_rect.width)
    y_centers = _axis_centers([offset[1] for offset in offsets], page_rect.height)
    if x_centers is None or y_centers is None:
        return None
    rows = len(y_centers)
    columns = len(x_centers)
    if len(offsets) > rows * columns:
        return None

    cells = []
    for offset in offsets:
        cell = (
            _nearest_center(x_centers, offset[0]),
            _nearest_center(y_centers, offset[1]),
        )
        if cell in cells:
            return None
        cells.append(cell)

    first_row = cells[0][1]
    order = 'row' if cells[1][1] == first_row else 'column'
    return rows, columns, order


def _axis_centers(values, page_dimension):
    ordered = sorted(values)
    clusters = []
    for value in ordered:
        if not clusters or value - clusters[-1][0] > OFFSET_TOLERANCE_PT:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    centers = [sum(cluster) / len(cluster) for cluster in clusters]
    steps = [later - earlier for earlier, later in zip(centers, centers[1:])]
    if steps:
        if min(steps) <= 0:
            return None
        if max(steps) - min(steps) > OFFSET_TOLERANCE_PT:
            return None
        if max(steps) > page_dimension + OFFSET_TOLERANCE_PT:
            return None
    return centers


def _nearest_center(centers, value):
    return min(range(len(centers)), key=lambda index: abs(centers[index] - value))


def _fixed_path_indexes(drawings, offsets):
    fixed = [False] * len(drawings[0])
    for page_index in range(1, len(drawings)):
        dx, dy = offsets[page_index]
        for path_index in range(len(drawings[0])):
            delta_x = (
                drawings[page_index][path_index]['rect'].x0
                - drawings[0][path_index]['rect'].x0
            )
            delta_y = (
                drawings[page_index][path_index]['rect'].y0
                - drawings[0][path_index]['rect'].y0
            )
            if (abs(delta_x - dx) > OFFSET_TOLERANCE_PT
                    or abs(delta_y - dy) > OFFSET_TOLERANCE_PT):
                fixed[path_index] = True
    return fixed


def _rect_overlaps(rect, window):
    return (
        rect.x1 >= window.x0 - WINDOW_TOLERANCE_PT
        and rect.x0 <= window.x1 + WINDOW_TOLERANCE_PT
        and rect.y1 >= window.y0 - WINDOW_TOLERANCE_PT
        and rect.y0 <= window.y1 + WINDOW_TOLERANCE_PT
    )


def _rect_within_bounds(rect, bounds):
    minimum_x, minimum_y, maximum_x, maximum_y = bounds
    return (
        rect.x0 >= minimum_x - WINDOW_TOLERANCE_PT
        and rect.y0 >= minimum_y - WINDOW_TOLERANCE_PT
        and rect.x1 <= maximum_x + WINDOW_TOLERANCE_PT
        and rect.y1 <= maximum_y + WINDOW_TOLERANCE_PT
    )


def _rect_completely_outside(rect, page_rect):
    return (
        rect.x1 <= page_rect.x0
        or rect.x0 >= page_rect.x1
        or rect.y1 <= page_rect.y0
        or rect.y0 >= page_rect.y1
    )


def _extract_shapes(drawings, retained_indexes, fitz):
    shapes = []
    for index in retained_indexes:
        drawing = drawings[index]
        if drawing.get('type') in {'clip', 'group'}:
            continue
        fill = drawing.get('fill')
        if (drawing.get('type') == 'f' and drawing.get('color') is None
                and (drawing.get('fill_opacity', 1) <= 0
                     or (fill is not None and all(channel >= 1 for channel in fill)))):
            continue
        drawing_shapes = []
        for item in drawing.get('items', []):
            points = drawing_item_points(item, fitz)
            converted = [
                {'x': point.x, 'y': point.y}
                for point in points
            ]
            append_contiguous_shape(drawing_shapes, converted)
        shapes.extend(drawing_shapes)

    if not shapes:
        return [], 0.0, 0.0

    xs = [point['x'] for shape in shapes for point in shape]
    ys = [point['y'] for shape in shapes for point in shape]
    x0 = min(xs)
    y1 = max(ys)
    maximum_segments = max(1000, int(os.getenv(
        'PDF_MAX_OUTPUT_SEGMENTS', str(DEFAULT_MAX_OUTPUT_SEGMENTS)
    )))
    total_segments = sum(max(len(shape) - 1, 0) for shape in shapes)
    if total_segments > maximum_segments:
        raise WholeSheetTooComplex(
            f'PDF 线条数量过多，最多支持 {maximum_segments} 条'
        )

    converted_shapes = [
        [
            {
                'x': (point['x'] - x0) * UNITS_PER_PT,
                'y': (y1 - point['y']) * UNITS_PER_PT,
            }
            for point in shape
        ]
        for shape in shapes
    ]
    width_units = (max(xs) - x0) * UNITS_PER_PT
    height_units = (y1 - min(ys)) * UNITS_PER_PT
    return converted_shapes, width_units, height_units


def has_big_piece(shapes, units_per_mm):
    """Return True when the paths enclose one large non-rectangular piece."""
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    minimum_length = BIG_PIECE_MIN_EDGE_MM * units_per_mm
    lines = []
    for shape in shapes:
        if len(shape) < 2:
            continue
        coordinates = [(float(point['x']), float(point['y'])) for point in shape]
        line = LineString(coordinates)
        if line.length >= minimum_length:
            lines.append(line)
    if not lines:
        return False

    minimum_area = BIG_PIECE_MIN_AREA_CM2 * 100.0 * units_per_mm * units_per_mm
    for polygon in polygonize(unary_union(lines)):
        if polygon.area < minimum_area:
            continue
        min_x, min_y, max_x, max_y = polygon.bounds
        bounding_area = (max_x - min_x) * (max_y - min_y)
        if bounding_area <= 0:
            continue
        if polygon.area / bounding_area < BIG_PIECE_MAX_FILL_RATIO:
            return True
    return False


def whole_sheet_layout_result(sheet):
    """Build the layout dictionary matching ``convert_pdf_to_plt`` exactly."""
    rows = int(sheet['rows'])
    columns = int(sheet['columns'])
    page_count = int(sheet['page_count'])
    return {
        'page_count': page_count,
        'selected_count': page_count,
        'empty_cells': max(rows * columns - page_count, 0),
        'rows': rows,
        'columns': columns,
        'order': sheet['order'],
        'output_rotation': 0,
        'margin_mm': 0,
        'preserve_outer_edges': False,
        'crop_left_mm': 0,
        'crop_right_mm': 0,
        'crop_top_mm': 0,
        'crop_bottom_mm': 0,
        'width_mm': round(sheet['width_units'] * 25.4 / UNITS_PER_INCH, 2),
        'height_mm': round(sheet['height_units'] * 25.4 / UNITS_PER_INCH, 2),
        'source_types': ['vector'],
        'raster_fallback': False,
        'embedded_layout_detected': False,
        'embedded_layout_applied': False,
    }
