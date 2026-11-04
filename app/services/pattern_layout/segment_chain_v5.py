"""原 segment 闭合链证书和局部重复归属候选；不把几何角色当语义独占。

接口输入与既有 pieces/paths JSON 一致。不读文件、不修改输入、不补线、不缓冲。
只接纳完整原 segment 严格位于候选边界的支持；path 中其他 segment
和外来交叉不否定闭合链，但仍保存在 local_topology 中供语义层处理。
"""
import copy
import math
from collections import defaultdict
from shapely.geometry import LineString, Polygon, Point
from shapely.strtree import STRtree


def _epsilon(value):
    if not math.isfinite(value) or value < 0:
        raise ValueError('numeric epsilon must be finite and nonnegative')


def _segments(paths):
    seen = set()
    for path in paths:
        pid = path['id']
        if pid in seen:
            raise ValueError('duplicate source path id')
        seen.add(pid)
        points = path.get('points', [])
        for p in points:
            if len(p) != 2 or not all(math.isfinite(v) for v in p):
                raise ValueError('source coordinates must be finite 2D points')
        for index, (a, b) in enumerate(zip(points, points[1:])):
            if a != b:
                yield {'path_id': pid, 'segment_index': index,
                       'a': copy.deepcopy(a), 'b': copy.deepcopy(b)}, LineString([a, b])


def _polygon(outer):
    try:
        if any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in outer):
            return None
        poly = Polygon(outer)
        return poly if poly.is_valid and not poly.is_empty and poly.area > 0 else None
    except (TypeError, ValueError):
        return None


def certify_boundary_chain(outer, paths, *, numeric_epsilon_mm):
    """公开入口；要求 Shapely 2.1.2 的 STRtree 索引返回约定。"""
    _epsilon(numeric_epsilon_mm)
    segments = list(_segments(paths))
    return _certify_boundary_chain(outer, segments, STRtree([line for _, line in segments]))


def _certify_boundary_chain(outer, segments, source_tree):
    """证明整圈由完整原段组成。允许一个 path 混有边界、内线和外尾。

    interval epsilon 不用于填缝。边界覆盖使用 GEOS 严格差集空判定；其
    数值假阴性将显式 defer。numeric_epsilon_mm 仅验证引用端点，不扩边。
    原段次序可与环方向相反；每条环边证书保存所有交叠原段及原 t 区间。
    """
    result = {'version': 'raw-segment-chain-v5-1', 'complete': False,
              'source_geometry_changed': False, 'buffer_or_gap_fill_used': False,
              'boundary_support': [], 'edge_coverage': [], 'local_topology': [],
              'reasons': [], 'limitations': [
                  '完整原段严格边界差集的浮点假阴性会保守拒绝，不据此认定原线缺失',
                  '完整闭合链证明几何支撑，不证明裁片身份或内线不能双用']}
    poly = _polygon(outer)
    if poly is None:
        result['reasons'].append('invalid_candidate_polygon')
        return result
    supported = []
    # Shapely 2.1.2：predicate 在 GEOS 中过滤，原线段不因索引改变。
    for source_index in sorted(source_tree.query(poly.boundary, predicate='intersects').tolist()):
        ref, line = segments[source_index]
        if line.difference(poly.boundary).is_empty:
            supported.append((ref, line))
            result['boundary_support'].append(dict(copy.deepcopy(ref), t0=0., t1=1.))
        elif line.intersects(poly.boundary):
            result['local_topology'].append(dict(copy.deepcopy(ref),
                relation='boundary_crossing' if line.crosses(poly) else 'boundary_contact',
                intersection_geometry=line.intersection(poly.boundary).__geo_interface__,
                invalidates_boundary_chain=False, semantic_role='unresolved'))
    support_tree = STRtree([line for _, line in supported])
    # 逐环边差集而非合并整网再差集，避免全路径混合及 union 数值误差误拒。
    for index, (a, b) in enumerate(zip(poly.exterior.coords, list(poly.exterior.coords)[1:])):
        edge = LineString([a, b])
        if edge.length == 0:
            continue
        missing = edge
        witnesses = []
        for support_index in sorted(support_tree.query(edge, predicate='intersects').tolist()):
            ref, line = supported[support_index]
            hit = edge.intersection(line)
            if hit.is_empty or hit.length == 0:
                continue
            # 投影只记录证书区间，不用投影后的坐标作新的覆盖判定。
            parts = list(hit.geoms) if hasattr(hit, 'geoms') else [hit]
            for part in parts:
                if part.geom_type != 'LineString' or part.length == 0:
                    continue
                ts = [line.project(Point(p), normalized=True)
                      for p in part.coords]
                witnesses.append(dict(copy.deepcopy(ref), t0=min(ts), t1=max(ts)))
            missing = missing.difference(line)
        result['edge_coverage'].append({'outer_edge_index': index, 'a': list(a), 'b': list(b),
            'covered': missing.is_empty, 'unproven_length_mm': missing.length,
            'source_intervals': witnesses})
    result['complete'] = bool(result['edge_coverage']) and all(e['covered'] for e in result['edge_coverage'])
    result['reasons'] = [] if result['complete'] else ['complete_original_segment_boundary_chain_not_proven']
    return result


