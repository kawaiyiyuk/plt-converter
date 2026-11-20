import hashlib
import json
import re
import unittest

from app.services.pattern_layout.browser_payload import compact_browser_result
from app.services.pattern_layout.network_components import partition_network
from app.services.pattern_layout_api import export_project, recognize_source
from app.services.pattern_layout_client_result import verify_client_result
from app.services.pattern_layout_fingerprint import (
    export_runtime_version,
    recognition_code_version,
    recognition_components,
)
from app.services.pattern_layout_variants import recognize_with_variants


RUNTIME = {'pyodide': '314.0.7', 'python': '3.14.0', 'numpy': '2.4.6', 'shapely': '2.1.2', 'geos': '3.12.1'}


def _strip_audits(candidate):
    fragments = [
        (f['path_id'], f['segment_index'], f['t0'], f['t1'], f['a'], f['b'], f['style'])
        for f in candidate['fragments']
    ]
    return {
        'id': candidate['id'],
        'status': candidate['status'],
        'bounds': candidate.get('bounds'),
        'outer': candidate.get('outer'),
        'fragments': fragments,
        'issues': [i['code'] for i in candidate.get('issues', ())],
        'text_ids': candidate.get('text_ids', []),
        'parent_candidate_id': candidate.get('parent_candidate_id'),
        'loop_id': candidate.get('loop_id'),
        'source_closed_path_ids': candidate.get('source_closed_path_ids'),
        'excluded_rectangle': candidate.get('excluded_rectangle'),
        'content_complete_for_region': candidate.get('content_complete_for_region'),
    }


def _client_payload(result, name='sample.plt'):
    def cand(c):
        out = {
            'id': c['id'],
            'status': c['status'],
            'outer': c['outer'],
            'fragments': [[f['path_id'], f['segment_index'], f['t0'], f['t1']] for f in c['fragments']],
        }
        if 'issues' in c:
            out['issues'] = [{'code': i['code']} for i in c['issues']]
        for key in (
            'parent_candidate_id', 'variant_label_cn', 'loop_id',
            'source_closed_path_ids', 'area_mm2', 'excluded_rectangle',
            'content_complete_for_region',
        ):
            if key in c:
                out[key] = c[key]
        return out

    return {
        'format': 'pattern-layout-client-result',
        'format_version': 1,
        'source': {
            'sha256': result['source']['sha256'],
            'name': name,
            'path_count': len(result['paths']),
            'text_count': sum(1 for s in result['non_path_shapes'] if s.get('type') == 'text'),
            'warnings': list(result['source'].get('warnings', ())),
        },
        'engine': {
            'engine_version': result['source']['engine_version'],
            'code_version': recognition_code_version(),
            'runtime_version': hashlib.sha256(b'p0-test-runtime').hexdigest(),
            'runtime': dict(RUNTIME),
        },
        'pieces': [cand(c) for c in result['pieces']],
        'auxiliary': [cand(c) for c in result['auxiliary']],
        'unassigned': {
            'count': result['unassigned']['count'],
            'length_mm': result['unassigned']['length_mm'],
        },
    }


