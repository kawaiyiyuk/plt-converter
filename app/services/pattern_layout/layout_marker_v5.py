"""严格原线菱形十字的四角重复关系，仅用于离线实验可恢复分流。

四件真实小裁片若具有相同原线和排布，几何上不可区分；本模块不证明语义。
必须最后执行；旧 boundary/ownership/unassigned 是分流前快照，不能误称为当前主片审计。
"""
import copy
import itertools
import math
from collections import defaultdict
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from .face_policy import digest

VERSION = 'four-corner-original-diamond-cross-v5'
DEFAULT_SPAN_TO_SYMBOL_RATIO = 20.0
SEMANTIC_RISK = 'four_real_small_pieces_with_identical_geometry_and_layout_are_indistinguishable'


def merged(parts):
    """区间只按精确重叠合并；物理长度比较统一使用调用方numeric epsilon。"""
    out = []
    for a, b in sorted(parts):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _covered(a, b, eps):
    """只用于数值证据，不用buffer创建或替换任何源线/候选。"""
    return a.difference(b.buffer(eps)).is_empty


def _same_lines(a, b, eps):
    return _covered(a, b, eps) and _covered(b, a, eps)


def _geometry(piece, eps):
    """由候选外边界检验轴向菱形，坐标仅构造比较模板，不写回结果。"""
    if piece.get('holes'):
        return None
    try:
        poly = Polygon(piece['outer'])
    except (ValueError, TypeError, KeyError):
        return None
    if not poly.is_valid or poly.is_empty or poly.interiors:
        return None
    x0, y0, x1, y1 = poly.bounds
    width, height = x1 - x0, y1 - y0
    if min(width, height) <= eps:
        return None
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    vertices = [(cx, y0), (x1, cy), (cx, y1), (x0, cy), (cx, y0)]
    diamond = LineString(vertices)
    if not _same_lines(poly.boundary, diamond, eps):
        return None
    diagonals = [LineString([(x0, cy), (x1, cy)]), LineString([(cx, y0), (cx, y1)])]
    return {'center': [cx, cy], 'width': width, 'height': height, 'bounds': [x0, y0, x1, y1],
            'expected': unary_union([diamond] + diagonals), 'polygon': poly}


def _source_certificate(piece, shape, paths, eps):
    """每个真实fragment须回指原segment，且所有参与原path完整落在同一符号上。"""
    errors = []
    intervals = defaultdict(list)
    lines = []
    used_paths = set()
    for f in piece.get('fragments', []):
        pid, si = f.get('path_id'), f.get('segment_index')
        source = paths.get(pid)
        if source is None or not isinstance(si, int) or si < 0 or si + 1 >= len(source['points']):
            errors.append('invalid_fragment_source_reference')
            continue
        t0, t1 = f.get('t0'), f.get('t1')
        if not isinstance(t0, (float, int)) or not isinstance(t1, (float, int)) or not (0 <= t0 < t1 <= 1):
            errors.append('invalid_fragment_interval')
            continue
        a, b = source['points'][si:si + 2]
        aa = [a[k] + (b[k] - a[k]) * t0 for k in (0, 1)]
        bb = [a[k] + (b[k] - a[k]) * t1 for k in (0, 1)]
        if (not isinstance(f.get('a'), (list, tuple)) or len(f['a']) != 2
                or not isinstance(f.get('b'), (list, tuple)) or len(f['b']) != 2):
            errors.append('missing_fragment_coordinates')
            continue
        if math.dist(aa, f['a']) > eps or math.dist(bb, f['b']) > eps:
            errors.append('fragment_coordinates_disagree_with_source')
            continue
        styles = source.get('segment_styles', [])
        source_style = styles[si] if si < len(styles) else None
        if f.get('style') != source_style:
            errors.append('fragment_style_disagrees_with_source')
        line = LineString([aa, bb])
        if line.length <= eps:
            continue
        intervals[(pid, si)].append([t0, t1])
        used_paths.add(pid)
        lines.append(line)
    actual = unary_union(lines)
    if not _same_lines(actual, shape['expected'], eps):
        errors.append('fragment_geometry_not_exact_diamond_and_two_diagonals')
    for pid in sorted(used_paths):
        source = paths[pid]
        for si, (a, b) in enumerate(zip(source['points'], source['points'][1:])):
            line = LineString([a, b])
            if line.length <= eps:
                continue
            # 参数区间以对应segment的物理长度换算同一个numeric epsilon。
            coverage = merged(intervals.get((pid, si), []))
            selected = sum(t1 - t0 for t0, t1 in coverage)
            if (1 - selected) * line.length > eps:
                errors.append('participating_original_path_not_completely_selected')
            if not _covered(line, shape['expected'], eps):
                errors.append('participating_original_path_has_extra_line')
    # 不能漏掉候选内未被选入的十字/多余线；额外重合来源也需完整回指。
    for pid, source in paths.items():
        for si, (a, b) in enumerate(zip(source['points'], source['points'][1:])):
            line = LineString([a, b])
            if line.length <= eps:
                continue
            hit = line.intersection(shape['polygon'])
            if hit.length <= eps:
                continue
            if (pid, si) not in intervals:
                errors.append('unselected_original_source_in_symbol')
            elif not _covered(hit, shape['expected'], eps):
                errors.append('extra_original_source_line_in_symbol')
    return {'blocking_reasons': sorted(set(errors)), 'source_path_ids': sorted(used_paths),
            'source_segment_intervals': [{'path_id': pid, 'segment_index': si, 'intervals': merged(parts)}
                                         for (pid, si), parts in sorted(intervals.items())],
            'numeric_buffer_for_comparison_only': True, 'source_points_changed': False}


