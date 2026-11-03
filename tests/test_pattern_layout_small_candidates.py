"""Small closed fragments below 1 cm2 are ignored; support_refs stays byte-identical."""
import unittest
from shapely.geometry import LineString, Point, Polygon
from app.services.pattern_layout.independent_shells import support_refs


def naive_support_refs(poly, paths, eps):
    refs = []
    for source in paths:
        dist = 0.
        for si, (a, b) in enumerate(zip(source['points'], source['points'][1:])):
            line = LineString([a, b]); length = line.length
            if length <= eps: continue
            hit = line.intersection(poly.boundary.buffer(eps))
            for g in getattr(hit, 'geoms', [hit]):
                if g.geom_type != 'LineString' or g.length <= eps: continue
                ts = [line.project(Point(v)) / length for v in g.coords]; t0 = max(0, min(ts)); t1 = min(1, max(ts))
                refs.append({'path_id': source['id'], 'segment_index': si, 't0': t0, 't1': t1, 'a': [a[k] + (b[k] - a[k]) * t0 for k in (0, 1)], 'b': [a[k] + (b[k] - a[k]) * t1 for k in (0, 1)], 'style': source['segment_styles'][si] if si < len(source['segment_styles']) else None, 'distance_from_path_start_mm': dist + t0 * length, 'assignment': 'source_boundary_observation'})
            dist += length
    return refs


SOURCE = (b'IN;PU0,0;PD4000,0,4000,4000,0,4000,0,0;'
          b'PU1000,1000;PD1200,1000,1200,1200,1000,1200,1000,1000;'
          b'PU4500,1000;PD4700,1000,4700,1200,4500,1200,4500,1000;'
          b'PU5000,0;PD5800,0,5800,800,5000,800,5000,0;PU;')


class SupportRefsEquivalenceTests(unittest.TestCase):
    def test_fast_bbox_rejection_matches_recomputed_buffer(self):
        poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        paths = [
            {'id': 'far', 'points': [[20, 20], [25, 25]], 'segment_styles': [None]},
            {'id': 'cross', 'points': [[-5, 5], [15, 5]], 'segment_styles': [{'lt_argument': ''}]},
            {'id': 'zero', 'points': [[5, 5], [5, 5], [5, 12]], 'segment_styles': [None, None]},
            {'id': 'touch', 'points': [[0, -5], [0, 5]], 'segment_styles': [None]},
        ]
        eps = 0.01
        self.assertEqual(support_refs(poly, paths, eps), naive_support_refs(poly, paths, eps))
        self.assertEqual(support_refs(poly, paths, eps), support_refs(poly, paths, eps))


class SmallCandidateFilterTests(unittest.TestCase):
    def test_sub_100mm2_closed_fragments_are_not_candidates(self):
        from app.services.pattern_layout_api import recognize_source
        result = recognize_source(SOURCE)
        candidates = result['pieces'] + result['auxiliary']
        self.assertFalse(any(candidate['id'] in ('S-shape-2', 'S-shape-3') for candidate in candidates))
        self.assertFalse(any(Polygon(candidate['outer']).area < 100. for candidate in candidates))
        self.assertFalse(any(set(candidate.get('source_closed_path_ids', [])) & {'shape-2', 'shape-3'} for candidate in candidates))
        twenty = next(candidate for candidate in result['pieces'] if candidate.get('source_closed_path_ids') == ['shape-4'])
        self.assertAlmostEqual(Polygon(twenty['outer']).area, 400.)
        big = next(candidate for candidate in result['pieces'] if candidate.get('source_closed_path_ids') == ['shape-1'])
        self.assertIn('shape-2', {fragment['path_id'] for fragment in big['fragments']})
        self.assertIn('shape-3', {fragment['path_id'] for fragment in result['unassigned']['fragments']})
        filtered = result['small_candidate_filter']
        self.assertEqual(filtered['ignored_closed_loop_count'], 2)
        self.assertEqual(filtered['ignored_source_contour_count'], 2)
        # 小闭合原线同时是闭环和 S- 源轮廓，ignored_count 只计不重复的碎块。
        self.assertEqual(filtered['ignored_count'], filtered['ignored_closed_loop_count'] + filtered['ignored_face_alternative_count'])
        self.assertEqual(filtered['ignored_count'], 2)
        self.assertGreater(filtered['ignored_count'], 0)

    def test_exactly_100mm2_closed_fragment_is_kept(self):
        from app.services.pattern_layout_api import recognize_source
        result = recognize_source(b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;')
        self.assertIn('S-shape-1', [candidate['id'] for candidate in result['pieces']])
        self.assertEqual(result['small_candidate_filter']['ignored_count'], 0)

    def test_variants_result_keeps_small_candidate_filter(self):
        from app.services.pattern_layout_variants import recognize_with_variants
        result = recognize_with_variants(SOURCE)
        self.assertIn('small_candidate_filter', result)
        self.assertEqual(result['small_candidate_filter']['ignored_count'], 2)

    def test_small_rectangle_does_not_become_omitrect_variant(self):
        from app.services.pattern_layout_variants import recognize_with_variants
        source = (b'IN;PU0,0;PD4000,0,4000,4000,0,4000,0,0;'
                  b'PU1000,1000;PD1200,1000,1200,1200,1000,1200,1000,1000;'
                  b'PU1600,1600;PD2400,1600,2400,2400,1600,2400,1600,1600;PU;')
        result = recognize_with_variants(source)
        omitted = [set(candidate['excluded_rectangle']['source_path_ids']) for candidate in result['pieces'] if candidate.get('excluded_rectangle')]
        self.assertFalse(any('shape-2' in ids for ids in omitted))
        self.assertTrue(any('shape-3' in ids for ids in omitted))
        self.assertGreaterEqual(result['rectangle_alternative_audit']['ignored_small_rectangle_count'], 1)
