"""Near-closed source contours within 1 mm are offered as review-only alternatives."""
import math
import unittest
from app.services.pattern_layout_api import export_project, recognize_source
from app.services.plt_parser import parse_plt

UNITS_PER_MM = 40
JOINED_ISSUE = 'source_strokes_joined_for_recognition'
INDEPENDENT_STATUS = 'independent_source_contour_needs_review'

# Two open strokes whose four endpoints leave two 3-unit (0.075 mm) seams.
SAMPLE_04 = b'IN;SP1;PA;PU0,0;PD4000,0,4000,2397;PU4000,2400;PD0,2400,0,3;PU;PG;'
# Two already near-closed strokes. Their outer endpoints are adjacent, but a
# closed single-stroke contour must not take part in cross-stroke joining.
SAMPLE_05 = (b'IN;SP1;PA;PU4000,0;PD0,0,0,2400,4000,2400,4000,3;'
             b'PU4003,0;PD8003,0,8003,2400,4003,2400,4003,3;PU;PG;')
# Same layout as sample 04, but both seams are 5 units (0.125 mm) instead.
WIDE_SEAMS = b'IN;SP1;PA;PU0,0;PD4000,0,4000,2395;PU4000,2400;PD0,2400,0,5;PU;PG;'
# An extra stroke competes with sample 04's upper-right endpoint.
SAMPLE_07 = (b'IN;SP1;PA;PU0,0;PD4000,0,4000,2397;PU4000,2400;PD0,2400,0,3;'
             b'PU4001,2397;PD4100,2200;PU;PG;')


def square(size_mm, gap_mm=0.):
    side = int(round(size_mm * UNITS_PER_MM)); gap = int(round(gap_mm * UNITS_PER_MM))
    return f'IN;PU0,0;PD{side},0,{side},{side},0,{side},0,{gap};PU;'.encode()


def twenty_stroke_rectangle():
    """20 笔组成 20 mm 方框，仅闭合缺口保留 3 个单位。"""
    points = [(x, 0) for x in range(0, 801, 160)]
    points += [(800, y) for y in range(160, 801, 160)]
    points += [(x, 800) for x in range(640, -1, -160)]
    points += [(0, y) for y in range(640, -1, -160)]
    points[-1] = (0, 3)
    commands = ['IN;', 'SP1;', 'PA;']
    for start, end in zip(points, points[1:]):
        commands.append(f'PU{start[0]},{start[1]};PD{end[0]},{end[1]};')
    commands.append('PU;PG;')
    return ''.join(commands).encode()


def s_candidate(result, path_id='shape-1'):
    return next((candidate for candidate in result['pieces'] if candidate['id'] == 'S-' + path_id), None)


def joined_candidate(result, path_ids):
    """Return the review candidate that joins exactly these source strokes."""
    wanted = set(path_ids)
    return next((candidate for candidate in result['pieces']
                 if candidate.get('status') == INDEPENDENT_STATUS
                 and set(candidate.get('source_closed_path_ids', [])) == wanted
                 and any(issue.get('code') == JOINED_ISSUE for issue in candidate.get('issues', []))), None)


def has_issue(candidate, code):
    return any(issue.get('code') == code for issue in candidate.get('issues', []))


def path_shapes(raw):
    return [shape for shape in parse_plt(raw, projection=True)['shapes'] if shape['type'] == 'path']


def path_points(shape):
    return [(point['x'], point['y']) for point in shape['points']]


def has_segment(paths, a, b, tolerance=1e-6):
    return any((math.dist(start, a) <= tolerance and math.dist(end, b) <= tolerance) or
               (math.dist(start, b) <= tolerance and math.dist(end, a) <= tolerance)
               for path in paths for start, end in zip(path, path[1:]))


