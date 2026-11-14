"""多笔近接端点拼环：识别外壳只用于几何判断，导出仍用原始线段。"""
import math
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree
from .closed_loop import bounds_of_points, interval_parts, polygon_coords

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
    for item in endpoints:
        found = None
        ambiguous = False
        for raw_index in tree.query(Point(item['point']), predicate='dwithin', distance=tolerance):
            index = int(raw_index)
            if endpoints[index]['path_id'] == item['path_id']:
                continue
            if found is None:
                found = index
            elif index != found:
                ambiguous = True
                break
        unique[item['index']] = None if ambiguous else found
    pairs = {}
    for index, candidate in unique.items():
        if candidate is not None and unique.get(candidate) == index:
            pairs[index] = candidate
            pairs[candidate] = index
    return pairs


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


def source_contour_fragments(paths, member_ids, polygon, epsilon):
    """单笔与多笔候选共用的源片段提取；只在成员笔画上取完整原段。"""
    polygon_bounds = polygon.bounds
    fragments = []
    for source in paths:
        for index, (a, b) in enumerate(zip(source['points'], source['points'][1:])):
            selected = ([(0., 1.)] if source['id'] in member_ids and math.dist(a, b) > epsilon
                        else interval_parts(a, b, polygon, epsilon, polygon_bounds))
            for t0, t1 in selected:
                fragments.append({'path_id': source['id'], 'segment_index': index, 't0': t0, 't1': t1,
                                  'a': [a[k] + (b[k] - a[k]) * t0 for k in (0, 1)],
                                  'b': [a[k] + (b[k] - a[k]) * t1 for k in (0, 1)],
                                  'style': source['segment_styles'][index],
                                  'assignment': 'geometric_region_ambiguous'})
    return fragments


def discover_multi_stroke_alternatives(paths, excluded_path_ids, epsilon, params):
    """返回 (候选, 多边形) 列表；不生成任何合成 path 或 fragment。"""
    tolerance = params['multi_stroke_join_tolerance_mm'] + epsilon
    paths_by_id = {path['id']: path for path in paths}
    path_order = {path['id']: index for index, path in enumerate(paths)}
    endpoints = _endpoints(paths, excluded_path_ids)
    pairs = _unique_endpoint_pairs(endpoints, tolerance)
    adjacency, path_ends = _endpoint_graph(endpoints, pairs)
    alternatives = []
    for component in _cycles(adjacency):
        ordered = _walk_cycle(min(component), endpoints, pairs, path_ends)
        if ordered is None or len(ordered) != len(component) // 2:
            continue
        member_ids = sorted(step['path_id'] for step in ordered)
        if len(member_ids) < 2 or len(set(member_ids)) != len(member_ids):
            continue
        ring, seams = _ring_for_cycle(ordered, paths_by_id)
        if not seams or max(seams) <= epsilon:
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
                     'fragments': source_contour_fragments(paths, set(member_ids), polygon, epsilon),
                     'source_closed_path_ids': member_ids, 'semantic_piece_verified': False,
                     'content_complete_for_region': True,
                     'issues': [{'code': JOINED_ISSUE, 'seam_count': len(seams),
                                 'max_seam_mm': max(seams)},
                                {'code': 'interior_geometry_may_include_other_real_pieces_or_sizes'},
                                {'code': 'not_semantic_piece_truth_no_exclusive_owner_assumed'}]}
        alternatives.append((candidate, polygon))
    return sorted(alternatives, key=lambda item: item[0]['id'])