def _shared_intervals(piece, all_pieces, eps, paths):
    conflicts = []
    for f in piece.get('fragments', []):
        pid, si = f.get('path_id'), f.get('segment_index')
        source = paths.get(pid)
        if source is None or not isinstance(si, int) or not 0 <= si < len(source['points']) - 1:
            continue
        length = math.dist(*source['points'][si:si + 2])
        for other in all_pieces:
            if other is piece:
                continue
            for q in other.get('fragments', []):
                if (q.get('path_id'), q.get('segment_index')) != (pid, si):
                    continue
                lo = max(f.get('t0', 0), q.get('t0', 0))
                hi = min(f.get('t1', 0), q.get('t1', 0))
                if (hi - lo) * length > eps:
                    conflicts.append({'other_piece_id': other['id'], 'path_id': pid,
                                      'segment_index': si, 't0': lo, 't1': hi})
    return conflicts


def _rectangle_group(group, raw_bounds, eps, ratio):
    """四中心只能占据矩形四角；源版图纵向两端要求上下符号外缘对齐。"""
    if max(x['width'] for x in group) - min(x['width'] for x in group) > eps:
        return None
    if max(x['height'] for x in group) - min(x['height'] for x in group) > eps:
        return None
    centers = [x['center'] for x in group]
    xs = [c[0] for c in centers]
    ys = [c[1] for c in centers]
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    symbol = max(max(x['width'], x['height']) for x in group)
    if min(right - left, bottom - top) < ratio * symbol:
        return None
    expected = [(left, top), (right, top), (left, bottom), (right, bottom)]
    used = set()
    for center in centers:
        matches = [i for i, pt in enumerate(expected) if math.dist(center, pt) <= eps]
        if len(matches) != 1 or matches[0] in used:
            return None
        used.add(matches[0])
    if abs(min(x['bounds'][1] for x in group) - raw_bounds[1]) > eps:
        return None
    if abs(max(x['bounds'][3] for x in group) - raw_bounds[3]) > eps:
        return None
    return {'piece_ids': sorted(x['piece_id'] for x in group),
            'center_rectangle': [left, top, right, bottom],
            'symbol_width_mm': group[0]['width'], 'symbol_height_mm': group[0]['height'],
            'span_to_max_symbol_ratio': [(right - left) / symbol, (bottom - top) / symbol],
            'longitudinal_source_extrema_aligned': True}