class NearClosedSourceContourTests(unittest.TestCase):
    def test_half_mm_gap_becomes_review_candidate(self):
        result = recognize_source(square(20, .5))
        candidate = s_candidate(result)
        self.assertIsNotNone(candidate)
        issues = {issue['code']: issue for issue in candidate['issues']}
        self.assertIn('source_path_not_exactly_closed', issues)
        self.assertAlmostEqual(issues['source_path_not_exactly_closed']['gap_mm'], .5)
        points = next(path for path in result['paths'] if path['id'] == 'shape-1')['points']
        self.assertEqual(len(candidate['fragments']), len(points) - 1)
        self.assertTrue(all(fragment['segment_index'] < len(points) - 1 for fragment in candidate['fragments']))

    def test_two_mm_gap_is_not_a_candidate(self):
        result = recognize_source(square(20, 2.))
        self.assertIsNone(s_candidate(result))

    def test_exactly_closed_square_has_no_gap_issue(self):
        result = recognize_source(square(20, 0.))
        candidate = s_candidate(result)
        self.assertIsNotNone(candidate)
        self.assertNotIn('source_path_not_exactly_closed', {issue['code'] for issue in candidate['issues']})

    def test_small_near_closed_square_is_filtered(self):
        result = recognize_source(square(5, .5))
        self.assertIsNone(s_candidate(result))
        if 'near_closed_tolerance_mm' in result['source']['algorithm_parameters']:
            self.assertEqual(result['small_candidate_filter']['ignored_source_contour_count'], 1)


