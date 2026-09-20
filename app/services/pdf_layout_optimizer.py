"""Recommend a PDF tiling layout by matching vector paths across page seams."""

import itertools
import math
import os

from .pdf_to_plt import (
    MM_TO_PT,
    drawing_item_points,
    is_internal_guide_drawing,
    load_fitz,
    open_pdf_document,
    read_pdf_layout_metadata_from_document,
    validate_pdf_complexity,
)


EDGE_NAMES = ('left', 'right', 'top', 'bottom')
BOUNDARY_EPS_PT = 0.15
BOUNDARY_RANGE_EPS_PT = 0.5
DEDUP_TOLERANCE_PT = 0.8
MATCH_TOLERANCE_PT = 2.0
MAX_GRID_SIZE = 24
DEFAULT_MAX_BLANK_CELLS = 4
DEFAULT_MAX_CROP_MM = 15.0
DEFAULT_CROP_STEP_MM = 0.1


def optimize_pdf_layout(source):
    """Analyze a vector PDF and return the strongest page-layout suggestion."""
    fitz = load_fitz()
    document = open_pdf_document(source, fitz)
    try:
        if document.page_count == 0:
            raise ValueError('PDF 没有可用页面')
        if document.page_count > 200:
            raise ValueError('PDF 页数过多，最多支持 200 页')
        validate_pdf_complexity(document)
        embedded_layout = read_pdf_layout_metadata_from_document(document)
        if embedded_layout and embedded_layout.get('complete_layout'):
            return _embedded_layout_suggestion(embedded_layout)

        page_sizes, segments_by_page = _extract_vector_segments(
            document,
            fitz,
            ignore_internal_guides=bool(embedded_layout),
        )
    finally:
        document.close()

    if not _uniform_page_sizes(page_sizes):
        return _insufficient_suggestion('页面尺寸不一致，暂时无法可靠判断接缝')
    if not any(segments_by_page):
        return _insufficient_suggestion('没有检测到可用于拼接判断的矢量线条')

    width, height = page_sizes[0]
    maximum_crop = min(
        _positive_float_env('PDF_LAYOUT_OPTIMIZER_MAX_CROP_MM', DEFAULT_MAX_CROP_MM),
        max(width / MM_TO_PT / 2 - 1, 0),
        max(height / MM_TO_PT / 2 - 1, 0),
    )
    step = _positive_float_env('PDF_LAYOUT_OPTIMIZER_CROP_STEP_MM', DEFAULT_CROP_STEP_MM)
    crop_values = [round(index * step, 6) for index in range(int(maximum_crop / step) + 1)]
    maximum_blank_cells = max(
        0,
        min(
            int(os.getenv('PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS', DEFAULT_MAX_BLANK_CELLS)),
            8,
        ),
    )

    coarse = []
    signatures_by_crop = {}
    for crop_mm in crop_values:
        signatures = build_edge_signatures(
            segments_by_page,
            width,
            height,
            crop_mm,
        )
        signatures_by_crop[crop_mm] = signatures
        result = choose_best_layout(
            signatures,
            len(segments_by_page),
            maximum_blank_cells=maximum_blank_cells,
            optimize_blanks=False,
        )
        coarse.append((_rank_result(result), crop_mm))

    # When seam evidence is identical, keep the least destructive crop.
    coarse.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    shortlisted_crops = []
    for _rank, crop_mm in coarse:
        if any(abs(crop_mm - existing) < step * 0.51 for existing in shortlisted_crops):
            continue
        shortlisted_crops.append(crop_mm)
        if len(shortlisted_crops) >= 5:
            break

    finalists = []
    for crop_mm in shortlisted_crops:
        result = choose_best_layout(
            signatures_by_crop[crop_mm],
            len(segments_by_page),
            maximum_blank_cells=maximum_blank_cells,
            optimize_blanks=True,
        )
        result['crop_mm'] = crop_mm
        finalists.append(result)
    best = max(
        finalists,
        key=lambda result: (_rank_result(result), -float(result['crop_mm'])),
    )
    crop_mm = round(float(best.pop('crop_mm')), 1)
    best.update({
        'crop_margins_mm': {
            'top': crop_mm,
            'right': crop_mm,
            'bottom': crop_mm,
            'left': crop_mm,
        },
        # Seam geometry determines connectivity, but not an absolute garment "up" direction.
        # Zero degrees keeps the source-page orientation and remains user-adjustable afterwards.
        'output_rotation': 0,
        'rotation_basis': 'source_pages',
        'source': 'vector_seams',
    })
    if best['confidence'] == 'low':
        best['reason'] = (
            '检测到多个同样合理的拼版，无法可靠判断行列，请保留当前排版并手动调整'
            if best.get('layout_ambiguous')
            else '可匹配的跨页矢量接缝不足，建议保留当前排版并手动调整'
        )
    return best


