"""多笔近接端点拼环：识别外壳只用于几何判断，导出仍用原始线段。"""
import math
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree
from .closed_loop import bounds_of_points, interval_parts, polygon_coords
from .fragment_index import PathSpatialIndex, collect_region_fragments

JOINED_ISSUE = 'source_strokes_joined_for_recognition'


def _endpoints(paths, excluded_path_ids):
    endpoints = []
    for path in paths:
        if path['id'] in excluded_path_ids:
            continue
        points = path['points']
        if len(points) < 2 or points[0] == points[-1]:
            continue
        for side, point in (('start', points[0]), ('end', points[-1])):
            endpoints.append({'index': len(endpoints), 'path_id': path['id'], 'side': side,
                              'point': (float(point[0]), float(point[1]))})
    return endpoints


def _unique_endpoint_pairs(endpoints, tolerance):
    """只保留互为唯一近邻的跨笔画端点；遇第二个候选项立即判歧义。"""
    if not endpoints:
        return {}
    tree = STRtree([Point(item['point']) for item in endpoints])
    unique = {}
    for local_i, item in enumerate(endpoints):
        found = None
        ambiguous = False
        for raw_index in tree.query(Point(item['point']), predicate='dwithin', distance=tolerance):
            local_j = int(raw_index)
            if endpoints[local_j]['path_id'] == item['path_id']:
                continue
            if found is None:
                found = local_j
            elif local_j != found:
                ambiguous = True
                break
        unique[local_i] = None if ambiguous else found
    pairs = {}
    for local_i, candidate in unique.items():
        if candidate is not None and unique.get(candidate) == local_i:
            idx_a = endpoints[local_i]['index']
            idx_b = endpoints[candidate]['index']
            pairs[idx_a] = idx_b
            pairs[idx_b] = idx_a
    return pairs


def _tiered_endpoint_pairs(endpoints, tiers, epsilon):
    """按递增容差逐层锁定唯一跨笔画配对，避免较远相邻轮廓拆散近邻接缝。"""
    clean_tiers = []
    for tier in tiers:
        value = float(tier)
        if not clean_tiers or value > clean_tiers[-1]:
            clean_tiers.append(value)
    if not clean_tiers:
        return {}
    if len(clean_tiers) == 1:
        return _unique_endpoint_pairs(endpoints, clean_tiers[0] + epsilon)
    pairs = {}
    remaining = list(endpoints)
    for tier in clean_tiers:
        if len(remaining) < 2:
            break
        layer_pairs = _unique_endpoint_pairs(remaining, tier + epsilon)
        if layer_pairs:
            pairs.update(layer_pairs)
            matched = set(layer_pairs)
            remaining = [item for item in remaining if item['index'] not in matched]
    return pairs


def _self_closing_chains(adjacency, endpoints, tolerance):
    """找出无分叉且首尾在容差内的多笔开链，供宽松模式局部闭合识别。"""
    seen = set()
    chains = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        component = []
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbour in adjacency[node]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        ends = [node for node in component if len(adjacency[node]) == 1]
        if len(component) >= 4 and len(ends) == 2 and all(len(adjacency[node]) in (1, 2) for node in component):
            a, b = ends[0], ends[1]
            if endpoints[a]['path_id'] != endpoints[b]['path_id'] and math.dist(endpoints[a]['point'], endpoints[b]['point']) <= tolerance:
                chains.append((component, a, b))
    return chains


def _endpoint_graph(endpoints, pairs):
    path_ends = {}
    for item in endpoints:
        path_ends.setdefault(item['path_id'], {})[item['side']] = item['index']
    adjacency = {item['index']: [] for item in endpoints}
    for item in endpoints:
        sibling = path_ends[item['path_id']]['end' if item['side'] == 'start' else 'start']
        adjacency[item['index']].append(sibling)
    for first, second in pairs.items():
        if first < second:
            adjacency[first].append(second)
            adjacency[second].append(first)
    return adjacency, path_ends


def _cycles(adjacency):
    seen = set()
    cycles = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        component = []
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbour in adjacency[node]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        if len(component) >= 4 and all(len(adjacency[node]) == 2 for node in component):
            cycles.append(component)
    return cycles


def _walk_cycle(start, endpoints, pairs, path_ends):
    node = start
    ordered = []
    used = set()
    while True:
        item = endpoints[node]
        path_id = item['path_id']
        if path_id in used:
            return None
        used.add(path_id)
        sibling = path_ends[path_id]['end' if item['side'] == 'start' else 'start']
        ordered.append({'path_id': path_id, 'forward': item['side'] == 'start'})
        node = sibling
        next_node = pairs.get(node)
        if next_node is None:
            return None
        node = next_node
        if node == start:
            return ordered
        if len(ordered) > len(endpoints) // 2:
            return None