class MultiStrokeNearClosedContourTests(unittest.TestCase):
    def test_two_strokes_with_tiny_seams_form_one_review_candidate(self):
        result = recognize_source(SAMPLE_04)

        candidate = joined_candidate(result, ('shape-1', 'shape-2'))
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate['status'], INDEPENDENT_STATUS)
        self.assertTrue(has_issue(candidate, JOINED_ISSUE))
        self.assertEqual(set(candidate['source_closed_path_ids']), {'shape-1', 'shape-2'})
        self.assertFalse(candidate['semantic_piece_verified'])

        fragments = {(fragment['path_id'], fragment['segment_index'], fragment['t0'], fragment['t1'])
                     for fragment in candidate['fragments']}
        expected = {(path_id, segment_index, 0., 1.)
                    for path_id in ('shape-1', 'shape-2') for segment_index in range(2)}
        self.assertEqual(len(candidate['fragments']), 4)
        self.assertEqual(fragments, expected)

    def test_selected_joined_candidate_exports_only_the_original_strokes(self):
        result = recognize_source(SAMPLE_04)
        candidate = joined_candidate(result, ('shape-1', 'shape-2'))
        self.assertIsNotNone(candidate)

        document = {
            'schema_version': 1,
            'source_sha256': result['source']['sha256'],
            'candidates': [candidate],
            'placements': [{'piece_id': candidate['id'], 'x': 0, 'y': 0}],
            'acknowledged_source_warnings': list(result['source']['warnings']),
        }
        exported = export_project(SAMPLE_04, document)

        original = path_shapes(SAMPLE_04)
        produced = path_shapes(exported)
        self.assertEqual(len(original), 2)
        self.assertEqual(len(produced), 2)

        # Export may translate every point uniformly (and use floating point),
        # but the two stroke point sequences must stay unchanged.
        produced_points = [point for shape in produced for point in path_points(shape)]
        original_points = [point for shape in original for point in path_points(shape)]
        tx = min(x for x, _ in original_points) - min(x for x, _ in produced_points)
        ty = min(y for _, y in original_points) - min(y for _, y in produced_points)
        translated = [[(x + tx, y + ty) for x, y in path_points(shape)] for shape in produced]
        unmatched = [path_points(shape) for shape in original]
        for points in translated:
            match = next((expected for expected in unmatched
                          if len(expected) == len(points)
                          and all(math.dist(got, want) <= 1e-6 for got, want in zip(points, expected))), None)
            self.assertIsNotNone(match, f'exported path does not match an original stroke: {points!r}')
            unmatched.remove(match)
        self.assertEqual(unmatched, [])

        # The recognition-only synthetic shell must not become a closed source
        # path, and neither 3-unit seam may be drawn across the gap.
        self.assertTrue(all(path[0] != path[-1] for path in translated))
        self.assertFalse(has_segment(translated, (4000, 2397), (4000, 2400)))
        self.assertFalse(has_segment(translated, (0, 3), (0, 0)))

    def test_existing_single_stroke_contours_are_not_cross_joined(self):
        result = recognize_source(SAMPLE_05)

        independent = [candidate for candidate in result['pieces']
                       if candidate.get('status') == INDEPENDENT_STATUS]
        self.assertEqual(len(independent), 2)
        self.assertEqual({frozenset(candidate['source_closed_path_ids']) for candidate in independent},
                         {frozenset({'shape-1'}), frozenset({'shape-2'})})
        self.assertFalse(any(len(candidate.get('source_closed_path_ids', [])) > 1 for candidate in result['pieces']))
        self.assertIsNone(joined_candidate(result, ('shape-1', 'shape-2')))

    def test_seams_beyond_point_one_mm_keep_the_original_strokes(self):
        result = recognize_source(WIDE_SEAMS)

        self.assertFalse(any(len(candidate.get('source_closed_path_ids', [])) > 1
                             for candidate in result['pieces']))
        self.assertIsNone(joined_candidate(result, ('shape-1', 'shape-2')))
        self.assertEqual(result['paths'][0]['raw_points'],
                         [[point['x'], point['y']] for point in path_shapes(WIDE_SEAMS)[0]['points']])
        self.assertEqual(result['paths'][1]['raw_points'],
                         [[point['x'], point['y']] for point in path_shapes(WIDE_SEAMS)[1]['points']])

    def test_twenty_stroke_rectangle_gets_bounded_candidate_id(self):
        result = recognize_source(twenty_stroke_rectangle())

        joined = [candidate for candidate in result['pieces']
                  if candidate.get('status') == INDEPENDENT_STATUS
                  and len(candidate.get('source_closed_path_ids', [])) > 1]
        self.assertEqual(len(joined), 1)
        self.assertEqual(len(joined[0]['source_closed_path_ids']), 20)
        self.assertLessEqual(len(joined[0]['id']), 128)

    def test_competing_endpoint_does_not_guess_a_join(self):
        result = recognize_source(SAMPLE_07)

        self.assertIsNone(joined_candidate(result, ('shape-1', 'shape-2')))
        self.assertFalse(any(len(candidate.get('source_closed_path_ids', [])) > 1
                             for candidate in result['pieces']))