def classify_layout_markers(result, paths, *, numeric_epsilon_mm,
                            min_span_to_symbol_ratio=DEFAULT_SPAN_TO_SYMBOL_RATIO, apply=True):
    """最后执行：四角唯一组转auxiliary疑似标记，完整原candidate可恢复。

    20倍为AI自行假设，只是降低偶然排布风险，不证明四件小裁片不是裁片。
    任何多组（含不重叠的多组）均保留，避免程序自行挑选一种版面解释。
    """
    eps = numeric_epsilon_mm
    if not math.isfinite(eps) or eps <= 0:
        raise ValueError('numeric_epsilon_mm must be finite and positive')
    if not math.isfinite(min_span_to_symbol_ratio) or min_span_to_symbol_ratio <= 1:
        raise ValueError('min_span_to_symbol_ratio must be finite and greater than one')
    if len({p['id'] for p in paths}) != len(paths):
        raise ValueError('duplicate original source path id')
    pieces = result['pieces']
    if len({p['id'] for p in pieces}) != len(pieces):
        raise ValueError('duplicate candidate id')
    sources = {p['id']: p for p in paths}
    points = [pt for p in paths for pt in p['points']]
    if points:
        xs, ys = zip(*points)
        raw_bounds = [min(xs), min(ys), max(xs), max(ys)]
    else:
        raw_bounds = None
    decisions = []
    eligible = []
    for piece in pieces:
        original = copy.deepcopy(piece)
        row = {'piece_id': piece['id'], 'decision': 'retained_main_candidate',
               'blocking_reasons': [], 'original_record': original,
               'original_record_sha256': digest(original)}
        decisions.append(row)
        shape = _geometry(piece, eps)
        if shape is None:
            row['blocking_reasons'] = ['not_axis_aligned_diamond_outer']
            continue
        cert = _source_certificate(piece, shape, sources, eps)
        row['evidence'] = cert
        row['blocking_reasons'].extend(cert['blocking_reasons'])
        shared = _shared_intervals(piece, pieces, eps, sources)
        row['shared_source_interval_conflicts'] = shared
        if shared:
            row['blocking_reasons'].append('shared_source_interval_with_other_main_candidate')
        if piece.get('issues'):
            row['blocking_reasons'].append('candidate_has_unresolved_issues')
        if not row['blocking_reasons']:
            shape['piece_id'] = piece['id']
            eligible.append(shape)
    groups = []
    if raw_bounds:
        # 限制组合探索；超出明确保留，避免组合爆炸。
        # 这里无隐式采样，超过上限时整轮不进行分流。
        if len(eligible) <= 32:
            for group in itertools.combinations(eligible, 4):
                proof = _rectangle_group(group, raw_bounds, eps, min_span_to_symbol_ratio)
                if proof:
                    groups.append(proof)
    conflict = []
    if len(eligible) > 32:
        conflict.append('eligible_symbol_count_exceeds_explicit_32_limit_no_diversion')
    if len(groups) > 1:
        conflict.append('multiple_rectangle_groups_no_unique_layout_interpretation')
    selected = set(groups[0]['piece_ids']) if len(groups) == 1 and not conflict else set()
    changes = []
    for row in decisions:
        if row['piece_id'] not in selected:
            if not row['blocking_reasons']:
                row['blocking_reasons'] = conflict[:] or ['no_unique_four_corner_extrema_group']
            continue
        row['decision'] = 'suspected_layout_registration_marker'
        row['semantic_risks'] = [SEMANTIC_RISK, 'geometric_repetition_is_not_semantic_ground_truth']
        original = row['original_record']
        changes.append({'piece_id': row['piece_id'], 'from': 'pieces', 'to': 'auxiliary',
                        'original_record_sha256': row['original_record_sha256']})
        if apply:
            auxiliary = copy.deepcopy(original)
            auxiliary['status'] = 'suspected_layout_registration_marker'
            auxiliary['layout_marker_evidence'] = {
                'version': VERSION, 'original_record': copy.deepcopy(original),
                'original_record_sha256': row['original_record_sha256'],
                'source_certificate': copy.deepcopy(row['evidence']),
                'group': copy.deepcopy(groups[0]), 'semantic_risks': row['semantic_risks'],
                'restore_target': 'pieces', 'source_fragments_preserved': True}
            result['auxiliary'].append(auxiliary)
    if apply and selected:
        result['pieces'] = [piece for piece in pieces if piece['id'] not in selected]
    audit = {'version': VERSION, 'decisions': decisions, 'groups': groups, 'conflicts': conflict,
             'changes': changes, 'applied_count': len(changes) if apply else 0,
             'proposed_count': len(changes), 'apply': apply,
             'rules': {'symbol': 'axis-aligned diamond plus its two complete diagonals',
                       'four_identical_symbols_axis_aligned_rectangle': True,
                       'source_vertical_extrema_alignment_required': True,
                       'numeric_epsilon_mm': eps,
                       'min_span_to_max_symbol_ratio': min_span_to_symbol_ratio,
                       'eligible_symbol_count_limit': 32,
                       'ai_added_assumptions': ['minimum span ratio is an experimental heuristic',
                                                '32 eligible symbols limit defers entire diversion'],
                       'semantic_risk': SEMANTIC_RISK},
             'source_paths_changed': False,
             'prior_audits_scope': 'pre_layout_marker_diversion_snapshot',
             'prior_audits_not_current_main_piece_ownership_truth': bool(apply and selected),
             'main_piece_count_before': len(pieces),
             'main_piece_count_after': len(result['pieces']),
             'raw_source_bounds': raw_bounds}
    return audit
