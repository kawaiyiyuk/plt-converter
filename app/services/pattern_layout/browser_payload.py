"""精简浏览器识别结果负载：样式去重、整路径+局部片段混合压缩并剥离无用审计。

保留全部源路径几何/样式（供网页渲染与相位预览）、全部主/备/辅候选轮廓与
片段索引（供点选、排版与导出），并保留与服务端 pattern-layout-client-result
完全兼容的身份和统计字段。
"""
import json
import math
from collections import defaultdict

_CANDIDATE_OPTIONAL_FIELDS = (
    'text_ids',
    'parent_candidate_id',
    'variant_label_cn',
    'loop_id',
    'source_closed_path_ids',
    'area_mm2',
    'excluded_rectangle',
    'content_complete_for_region',
)


def _compact_fragments(raw_fragments, path_order, nonzero_segs, safe_whole):
    if not raw_fragments:
        return [], []
    orig_tuples = [[f['path_id'], f['segment_index'], f['t0'], f['t1']] for f in raw_fragments]
    by_path = defaultdict(list)
    for pid, si, t0, t1 in orig_tuples:
        if pid not in path_order:
            return [], orig_tuples
        by_path[pid].append((si, t0, t1))

    whole_path_ids = []
    partial_segments = []
    for pid, segs in by_path.items():
        nz = nonzero_segs.get(pid)
        if (safe_whole.get(pid) and nz
                and len(segs) == len(nz)
                and all(si == exp and t0 == 0 and t1 == 1 for (si, t0, t1), exp in zip(segs, nz))):
            whole_path_ids.append(pid)
        else:
            for si, t0, t1 in segs:
                partial_segments.append([pid, si, t0, t1])

    whole_path_ids.sort(key=path_order.__getitem__)
    rebuilt = [[pid, si, 0.0, 1.0] for pid in whole_path_ids for si in nonzero_segs[pid]]
    rebuilt.extend(partial_segments)
    rebuilt.sort(key=lambda f: (path_order[f[0]], f[1], f[2], f[3]))
    if rebuilt != orig_tuples:
        return [], orig_tuples
    return whole_path_ids, partial_segments


def _compact_candidate(candidate, path_order, nonzero_segs, safe_whole):
    if 'fragments' not in candidate and ('whole_path_ids' in candidate or 'partial_segments' in candidate):
        whole_path_ids = list(candidate.get('whole_path_ids', ()))
        partial_segments = list(candidate.get('partial_segments', ()))
    else:
        whole_path_ids, partial_segments = _compact_fragments(
            candidate.get('fragments', ()), path_order, nonzero_segs, safe_whole
        )
    compact = {
        'id': candidate['id'],
        'status': candidate['status'],
        'bounds': candidate.get('bounds'),
        'outer': candidate.get('outer'),
        'whole_path_ids': whole_path_ids,
        'partial_segments': partial_segments,
        'semantic_piece_verified': bool(candidate.get('semantic_piece_verified', False)),
    }
    if 'issues' in candidate:
        compact['issues'] = [
            {'code': issue['code']}
            for issue in candidate['issues']
            if isinstance(issue, dict) and 'code' in issue
        ]
    for key in _CANDIDATE_OPTIONAL_FIELDS:
        if key in candidate:
            compact[key] = candidate[key]
    return compact


def compact_browser_result(result):
    source = result.get('source') or {}
    units = float(source.get('units_per_mm') or 40.0)
    algo = source.get('algorithm_parameters') or {}
    eps = float(algo.get('numeric_epsilon_plotter_units', 1e-7)) / units

    styles = []
    style_ids = {}
    compact_paths = []
    path_order = {}
    nonzero_segs = {}
    safe_whole = {}
    for idx, path in enumerate(result.get('paths', ())):
        pid = path['id']
        path_order[pid] = idx
        pts = path['points']
        nz = []
        has_borderline = False
        for si, (a, b) in enumerate(zip(pts, pts[1:])):
            dist = math.dist(a, b)
            if dist > eps:
                nz.append(si)
                if dist <= 2.0 * eps:
                    has_borderline = True
        nonzero_segs[pid] = nz
        safe_whole[pid] = bool(nz) and not has_borderline

        identifiers = []
        for style in path.get('segment_styles', ()):
            key = json.dumps(style, sort_keys=True, separators=(',', ':'))
            sid = style_ids.get(key)
            if sid is None:
                sid = len(styles)
                style_ids[key] = sid
                styles.append(style)
            identifiers.append(sid)
        compact_paths.append({
            'id': pid,
            'points': pts,
            'pen': path['pen'],
            'line_width_units': path.get('line_width_units'),
            'segment_style_ids': identifiers,
        })

    unassigned = result.get('unassigned') or {}
    return {
        'schema_version': result.get('schema_version', 1),
        'source': result['source'],
        'style_table': styles,
        'paths': compact_paths,
        'non_path_shapes': result.get('non_path_shapes', []),
        'pieces': [_compact_candidate(c, path_order, nonzero_segs, safe_whole) for c in result.get('pieces', ())],
        'auxiliary': [_compact_candidate(c, path_order, nonzero_segs, safe_whole) for c in result.get('auxiliary', ())],
        'unassigned': {
            'count': unassigned.get('count', 0),
            'length_mm': unassigned.get('length_mm', 0.0),
        },
        'small_candidate_filter': result.get('small_candidate_filter'),
    }