class RelaxedModeNearClosedContourTests(unittest.TestCase):
    def test_0_625mm_gap_recovers_in_relaxed_1mm_mode_only(self):
        # 两段 100mm×60mm 矩形，一端留 0.625mm (25 绘图单位) 断口
        raw = b'IN;SP1;PA;PU0,0;PD4000,0,4000,2375;PU4000,2400;PD0,2400,0,0;PU;PG;'
        strict_res = recognize_source(raw, recognition_mode='strict')
        self.assertFalse(any(c['id'].startswith('SJ') for c in strict_res['pieces']))

        relaxed_res = recognize_source(raw, recognition_mode='relaxed')
        sjw = next((c for c in relaxed_res['pieces'] if c['id'].startswith('SJW-')), None)
        self.assertIsNotNone(sjw)
        self.assertEqual(sjw['status'], INDEPENDENT_STATUS)
        self.assertEqual(set(sjw['source_closed_path_ids']), {'shape-1', 'shape-2'})
        issues = {i['code']: i for i in sjw['issues']}
        self.assertEqual(issues['relaxed_stroke_join_needs_review']['tolerance_mm'], 1.0)
        self.assertAlmostEqual(issues[JOINED_ISSUE]['max_seam_mm'], .625, places=3)

        # 导出仍然严格保持原线段，不补线
        doc = {
            'schema_version': 1,
            'source_sha256': relaxed_res['source']['sha256'],
            'candidates': [sjw],
            'placements': [{'piece_id': sjw['id'], 'x': 0, 'y': 0}],
            'acknowledged_source_warnings': list(relaxed_res['source']['warnings']),
        }
        produced = [path_points(shape) for shape in path_shapes(export_project(raw, doc))]
        self.assertEqual(len(produced), 2)
        self.assertFalse(has_segment(produced, (4000, 2375), (4000, 2400)))

    def test_tiered_pairing_preserves_0_025mm_seam_near_0_375mm_neighbor(self):
        # 上半段与下半段之间存在 0.025mm (1 unit) 与 0.125mm (5 units) 断口；
        # 左侧顶点 0.375mm (15 units) 处紧邻另一块条带线段。分层配对不得被相邻干扰线拆散。
        raw = (
            b'IN;SP1;PA;'
            b'PU0,0;PD4000,0,4000,2395;'
            b'PU4000,2400;PD0,2400,0,1;'
            b'PU-15,0;PD-15,4000;PU;PG;'
        )
        relaxed_res = recognize_source(raw, recognition_mode='relaxed')
        sjw = next(
            (c for c in relaxed_res['pieces']
             if c['id'].startswith('SJW-') and set(c.get('source_closed_path_ids', [])) == {'shape-1', 'shape-2'}),
            None,
        )
        self.assertIsNotNone(sjw)
        self.assertNotIn('shape-3', sjw['source_closed_path_ids'])

    def test_self_closing_chain_recovers_shared_vertex_outer_contour(self):
        # shape-5 + shape-6 在 (0,1200) 处与左侧小角及内部横线 shape-7 共点，
        # 严格模式因共点歧义无法形成 SJ-* 且主壳拆成两块辅助；宽松模式局部自闭合后完整还原并带出内部线 shape-7。
        raw = (
            b'IN;SP1;PA;'
            b'PU0,1200;PD200,400,4000,400,4000,1400;'
            b'PU4000,1400;PD4000,2400,200,2400,0,1250;'
            b'PU0,1250;PD0,1200;'
            b'PU400,1400;PD3600,1400;'
            b'PU0,1200;PD0,-400,-800,-400,-800,2800,0,2800;'
            b'PU0,2800;PD0,1200;'
            b'PU-800,1200;PD0,1200;'
            b'PU;PG;'
        )
        strict_res = recognize_source(raw, recognition_mode='strict')
        self.assertFalse(any(
            c['id'].startswith('SJ') and set(c.get('source_closed_path_ids', [])) == {'shape-5', 'shape-6'}
            for c in strict_res['pieces']
        ))

        relaxed_res = recognize_source(raw, recognition_mode='relaxed')
        sjw = next(
            (c for c in relaxed_res['pieces']
             if c['id'].startswith('SJW-') and set(c.get('source_closed_path_ids', [])) == {'shape-5', 'shape-6'}),
            None,
        )
        self.assertIsNotNone(sjw)
        frag_paths = {f['path_id'] for f in sjw['fragments']}
        self.assertIn('shape-7', frag_paths, '自闭合外框必须保留域内线条')

    def test_1_2mm_gap_stays_rejected_in_relaxed_1mm_mode(self):
        # 1.2mm (48 units) 超出 1.0mm 宽松上限，仍不拼合
        raw = b'IN;SP1;PA;PU0,0;PD4000,0,4000,2352;PU4000,2400;PD0,2400,0,0;PU;PG;'
        relaxed_res = recognize_source(raw, recognition_mode='relaxed')
        self.assertFalse(any(c['id'].startswith('SJ') for c in relaxed_res['pieces']))


if __name__ == '__main__':
    unittest.main()
