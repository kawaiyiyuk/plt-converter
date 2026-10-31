"""Recommend a PDF tiling layout by matching vector paths across page seams."""

import itertools
import math
import os
from functools import lru_cache

from .pdf_to_plt import (
    MM_TO_PT,
    clip_page_polyline,
    drawing_item_points,
    is_internal_guide_drawing,
    iter_native_clipped_drawings,
    native_clip_drawings,
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
DEFAULT_MAX_BLANK_CELLS = 8
PDF_LAYOUT_ALGORITHM_VERSION = '4-asymmetric-selection-blank8-visible-clips'
DEFAULT_MAX_CROP_MM = 15.0
DEFAULT_CROP_STEP_MM = 0.1


def effective_pdf_layout_max_blank_cells():
    """Share the effective bounded search setting with analysis-cache identity."""
    try:
        value = int(os.getenv('PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS', DEFAULT_MAX_BLANK_CELLS))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_BLANK_CELLS
    return max(0, min(value, 8))


def normalize_pdf_page_indexes(page_indexes, page_count):
    """Validate source-page selection and keep the original PDF ordering."""
    if type(page_count) is not int or page_count < 1:
        raise ValueError('PDF 没有可用页面')
    if page_indexes is None:
        return list(range(page_count))
    if not isinstance(page_indexes, list) or not page_indexes:
        raise ValueError('page_indexes 必须是非空页码列表')
    if any(type(index) is not int or not 0 <= index < page_count for index in page_indexes):
        raise ValueError('page_indexes 必须包含有效的 PDF 原始页面索引')
    if len(set(page_indexes)) != len(page_indexes):
        raise ValueError('page_indexes 不允许重复页面')
    return sorted(page_indexes)


def optimize_pdf_layout(source, metadata_mode=None, page_indexes=None):
    """Analyze a vector PDF and return the strongest page-layout suggestion."""
    fitz = load_fitz()
    document = open_pdf_document(source, fitz)
    try:
        if document.page_count == 0:
            raise ValueError('PDF 没有可用页面')
        if document.page_count > 200:
            raise ValueError('PDF 页数过多，最多支持 200 页')
        selected_indexes = normalize_pdf_page_indexes(page_indexes, document.page_count)
        validate_pdf_complexity(document)
        embedded_layout = read_pdf_layout_metadata_from_document(document)
        from .pdf_to_plt import validate_metadata_mode
        metadata_mode = validate_metadata_mode(metadata_mode, embedded_layout)
        if metadata_mode == 'current':
            embedded_layout = None
        if embedded_layout and embedded_layout.get('complete_layout'):
            return _embedded_layout_suggestion(embedded_layout, selected_indexes)

        selection_options = {} if page_indexes is None else {'page_indexes': selected_indexes}
        page_sizes, segments_by_page = _extract_vector_segments(
            document,
            fitz,
            ignore_internal_guides=bool(embedded_layout),
            ignore_crop_guides=metadata_mode != 'current',
            **selection_options,
        )
        frame_crops = _extract_crop_candidates(document, fitz, selected_indexes)
    finally:
        document.close()

    if not _uniform_page_sizes(page_sizes):
        return _insufficient_suggestion(
            _size_mismatch_reason(page_sizes, selected_indexes)
        )
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
    maximum_blank_cells = effective_pdf_layout_max_blank_cells()

    coarse = []
    signatures_by_crop = {}
    crop_candidates = [dict.fromkeys(EDGE_NAMES, value) for value in crop_values]
    crop_candidates.extend(frame_crops)
    for margins in crop_candidates:
        crop_mm = tuple(margins[side] for side in EDGE_NAMES)
        signatures = build_edge_signatures(
            segments_by_page,
            width,
            height,
            margins,
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
    coarse.sort(key=lambda item: (item[0], -sum(item[1])), reverse=True)
    shortlisted_crops = []
    for _rank, crop_mm in coarse:
        if crop_mm in shortlisted_crops:
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
        key=lambda result: (_rank_result(result), -sum(result['crop_mm'])),
    )
    margins = dict(zip(EDGE_NAMES, best.pop('crop_mm')))
    best['page_slots'] = [selected_indexes[index] if index is not None else None
                          for index in best['page_slots']]
    best.update({
        'crop_margins_mm': margins,
        'preserve_outer_edges': True,
        # Seam geometry determines connectivity, but not an absolute garment "up" direction.
        # Zero degrees keeps the source-page orientation and remains user-adjustable afterwards.
        'output_rotation': 0,
        'rotation_basis': 'source_pages',
        'source': 'vector_seams',
    })
    if best['confidence'] == 'low':
        best['reason'] = (
            '空白格隔断了可连续的纸样接缝，建议保留当前排版并手动调整'
            if best.get('blank_seam_conflict')
            else '检测到多个同样合理的拼版，无法可靠判断行列，请保留当前排版并手动调整'
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
    # A crop's page-pair geometry is invariant across all grids and blank beams.
    score_cache = {}
    for rows, columns in _candidate_grids(page_count, maximum_blank_cells):
        for order in ('row', 'column'):
            capacity = rows * columns
            base_slots = list(range(page_count)) + [None] * (capacity - page_count)
            result = _evaluate_layout(rows, columns, order, base_slots, signatures, score_cache)
            candidates.append(result)

    if not candidates:
        raise ValueError('PDF 页数无法放入最大 24×24 的排版网格')

    candidates.sort(key=_rank_result, reverse=True)
    if optimize_blanks:
        # A correct grid can score poorly while every blank is still at the end. Refine
        # every legal grid/order before comparing them so internal blank rows or columns
        # are not discarded by the coarse arrangement.
        optimized = [
            _optimize_blank_positions(candidate, signatures, page_count, score_cache)
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
    blank_conflicts = _blank_seam_conflicts(best, signatures)
    if layout_ambiguous or blank_conflicts:
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
    best['blank_seam_conflict'] = bool(blank_conflicts)
    best['blank_seam_conflict_count'] = blank_conflicts
    return best


def _blank_seam_conflicts(layout, signatures):
    """Count strong unique connections that an internal blank run disconnects.

    Outer borders and blanks with no vector continuation carry no penalty.
    Require at least two independent crossings and a mutual match separated
    from every competing page by 0.1, so isolated marks/repeated guides do not
    turn an otherwise legitimate blank into a proven continuity conflict.
    """
    rows, columns = layout['rows'], layout['columns']
    slots, order = layout['page_slots'], layout['order']
    conflicts = 0
    for horizontal in (True, False):
        line_count, line_length = (rows, columns) if horizontal else (columns, rows)
        first_side, second_side = ('right', 'left') if horizontal else ('bottom', 'top')
        for line in range(line_count):
            previous_page = None
            has_blank = False
            for position in range(line_length):
                row, column = (line, position) if horizontal else (position, line)
                page = _page_at(slots, rows, columns, order, row, column)
                if page is None:
                    has_blank = previous_page is not None
                    continue
                if has_blank and _strong_unique_seam(
                    previous_page, page, first_side, second_side, signatures,
                ):
                    conflicts += 1
                previous_page = page
                has_blank = False
    return conflicts


def _strong_unique_seam(first, second, first_side, second_side, signatures):
    first_values = _dedupe(signatures[first][first_side])
    second_values = _dedupe(signatures[second][second_side])
    if min(len(first_values), len(second_values)) < 2:
        return False
    score = _match_score(first_values, second_values)
    if score < 0.9:
        return False
    for index, edges in enumerate(signatures):
        if index in (first, second):
            continue
        forward = _match_score(first_values, edges[second_side]) or 0.0
        backward = _match_score(edges[first_side], second_values) or 0.0
        if max(forward, backward) > score - 0.1 + 1e-9:
            return False
    return True


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
    margins = crop_mm if isinstance(crop_mm, dict) else dict.fromkeys(EDGE_NAMES, crop_mm)
    left = margins['left'] * MM_TO_PT
    right = width - margins['right'] * MM_TO_PT
    top = margins['top'] * MM_TO_PT
    bottom = height - margins['bottom'] * MM_TO_PT
    signatures = []
    for segments in segments_by_page:
        edges = {name: [] for name in EDGE_NAMES}
        for start, end in segments:
            # A rectangular crop guide must not contribute four fake corner crossings.
            if any(abs(start[axis] - boundary) <= BOUNDARY_EPS_PT
                   and abs(end[axis] - boundary) <= BOUNDARY_EPS_PT
                   for axis, boundary in ((0, left), (0, right), (1, top), (1, bottom))):
                continue
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


def _optimize_blank_positions(candidate, signatures, page_count, score_cache=None):
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
            result = _evaluate_layout(rows, columns, order, slots, signatures, score_cache)
            scored.append((sequence, _rank_result(result)))
        scored.sort(key=lambda item: item[1], reverse=True)
        beam = scored[:beam_width]

    return max(
        (
            _evaluate_layout(rows, columns, order, list(sequence), signatures, score_cache)
            for sequence, _score in beam
        ),
        key=_rank_result,
    )


@lru_cache(maxsize=MAX_GRID_SIZE * MAX_GRID_SIZE * 2)
def _layout_adjacencies(rows, columns, order):
    """Cache grid topology only; visit seams in the original evaluation order."""
    pairs = []
    for row in range(rows):
        for column in range(columns):
            index = column * rows + row if order == 'column' else row * columns + column
            if column + 1 < columns:
                pairs.append((index, index + (rows if order == 'column' else 1), 'right', 'left'))
            if row + 1 < rows:
                pairs.append((index, index + (1 if order == 'column' else columns), 'bottom', 'top'))
    return tuple(pairs)


def _seam_score(first, second, first_side, second_side, signatures, score_cache):
    if score_cache is None:
        return _match_score(signatures[first][first_side], signatures[second][second_side])
    key = (first, second, first_side)
    if key not in score_cache:
        score_cache[key] = _match_score(signatures[first][first_side], signatures[second][second_side])
    return score_cache[key]


def _evaluate_layout(rows, columns, order, page_slots, signatures, score_cache=None):
    scores = []
    slots = page_slots
    if len(slots) < rows * columns:
        slots = list(slots) + [None] * (rows * columns - len(slots))
    for index, adjacent_index, first_side, second_side in _layout_adjacencies(rows, columns, order):
        page, adjacent = slots[index], slots[adjacent_index]
        if page is None or adjacent is None:
            continue
        score = _seam_score(page, adjacent, first_side, second_side, signatures, score_cache)
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
    # Rotation and mm/pt round trips can put an endpoint a few float ulps
    # outside its exact clipping boundary. This is precision tolerance, not crop padding.
    first = boundary if abs(start[axis] - boundary) <= 1e-4 else start[axis]
    second = boundary if abs(end[axis] - boundary) <= 1e-4 else end[axis]
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


def _extract_vector_segments(document, fitz, ignore_internal_guides=False, ignore_crop_guides=True, page_indexes=None):
    sizes = []
    pages = []
    for page_index in normalize_pdf_page_indexes(page_indexes, document.page_count):
        page = document.load_page(page_index)
        sizes.append((float(page.rect.width), float(page.rect.height)))
        rotation_matrix = getattr(page, 'rotation_matrix', None)
        segments = []
        drawings = native_clip_drawings(page)
        for drawing, visible_crop in iter_native_clipped_drawings(page, drawings, fitz, page.rect):
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
                    # Preserve guide classification on the original segment:
                    # clipping a diagonal to a tiny span must not turn it into a guide.
                    if ignore_crop_guides and _is_red_crop_guide_segment(
                        (start.x, start.y), (end.x, end.y), color,
                        float(page.rect.width), float(page.rect.height),
                    ):
                        continue
                    for visible_points in clip_page_polyline(
                        [start, end], page.rect.width, page.rect.height, fitz, visible_crop,
                    ):
                        segments.extend(
                            ((float(first.x), float(first.y)), (float(second.x), float(second.y)))
                            for first, second in itertools.pairwise(visible_points)
                        )
        pages.append(segments)
    return sizes, pages


def _extract_crop_candidates(document, fitz, page_indexes):
    """Use actual clipping/drawn rectangles as bounded asymmetric crop hypotheses.

    Clips take priority; repeated and larger frames precede small internal shapes.
    Only 16 hypotheses are seam-tested, independently of path complexity.
    The uniform search remains available when a PDF has no identifiable frame.
    """
    if not hasattr(document, 'load_page'):
        return []
    candidates = {}
    for index in page_indexes:
        page = document.load_page(index)
        width, height = float(page.rect.width), float(page.rect.height)
        try:
            drawings = page.get_drawings(extended=True)
        except TypeError:
            drawings = page.get_drawings()
        for drawing in drawings:
            is_clip = drawing.get('type') == 'clip'
            rectangles = [drawing.get('scissor')] if is_clip else [
                item[1] for item in drawing.get('items', []) if item[0] == 're'
            ]
            for rectangle in rectangles:
                if rectangle is None:
                    continue
                rectangle = fitz.Rect(rectangle)
                matrix = getattr(page, 'rotation_matrix', None)
                if matrix is not None:
                    rectangle = rectangle * matrix
                values = (rectangle.x0, width - rectangle.x1,
                          rectangle.y0, height - rectangle.y1)
                if not all(math.isfinite(value) and -1e-5 <= value <= 100 * MM_TO_PT
                           for value in values):
                    continue
                if rectangle.width < MM_TO_PT or rectangle.height < MM_TO_PT:
                    continue
                margins = tuple(max(0.0, value) / MM_TO_PT for value in values)
                # Quantization groups float noise only; keep original precise geometry.
                key = tuple(round(value, 4) for value in margins)
                candidate = candidates.setdefault(key, {
                    'margins': dict(zip(EDGE_NAMES, margins)), 'pages': set(),
                    'clip': False, 'area': rectangle.width * rectangle.height,
                })
                candidate['pages'].add(index)
                candidate['clip'] = candidate['clip'] or is_clip
    ranked = sorted(candidates.values(), key=lambda item: (
        item['clip'], len(item['pages']), item['area'],
    ), reverse=True)
    return [item['margins'] for item in ranked[:16]]


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


def _size_mismatch_reason(page_sizes, page_indexes):
    """Name the pages that differ from the most common size, or keep the generic reason."""
    default_reason = '页面尺寸不一致，暂时无法可靠判断接缝'
    clusters = []
    for index, size in enumerate(page_sizes):
        for cluster in clusters:
            width, height = cluster['size']
            if abs(size[0] - width) <= 0.5 and abs(size[1] - height) <= 0.5:
                cluster['indexes'].append(index)
                break
        else:
            clusters.append({'size': size, 'indexes': [index]})
    if not clusters:
        return default_reason
    majority = max(clusters, key=lambda cluster: len(cluster['indexes']))
    if sum(1 for cluster in clusters
           if len(cluster['indexes']) == len(majority['indexes'])) > 1:
        return default_reason
    majority_width, majority_height = majority['size']
    differing = [
        index for index, (width, height) in enumerate(page_sizes)
        if abs(width - majority_width) > 0.5 or abs(height - majority_height) > 0.5
    ]
    if not differing:
        return default_reason
    page_numbers = '、'.join(str(page_indexes[index] + 1) for index in differing)
    return f'第 {page_numbers} 页尺寸与其他页不同（可能是封面或效果图），请删除这些页后再识别'


def _embedded_layout_suggestion(metadata, page_indexes=None):
    return {
        'rows': metadata['rows'],
        'columns': metadata['columns'],
        'order': metadata['order'],
        'page_slots': [index if page_indexes is None or index in page_indexes else None
                       for index in metadata['page_slots']],
        'crop_margins_mm': metadata['crop_margins_mm'],
        'preserve_outer_edges': False,
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