def analyze_source_overlap(pieces, paths, *, numeric_epsilon_mm, interval_epsilon=1e-12):
    """给父层提供区间候选和所有原引用备份。没有自动删除规则。

    proposed_after_owner_piece_ids 仅表示可对照的边界独占假设；
    auto_applicable 永远为 False，不能将其作为自动应用队列。
    """
    _epsilon(numeric_epsilon_mm)
    _epsilon(interval_epsilon)
    segments = list(_segments(paths))  # 验证重复 id 和坐标，不能静默覆盖来源。
    source_tree = STRtree([line for _, line in segments])
    raw = {p['id']: p for p in paths}
    if len({p['id'] for p in pieces}) != len(pieces):
        raise ValueError('duplicate piece id')
    polys = {p['id']: _polygon(p.get('outer', [])) for p in pieces}
    chains = {p['id']: _certify_boundary_chain(p.get('outer', []), segments, source_tree) for p in pieces}
    refs = defaultdict(list)
    invalid = []
    for piece in pieces:
        for f in piece.get('fragments', []):
            try:
                si = f['segment_index']; lo = f['t0']; hi = f['t1']
                if isinstance(si, bool) or not isinstance(si, int) or si < 0:
                    raise ValueError()
                a, b = raw[f['path_id']]['points'][si:si+2]
                if not all(math.isfinite(v) for v in (lo, hi)) or not 0 <= lo < hi <= 1 or a == b:
                    raise ValueError()
                for key, t in [('a', lo), ('b', hi)]:
                    expected = [a[k] + (b[k]-a[k])*t for k in (0, 1)]
                    if len(f[key]) != 2 or not all(math.isfinite(v) for v in f[key]) or math.dist(f[key], expected) > numeric_epsilon_mm:
                        raise ValueError()
                refs[(f['path_id'], si)].append((piece['id'], f))
            except (KeyError, TypeError, ValueError):
                invalid.append({'piece_id': piece['id'], 'fragment': copy.deepcopy(f),
                                'reason': 'invalid_source_reference'})
    audit = {'version': 'segment-chain-overlap-v5-1', 'chains': chains, 'candidates': [],
             'deferred': [], 'protected_shared': [], 'applied': [], 'invalid_references': invalid,
             'automatic_ownership_change_implemented': False,
             'limitations': ['边界链与另一片内部或外部关系仍不排除有意双用或外置标记，须保留原归属',
                             '无可执行的语义排他证书时仅返回对照候选，不隐去重复或待核项']}
    for (pid, si), rows in refs.items():
        cuts = sorted({f[t] for _, f in rows for t in ('t0', 't1')})
        a, b = raw[pid]['points'][si:si+2]
        full = LineString([a, b])
        for lo, hi in zip(cuts, cuts[1:]):
            if hi-lo <= interval_epsilon:
                continue
            active = [(oid, f) for oid, f in rows if f['t0'] <= lo and f['t1'] >= hi]
            ids = sorted({oid for oid, _ in active}, key=str)
            if len(ids) < 2:
                continue
            ends = [[a[k]+(b[k]-a[k])*t for k in (0, 1)] for t in (lo, hi)]
            line = LineString(ends)
            roles = []
            for oid in ids:
                poly = polys[oid]
                proof = 'unproven'
                if poly is not None:
                    if full.difference(poly.boundary).is_empty:
                        role = 'boundary'; proof = 'full_original_segment_exact_boundary_implies_interval'
                    elif line.difference(poly.boundary).is_empty:
                        role = 'boundary'; proof = 'interpolated_interval_exact_boundary'
                    elif poly.contains(line) and line.disjoint(poly.boundary):
                        role = 'strict_interior'; proof = 'strict_interval_geometry'
                    elif line.disjoint(poly):
                        role = 'strict_exterior'; proof = 'strict_interval_geometry'
                    else:
                        role = 'crossing_or_contact'
                else:
                    role = 'unproven'
                roles.append({'piece_id': oid, 'role': role, 'proof': proof,
                              'complete_boundary_chain': chains[oid]['complete']})
            row = {'source_interval': {'path_id': pid, 'segment_index': si, 't0': lo, 't1': hi,
                                      'a': ends[0], 'b': ends[1]},
                   'owners': roles, 'before_owner_piece_ids': ids,
                   'after_owner_piece_ids': ids[:],
                   'before_fragments': [dict(copy.deepcopy(f), piece_id=oid) for oid, f in active],
                   'auto_applicable': False}
            row['after_fragments'] = copy.deepcopy(row['before_fragments'])
            if all(r['role'] == 'boundary' for r in roles):
                row['reasons'] = ['true_shared_boundary_preserved']
                audit['protected_shared'].append(row)
                continue
            targets = [r['piece_id'] for r in roles if r['role'] == 'boundary' and r['complete_boundary_chain']]
            row['reasons'] = ['semantic_exclusive_ownership_not_proven']
            if len(ids) == 2 and len(targets) == 1:
                row['proposed_after_owner_piece_ids'] = targets
                row['candidate_kind'] = 'boundary_chain_exclusive_ownership_counterfactual'
                row['required_evidence'] = ['机器可验证的来源语义排他证据，排除内线双用或外置标记；单纯几何关系不满足']
                audit['candidates'].append(copy.deepcopy(row))
            else:
                row['reasons'].append('no_unique_complete_boundary_chain_owner')
            audit['deferred'].append(row)
    audit['stats'] = {name+'_count': len(audit[name]) for name in
                      ('candidates', 'deferred', 'protected_shared', 'invalid_references', 'applied')}
    return audit
