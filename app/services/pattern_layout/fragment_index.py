"""共享源路径空间索引与区域片段快速提取。

保持原 paths 顺序、原端点与浮点结果；严格内部路径直接整段映射，
跨界或贴边路径仍下钻逐段求交，不近似、不改坐标。
"""
import math
from shapely import STRtree, prepare
from shapely.geometry import LineString, Point, box


def _segments(geom):
    if geom.is_empty:
        return []
    if geom.geom_type in ('LineString', 'LinearRing'):
        return [geom]
    return [part for sub in getattr(geom, 'geoms', ()) for part in _segments(sub)]


def interval_parts_fast(a, b, region, eps, bounds=None):
    if bounds is not None:
        if not bounds:
            return []
        x0, y0, x1, y1 = bounds
        if max(a[0], b[0]) < x0 or min(a[0], b[0]) > x1 or max(a[1], b[1]) < y0 or min(a[1], b[1]) > y1:
            return []
    if math.dist(a, b) <= eps:
        return []
    line = LineString([a, b])
    length = line.length
    if length <= eps:
        return []
    result = []
    for part in _segments(line.intersection(region)):
        if part.length <= eps:
            continue
        ts = [line.project(Point(p)) / length for p in part.coords]
        result.append((max(0, min(ts)), min(1, max(ts))))
    return sorted(result)


class PathSpatialIndex:
    """对原 paths 建一次 STRtree，供主候选与备选区域提取复用。"""

    def __init__(self, paths, lines_by_id=None):
        self.paths = paths
        self.path_index_by_id = {}
        self.lines = []
        self.seg_lengths = []
        self.seg_offsets = []
        tree_geoms = []
        self._tree_to_path_idx = []
        for idx, path in enumerate(paths):
            pid = path['id']
            self.path_index_by_id[pid] = idx
            points = path['points']
            line = lines_by_id.get(pid) if lines_by_id is not None else (
                LineString(points) if len(points) >= 2 else None
            )
            self.lines.append(line)
            if line is not None:
                self._tree_to_path_idx.append(idx)
                tree_geoms.append(line)
            lengths = []
            offsets = []
            acc = 0.0
            for a, b in zip(points, points[1:]):
                seg_len = math.dist(a, b)
                lengths.append(seg_len)
                offsets.append(acc)
                acc += seg_len
            self.seg_lengths.append(lengths)
            self.seg_offsets.append(offsets)
        self.tree = STRtree(tree_geoms) if tree_geoms else None

    def query_path_indices(self, bounds, extra_ids=()):
        indices = set()
        if self.tree is not None and bounds and len(bounds) == 4:
            x0, y0, x1, y1 = bounds
            env = box(x0, y0, x1, y1)
            for tree_idx in self.tree.query(env):
                indices.add(self._tree_to_path_idx[int(tree_idx)])
        for pid in extra_ids:
            idx = self.path_index_by_id.get(pid)
            if idx is not None:
                indices.add(idx)
        return sorted(indices)


def collect_region_fragments(
    path_index,
    member_ids,
    polygon,
    epsilon,
    *,
    assignment='geometric_region_ambiguous',
):
    """用 STRtree + prepare(poly).contains_properly(line) 提取区域内全部片段。"""
    if polygon.is_empty:
        return []
    poly_bounds = polygon.bounds
    prepare(polygon)
    fragments = []
    candidate_indices = path_index.query_path_indices(poly_bounds, extra_ids=member_ids)
    for idx in candidate_indices:
        source = path_index.paths[idx]
        sid = source['id']
        points = source['points']
        styles = source.get('segment_styles', ())
        seg_lengths = path_index.seg_lengths[idx]
        if sid in member_ids:
            for seg_idx, (a, b) in enumerate(zip(points, points[1:])):
                if seg_lengths[seg_idx] > epsilon:
                    fragments.append({
                        'path_id': sid,
                        'segment_index': seg_idx,
                        't0': 0.0,
                        't1': 1.0,
                        'a': [a[k] + (b[k] - a[k]) * 0.0 for k in (0, 1)],
                        'b': [a[k] + (b[k] - a[k]) * 1.0 for k in (0, 1)],
                        'style': styles[seg_idx] if seg_idx < len(styles) else None,
                        'assignment': assignment,
                    })
            continue
        line = path_index.lines[idx]
        if line is None or polygon.disjoint(line):
            continue
        if polygon.contains_properly(line):
            for seg_idx, (a, b) in enumerate(zip(points, points[1:])):
                if seg_lengths[seg_idx] > epsilon:
                    fragments.append({
                        'path_id': sid,
                        'segment_index': seg_idx,
                        't0': 0.0,
                        't1': 1.0,
                        'a': [a[k] + (b[k] - a[k]) * 0.0 for k in (0, 1)],
                        'b': [a[k] + (b[k] - a[k]) * 1.0 for k in (0, 1)],
                        'style': styles[seg_idx] if seg_idx < len(styles) else None,
                        'assignment': assignment,
                    })
            continue
        for seg_idx, (a, b) in enumerate(zip(points, points[1:])):
            selected = interval_parts_fast(a, b, polygon, epsilon, poly_bounds)
            for t0, t1 in selected:
                fragments.append({
                    'path_id': sid,
                    'segment_index': seg_idx,
                    't0': t0,
                    't1': t1,
                    'a': [a[k] + (b[k] - a[k]) * t0 for k in (0, 1)],
                    'b': [a[k] + (b[k] - a[k]) * t1 for k in (0, 1)],
                    'style': styles[seg_idx] if seg_idx < len(styles) else None,
                    'assignment': assignment,
                })
    return fragments