class PatternLayoutP0PerfTests(unittest.TestCase):
    def _assert_equivalent_recognition(self, full, fast, *, check_auxiliary=True):
        self.assertEqual(len(full['pieces']), len(fast['pieces']))
        for a, b in zip(full['pieces'], fast['pieces']):
            self.assertEqual(_strip_audits(a), _strip_audits(b))
        if check_auxiliary:
            self.assertEqual(len(full['auxiliary']), len(fast['auxiliary']))
            for a, b in zip(full['auxiliary'], fast['auxiliary']):
                sa = _strip_audits(a)
                sb = _strip_audits(b)
                if sa['status'] == 'attached_closed_face':
                    sa['status'] = 'source_backed_closed_face_needs_review'
                self.assertEqual(sa, sb)
        self.assertEqual(full['unassigned']['count'], fast['unassigned']['count'])
        self.assertEqual(full['unassigned']['length_mm'], fast['unassigned']['length_mm'])

    def test_include_research_audits_false_matches_full_recognition_and_fold_export(self):
        # 含外片、片内嵌套小环（排除外轮廓后出现）、贯通分割弦、片内孤立悬垂树、标注文字、内部小矩形（omitrect）和短线头
        plt_parts = [
            'IN;',
            # 外框轮廓 A (0..4000 x 0..4000) + 片内矩形 (800..1600 x 800..1600) + 孤立悬垂线段 + 文字
            'PU0,0;PD4000,0,4000,4000,0,4000,0,0;',
            'PU800,800;PD1600,800,1600,1600,800,1600,800,800;',
            'PU2400,2400;PD2800,2400,2800,2800;',
            'PU400,400;LBPIECE-A\x03',
            # 另一片 B (6000..10000 x 0..4000) + 内部嵌套独立环 (7000..8500 x 1000..2500) + 贯通分割弦 (6000,2000)-(10000,2000)
            'PU6000,0;PD10000,0,10000,4000,6000,4000,6000,0;',
            'PU7000,1000;PD8500,1000,8500,2500,7000,2500,7000,1000;',
            'PU6000,2000;PD10000,2000;',
            # 宽松模式多笔闭合环 (12000..15000 x 0..3000)（端点留 0.25mm=10 绘图单位微缺口）
            'PU12000,0;PD15000,0,15000,3000;',
            'PU15000,3000;PD12000,3000,12000,10;',
            # 短线头
            'PU16000,0;PD16040,0;PU;',
        ]
        raw = ''.join(plt_parts).encode('ascii')

        for mode in ('strict', 'relaxed'):
            with self.subTest(mode=mode):
                full = recognize_with_variants(
                    raw, 'complex.plt', include_full_support_audit=False,
                    include_research_audits=True, recognition_mode=mode,
                )
                fast = recognize_with_variants(
                    raw, 'complex.plt', include_full_support_audit=False,
                    include_research_audits=False, recognition_mode=mode,
                )
                self._assert_equivalent_recognition(full, fast, check_auxiliary=True)
                if mode == 'relaxed':
                    # 宽松多笔候选应出现且不与严格闭合重复
                    self.assertTrue(any(p['id'].startswith('SJ-') or p['id'].startswith('SJW-') for p in fast['pieces']))
                # 验证 on_fold 与 unfolded 导出 PLT 逐字节一致
                candidate_fast = next(p for p in fast['pieces'] if p.get('source_closed_path_ids') == ['shape-1'])
                candidate_full = next(p for p in full['pieces'] if p.get('source_closed_path_ids') == ['shape-1'])
                min_y = min(min(f['a'][1], f['b'][1]) for f in candidate_fast['fragments'])
                fold_frags = [
                    {'path_id': f['path_id'], 'segment_index': f['segment_index'], 't0': 0, 't1': 1}
                    for f in candidate_fast['fragments']
                    if abs(f['a'][1] - min_y) <= 1e-6 and abs(f['b'][1] - min_y) <= 1e-6
                ]
                self.assertTrue(fold_frags)
                edge = {'a': [0.0, min_y], 'b': [100.0, min_y], 'fragments': fold_frags}
                for lay_mode, cut_mode, py in (('folded', 'on_fold', 0), ('single', 'unfolded', 120)):
                    doc_full = {
                        'schema_version': 2,
                        'source_sha256': full['source']['sha256'],
                        'fabric': {'lay_mode': lay_mode, 'width_mm': 500, 'length_mm': 500},
                        'candidates': [candidate_full],
                        'placements': [{'piece_id': candidate_full['id'], 'x': 20, 'y': py, 'cut_mode': cut_mode, 'fold_edge': edge}],
                    }
                    doc_fast = {
                        'schema_version': 2,
                        'source_sha256': fast['source']['sha256'],
                        'fabric': {'lay_mode': lay_mode, 'width_mm': 500, 'length_mm': 500},
                        'candidates': [candidate_fast],
                        'placements': [{'piece_id': candidate_fast['id'], 'x': 20, 'y': py, 'cut_mode': cut_mode, 'fold_edge': edge}],
                    }
                    self.assertEqual(export_project(raw, doc_full), export_project(raw, doc_fast))

    def test_four_corner_diamond_cross_markers_divert_equally_without_audit(self):
        # 构造 24800x20800 大跨度与四个角上的 800x800 (200 mm²) 轴向菱形十字定位标
        parts = ['IN;', 'PU5000,5000;PD10000,5000,10000,10000,5000,10000,5000,5000;']
        for cx, cy in ((500, 500), (24500, 500), (24500, 20500), (500, 20500)):
            r = 400
            parts.append(f'PU{cx},{cy-r};PD{cx+r},{cy},{cx},{cy+r},{cx-r},{cy},{cx},{cy-r};')
            parts.append(f'PU{cx-r},{cy};PD{cx+r},{cy};PU{cx},{cy-r};PD{cx},{cy+r};')
        raw = ''.join(parts).encode('ascii')

        full = recognize_source(raw, 'markers.plt', include_full_support_audit=False, include_research_audits=True)
        fast = recognize_source(raw, 'markers.plt', include_full_support_audit=False, include_research_audits=False)
        self._assert_equivalent_recognition(full, fast, check_auxiliary=True)
        diverted = [a for a in fast['auxiliary'] if a.get('status') == 'suspected_layout_registration_marker']
        self.assertEqual(len(diverted), 4)

    def test_empty_available_and_frame_only_plt_do_not_fail(self):
        forest, non_forest_ids, forest_dangles, components = partition_network({}, [], {}, count_forest_dangles=True)
        self.assertEqual((forest, non_forest_ids, forest_dangles, components), ([], set(), 0, []))

        # 单外框包含内片，外框排除后仅剩单环场景或空场景，验证 scenarios 不崩溃
        frame_only = b'IN;PU0,0;PD40000,0,40000,20000,0,20000,0,0;PU1000,1000;PD3000,1000,3000,3000,1000,3000,1000,1000;PU;'
        result = recognize_source(frame_only, 'frame.plt', include_full_support_audit=False, include_research_audits=False)
        self.assertGreaterEqual(len(result['pieces']) + len(result['auxiliary']), 1)

    def test_compact_browser_result_strips_audits_and_roundtrips_client_result(self):
        # 含重复零长点与整路径+部分切割片段，验证 P1 whole_path_ids + partial_segments 压缩与往返
        raw = b'IN;SP1;LT2,10,1;PU0,0;PD4000,0,4000,0,4000,2000,0,2000,0,0;PU800,400;PD1600,400,1600,1200,800,1200,800,400;PU200,200;LBFRONT\x03PU;'
        full = recognize_with_variants(
            raw, 'compact.plt', include_full_support_audit=False,
            include_research_audits=False, recognition_mode='relaxed',
        )
        compact = compact_browser_result(full)

        # 验证已去掉 *_audit、experimental_grouping、raw_points、path.segment_styles，改为 style_table + whole_path_ids + partial_segments
        self.assertFalse(any(k.endswith('_audit') or k == 'experimental_grouping' for k in compact))
        self.assertGreaterEqual(len(compact['style_table']), 1)
        units = float((compact['source'] or {}).get('units_per_mm') or 40.0)
        eps = float(((compact['source'] or {}).get('algorithm_parameters') or {}).get('numeric_epsilon_plotter_units', 1e-7)) / units
        path_order = {}
        nonzero_segs = {}
        for idx, p in enumerate(compact['paths']):
            self.assertNotIn('raw_points', p)
            self.assertNotIn('segment_styles', p)
            self.assertIn('segment_style_ids', p)
            path_order[p['id']] = idx
            pts = p['points']
            nonzero_segs[p['id']] = [si for si, (a, b) in enumerate(zip(pts, pts[1:])) if ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 > eps]

        has_whole = False
        expanded_by_id = {}
        for cand in compact['pieces'] + compact['auxiliary']:
            self.assertFalse(any(k.endswith('_audit') or k == 'experimental_grouping' for k in cand))
            self.assertNotIn('fragments', cand)
            self.assertIn('whole_path_ids', cand)
            self.assertIn('partial_segments', cand)
            if cand['whole_path_ids']:
                has_whole = True
            frags = [[pid, si, 0.0, 1.0] for pid in cand['whole_path_ids'] for si in nonzero_segs[pid]]
            frags.extend(cand['partial_segments'])
            frags.sort(key=lambda f: (path_order[f[0]], f[1], f[2], f[3]))
            expanded_by_id[cand['id']] = frags
        self.assertTrue(has_whole)

        compact_bytes = json.dumps(compact, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        full_bytes = json.dumps(
            recognize_with_variants(raw, 'compact.plt', include_research_audits=True, recognition_mode='relaxed'),
            ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8')
        self.assertLess(len(compact_bytes), len(full_bytes))

        # 展开后的四元组与原 full fragments 顺序、坐标、区间严格一致，并可通过 verify_client_result 往返
        by_id = {c['id']: c for c in full['pieces'] + full['auxiliary']}
        for cid, exp in expanded_by_id.items():
            orig = [[f['path_id'], f['segment_index'], f['t0'], f['t1']] for f in by_id[cid]['fragments']]
            self.assertEqual(exp, orig)

        payload = _client_payload(full, 'compact.plt')
        compact_payload = {
            **payload,
            'pieces': [
                {
                    **{k: v for k, v in c.items() if k in (
                        'id', 'status', 'outer', 'issues', 'parent_candidate_id',
                        'variant_label_cn', 'loop_id', 'source_closed_path_ids',
                        'area_mm2', 'excluded_rectangle', 'content_complete_for_region',
                    )},
                    'fragments': expanded_by_id[c['id']],
                }
                for c in compact['pieces']
            ],
            'auxiliary': [
                {
                    **{k: v for k, v in c.items() if k in (
                        'id', 'status', 'outer', 'issues', 'parent_candidate_id',
                        'variant_label_cn', 'loop_id', 'source_closed_path_ids',
                        'area_mm2', 'excluded_rectangle', 'content_complete_for_region',
                    )},
                    'fragments': expanded_by_id[c['id']],
                }
                for c in compact['auxiliary']
            ],
        }
        verified = verify_client_result(raw, compact_payload)
        for vc in verified['pieces'] + verified['auxiliary']:
            fc = by_id[vc['id']]
            self.assertEqual(
                [(f['path_id'], f['segment_index'], f['t0'], f['t1'], f['a'], f['b'], f['style']) for f in vc['fragments']],
                [(f['path_id'], f['segment_index'], f['t0'], f['t1'], f['a'], f['b'], f['style']) for f in fc['fragments']],
            )

    def test_fingerprints_track_p0_modules_and_keep_export_runtime_valid(self):
        comps = recognition_components()
        for mod in (
            'pattern_layout/fragment_index.py',
            'pattern_layout/network_components.py',
            'pattern_layout/browser_payload.py',
        ):
            self.assertIn(mod, comps)
        self.assertRegex(recognition_code_version(), r'^[0-9a-f]{64}$')
        self.assertRegex(export_runtime_version(), r'^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