def _ring_for_cycle(ordered, paths_by_id):
    ring = []
    for step in ordered:
        points = paths_by_id[step['path_id']]['points']
        sequence = points if step['forward'] else list(reversed(points))
        ring.extend((float(point[0]), float(point[1])) for point in sequence)
    seams = []
    for index, step in enumerate(ordered):
        points = paths_by_id[step['path_id']]['points']
        exit_point = points[-1] if step['forward'] else points[0]
        following = ordered[(index + 1) % len(ordered)]
        next_points = paths_by_id[following['path_id']]['points']
        entry_point = next_points[0] if following['forward'] else next_points[-1]
        seams.append(math.dist(exit_point, entry_point))
    return ring, seams


def _dedupe_ring(ring, epsilon):
    result = []
    for point in ring:
        if not result or math.dist(point, result[-1]) > epsilon:
            result.append(point)
    if len(result) > 1 and math.dist(result[0], result[-1]) <= epsilon:
        result.pop()
    return result


def source_contour_fragments(paths, member_ids, polygon, epsilon, *, path_index=None):
    """单笔与多笔候选共用的源片段提取；只在成员笔画上取完整原段。"""
    index = path_index if path_index is not None else PathSpatialIndex(paths)
    return collect_region_fragments(index, member_ids, polygon, epsilon)


def discover_multi_stroke_alternatives(paths, excluded_path_ids, epsilon, params, *, skip_identities=frozenset(), path_index=None):
    """返回 (候选, 多边形) 列表；不生成任何合成 path 或 fragment。"""
    tolerance = params['multi_stroke_join_tolerance_mm'] + epsilon
    tiers = params.get('multi_stroke_join_tiers_mm')
    allow_self_closing = bool(params.get('allow_self_closing_chains', False))
    paths_by_id = {path['id']: path for path in paths}
    path_order = {path['id']: index for index, path in enumerate(paths)}
    index = path_index if path_index is not None else PathSpatialIndex(paths)
    endpoints = _endpoints(paths, excluded_path_ids)
    pairs = _tiered_endpoint_pairs(endpoints, tiers, epsilon) if tiers else _unique_endpoint_pairs(endpoints, tolerance)
    adjacency, path_ends = _endpoint_graph(endpoints, pairs)
    tasks = [(component, pairs, False) for component in _cycles(adjacency)]
    if allow_self_closing:
        tasks.extend((component, {**pairs, a: b, b: a}, True) for component, a, b in _self_closing_chains(adjacency, endpoints, tolerance))
    alternatives = []
    for component, cycle_pairs, is_self_closing in tasks:
        ordered = _walk_cycle(min(component), endpoints, cycle_pairs, path_ends)
        if ordered is None or len(ordered) != len(component) // 2:
            continue
        member_ids = sorted(step['path_id'] for step in ordered)
        if len(member_ids) < 2 or len(set(member_ids)) != len(member_ids):
            continue
        if frozenset(member_ids) in skip_identities:
            continue
        ring, seams = _ring_for_cycle(ordered, paths_by_id)
        if not seams:
            continue
        if not is_self_closing and max(seams) <= epsilon:
            continue
        if any(seam > tolerance for seam in seams):
            continue
        ring = _dedupe_ring(ring, epsilon)
        if len(ring) < 3:
            continue
        polygon = Polygon(ring)
        if polygon.is_empty or not polygon.is_valid:
            continue
        if polygon.area < params['min_candidate_area_mm2']:
            continue
        candidate = {'id': 'SJ-' + min(member_ids, key=path_order.__getitem__),
                     'status': 'independent_source_contour_needs_review',
                     'bounds': bounds_of_points(ring), 'outer': polygon_coords(polygon),
                     'fragments': source_contour_fragments(paths, set(member_ids), polygon, epsilon, path_index=index),
                     'source_closed_path_ids': member_ids, 'semantic_piece_verified': False,
                     'content_complete_for_region': True,
                     'issues': [{'code': JOINED_ISSUE, 'seam_count': len(seams),
                                 'max_seam_mm': max(seams)},
                                {'code': 'interior_geometry_may_include_other_real_pieces_or_sizes'},
                                {'code': 'not_semantic_piece_truth_no_exclusive_owner_assumed'}]}
        alternatives.append((candidate, polygon))
    return sorted(alternatives, key=lambda item: item[0]['id'])