def choose_best_layout(
    signatures,
    page_count,
    maximum_blank_cells=DEFAULT_MAX_BLANK_CELLS,
    optimize_blanks=True,
):
    """Choose rows, columns, fill order and preserved-order blank positions."""
    candidates = []
    for rows, columns in _candidate_grids(page_count, maximum_blank_cells):
        for order in ('row', 'column'):
            capacity = rows * columns
            base_slots = list(range(page_count)) + [None] * (capacity - page_count)
            result = _evaluate_layout(rows, columns, order, base_slots, signatures)
            candidates.append(result)

    if not candidates:
        raise ValueError('PDF 页数无法放入最大 24×24 的排版网格')

    candidates.sort(key=_rank_result, reverse=True)
    if optimize_blanks:
        # A correct grid can score poorly while every blank is still at the end. Refine
        # every legal grid/order before comparing them so internal blank rows or columns
        # are not discarded by the coarse arrangement.
        optimized = [
            _optimize_blank_positions(candidate, signatures, page_count)
            for candidate in candidates
        ]
        candidates = optimized + candidates

    best = max(candidates, key=_rank_result)
    best = dict(best)
    layout_ambiguous = _has_competing_layout(candidates, best)
    evidence = best['seam_evidence']
    positive_ratio = best['matched_seams'] / evidence if evidence else 0.0
    perfect_ratio = best['perfect_seams'] / evidence if evidence else 0.0
    mean_score = best['confidence_score']
    minimum_high_evidence = max(6, min(20, page_count // 2))
    if layout_ambiguous:
        confidence = 'low'
    elif (
        evidence >= minimum_high_evidence
        and mean_score >= 0.92
        and positive_ratio >= 0.9
        and perfect_ratio >= 0.7
    ):
        confidence = 'high'
    elif evidence >= 4 and mean_score >= 0.75 and positive_ratio >= 0.75:
        confidence = 'medium'
    else:
        confidence = 'low'
    best['confidence'] = confidence
    best['layout_ambiguous'] = layout_ambiguous
    return best


def _has_competing_layout(candidates, best):
    """Return True when seam quality cannot distinguish another grid/blank layout."""
    best_evidence = best['seam_evidence']
    if best_evidence < 4:
        return False
    best_positive_ratio = best['matched_seams'] / best_evidence
    best_perfect_ratio = best['perfect_seams'] / best_evidence
    best_blank_count = best['rows'] * best['columns'] - len([
        page for page in best['page_slots'] if page is not None
    ])
    best_identity = _layout_identity(best)
    seen = {best_identity}
    for candidate in candidates:
        identity = _layout_identity(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        evidence = candidate['seam_evidence']
        candidate_blank_count = candidate['rows'] * candidate['columns'] - len([
            page for page in candidate['page_slots'] if page is not None
        ])
        # Extra empty border rows/columns can preserve every real seam; the simpler
        # layout with fewer blanks dominates that representation rather than tying it.
        if candidate_blank_count > best_blank_count:
            continue
        if evidence < 4 or evidence < best_evidence * 0.9:
            continue
        positive_ratio = candidate['matched_seams'] / evidence
        perfect_ratio = candidate['perfect_seams'] / evidence
        if (
            candidate['matched_seams'] >= best['matched_seams'] * 0.9
            and candidate['confidence_score'] >= best['confidence_score'] - 0.02
            and positive_ratio >= best_positive_ratio - 0.02
            and perfect_ratio >= best_perfect_ratio - 0.02
        ):
            return True
    return False


def _layout_identity(result):
    rows = result['rows']
    columns = result['columns']
    visual_slots = tuple(
        _page_at(result['page_slots'], rows, columns, result['order'], row, column)
        for row in range(rows)
        for column in range(columns)
    )
    return rows, columns, visual_slots


def build_edge_signatures(segments_by_page, width, height, crop_mm):
    margin = crop_mm * MM_TO_PT
    left, right = margin, width - margin
    top, bottom = margin, height - margin
    signatures = []
    for segments in segments_by_page:
        edges = {name: [] for name in EDGE_NAMES}
        for start, end in segments:
            value = _crossing_coordinate(start, end, 0, left)
            if value is not None and top - BOUNDARY_RANGE_EPS_PT <= value <= bottom + BOUNDARY_RANGE_EPS_PT:
                edges['left'].append(value - top)
            value = _crossing_coordinate(start, end, 0, right)
            if value is not None and top - BOUNDARY_RANGE_EPS_PT <= value <= bottom + BOUNDARY_RANGE_EPS_PT:
                edges['right'].append(value - top)
            value = _crossing_coordinate(start, end, 1, top)
            if value is not None and left - BOUNDARY_RANGE_EPS_PT <= value <= right + BOUNDARY_RANGE_EPS_PT:
                edges['top'].append(value - left)
            value = _crossing_coordinate(start, end, 1, bottom)
            if value is not None and left - BOUNDARY_RANGE_EPS_PT <= value <= right + BOUNDARY_RANGE_EPS_PT:
                edges['bottom'].append(value - left)
        signatures.append({name: _dedupe(values) for name, values in edges.items()})
    return signatures


def _candidate_grids(page_count, maximum_blank_cells):
    seen = set()
    for rows in range(1, MAX_GRID_SIZE + 1):
        minimum_columns = math.ceil(page_count / rows)
        maximum_columns = min(
            MAX_GRID_SIZE,
            (page_count + maximum_blank_cells) // rows,
        )
        for columns in range(max(1, minimum_columns), maximum_columns + 1):
            blanks = rows * columns - page_count
            if blanks < 0 or blanks > maximum_blank_cells:
                continue
            key = (rows, columns)
            if key not in seen:
                seen.add(key)
                yield key


def _optimize_blank_positions(candidate, signatures, page_count):
    rows = candidate['rows']
    columns = candidate['columns']
    order = candidate['order']
    blank_count = rows * columns - page_count
    if blank_count <= 0:
        return candidate

    beam = [(tuple(range(page_count)), None)]
    beam_width = 64 if blank_count <= 2 else 16
    for _ in range(blank_count):
        expanded = {}
        for sequence, _score in beam:
            for position in range(len(sequence) + 1):
                next_sequence = sequence[:position] + (None,) + sequence[position:]
                expanded[next_sequence] = None
        remaining_blanks = blank_count - (len(next(iter(expanded))) - page_count)
        scored = []
        for sequence in expanded:
            slots = list(sequence) + [None] * remaining_blanks
            result = _evaluate_layout(rows, columns, order, slots, signatures)
            scored.append((sequence, _rank_result(result)))
        scored.sort(key=lambda item: item[1], reverse=True)
        beam = scored[:beam_width]

    return max(
        (
            _evaluate_layout(rows, columns, order, list(sequence), signatures)
            for sequence, _score in beam
        ),
        key=_rank_result,
    )


def _evaluate_layout(rows, columns, order, page_slots, signatures):
    scores = []
    for row in range(rows):
        for column in range(columns):
            page = _page_at(page_slots, rows, columns, order, row, column)
            if page is None:
                continue
            if column + 1 < columns:
                adjacent = _page_at(page_slots, rows, columns, order, row, column + 1)
                if adjacent is not None:
                    score = _match_score(
                        signatures[page]['right'],
                        signatures[adjacent]['left'],
                    )
                    if score is not None:
                        scores.append(score)
            if row + 1 < rows:
                adjacent = _page_at(page_slots, rows, columns, order, row + 1, column)
                if adjacent is not None:
                    score = _match_score(
                        signatures[page]['bottom'],
                        signatures[adjacent]['top'],
                    )
                    if score is not None:
                        scores.append(score)
    evidence = len(scores)
    return {
        'rows': rows,
        'columns': columns,
        'order': order,
        'page_slots': list(page_slots),
        'seam_evidence': evidence,
        'matched_seams': sum(score > 0 for score in scores),
        'perfect_seams': sum(score >= 0.999 for score in scores),
        'zero_seams': sum(score == 0 for score in scores),
        'confidence_score': round(sum(scores) / evidence, 4) if evidence else 0.0,
    }


def _rank_result(result):
    blank_count = sum(page is None for page in result['page_slots'])
    total_score = round(
        float(result['confidence_score']) * int(result['seam_evidence']),
        4,
    )
    return (
        total_score,
        result['perfect_seams'],
        result['matched_seams'],
        -result['zero_seams'],
        result['confidence_score'],
        result['seam_evidence'],
        -blank_count,
        -abs(result['rows'] - result['columns']),
    )


def _page_at(page_slots, rows, columns, order, row, column):
    index = column * rows + row if order == 'column' else row * columns + column
    return page_slots[index] if 0 <= index < len(page_slots) else None


def _match_score(first, second):
    if not first and not second:
        return None
    if not first or not second:
        return 0.0
    # Crossing coordinates are sorted. Match them one-to-one so two nearby
    # lines on one page cannot both claim the same line on the adjacent page.
    first_index = 0
    second_index = 0
    matches = 0
    while first_index < len(first) and second_index < len(second):
        delta = first[first_index] - second[second_index]
        if abs(delta) <= MATCH_TOLERANCE_PT:
            matches += 1
            first_index += 1
            second_index += 1
        elif delta < 0:
            first_index += 1
        else:
            second_index += 1
    return 2 * matches / (len(first) + len(second))


def _crossing_coordinate(start, end, axis, boundary):
    first = start[axis]
    second = end[axis]
    if abs(first - boundary) <= BOUNDARY_EPS_PT and abs(second - boundary) <= BOUNDARY_EPS_PT:
        return None
    if (first - boundary) * (second - boundary) > 0 or abs(second - first) < 1e-9:
        return None
    ratio = (boundary - first) / (second - first)
    if ratio < -1e-9 or ratio > 1 + 1e-9:
        return None
    other_axis = 1 - axis
    return start[other_axis] + ratio * (end[other_axis] - start[other_axis])


def _dedupe(values):
    result = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > DEDUP_TOLERANCE_PT:
            result.append(value)
    return result


def _extract_vector_segments(document, fitz, ignore_internal_guides=False):
    sizes = []
    pages = []
    for page_index in range(document.page_count):
        page = document.load_page(page_index)
        sizes.append((float(page.rect.width), float(page.rect.height)))
        rotation_matrix = getattr(page, 'rotation_matrix', None)
        segments = []
        for drawing in page.get_drawings():
            if ignore_internal_guides and is_internal_guide_drawing(drawing):
                continue
            color = drawing.get('color')
            if color is None:
                continue
            for item in drawing.get('items', []):
                points = drawing_item_points(item, fitz)
                if rotation_matrix is not None:
                    points = [point * rotation_matrix for point in points]
                for start, end in itertools.pairwise(points):
                    segment = (
                        (float(start.x), float(start.y)),
                        (float(end.x), float(end.y)),
                    )
                    if _is_red_crop_guide_segment(
                        segment[0],
                        segment[1],
                        color,
                        float(page.rect.width),
                        float(page.rect.height),
                    ):
                        continue
                    segments.append(segment)
        pages.append(segments)
    return sizes, pages


def _is_red_crop_guide_segment(start, end, color, width, height):
    if not _is_red_stroke(color):
        return False
    maximum_inset = (
        _positive_float_env('PDF_LAYOUT_OPTIMIZER_MAX_CROP_MM', DEFAULT_MAX_CROP_MM)
        * MM_TO_PT
        + BOUNDARY_RANGE_EPS_PT
    )
    delta_x = abs(end[0] - start[0])
    delta_y = abs(end[1] - start[1])
    if delta_y <= BOUNDARY_EPS_PT:
        y = (start[1] + end[1]) / 2
        return min(abs(y), abs(height - y)) <= maximum_inset
    if delta_x <= BOUNDARY_EPS_PT:
        x = (start[0] + end[0]) / 2
        return min(abs(x), abs(width - x)) <= maximum_inset
    return False


def _is_red_stroke(color):
    return bool(
        color
        and len(color) >= 3
        and float(color[0]) >= 0.9
        and float(color[1]) <= 0.15
        and float(color[2]) <= 0.15
    )


def _uniform_page_sizes(page_sizes):
    if not page_sizes:
        return False
    width, height = page_sizes[0]
    return all(
        abs(other_width - width) <= 0.5 and abs(other_height - height) <= 0.5
        for other_width, other_height in page_sizes[1:]
    )


def _embedded_layout_suggestion(metadata):
    return {
        'rows': metadata['rows'],
        'columns': metadata['columns'],
        'order': metadata['order'],
        'page_slots': metadata['page_slots'],
        'crop_margins_mm': metadata['crop_margins_mm'],
        'output_rotation': 0,
        'rotation_basis': 'source_pages',
        'source': 'embedded_metadata',
        'confidence': 'high',
        'confidence_score': 1.0,
        'seam_evidence': 0,
        'matched_seams': 0,
        'perfect_seams': 0,
        'zero_seams': 0,
        'layout_ambiguous': False,
    }


def _insufficient_suggestion(reason):
    return {
        'confidence': 'low',
        'confidence_score': 0.0,
        'seam_evidence': 0,
        'matched_seams': 0,
        'perfect_seams': 0,
        'zero_seams': 0,
        'layout_ambiguous': False,
        'reason': reason,
        'source': 'vector_seams',
    }


def _positive_float_env(name, default):
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default
