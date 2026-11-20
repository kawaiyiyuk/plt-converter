import unittest
from unittest.mock import patch

from shapely.geometry import LineString, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.services.pattern_layout.closed_loop import interval_parts
from app.services.pattern_layout_api import recognize_source

class PatternLayoutSpeedupTests(unittest.TestCase):
    def test_known_segment_length_skips_clipping_geometry_outside_piece(self):
        region = box(0, 0, 10, 10)
        eps = 1e-9
        start, end = (-5, 5), (5, 5)
        length = LineString([start, end]).length
        expected = interval_parts(start, end, region, eps, region.bounds)

        self.assertEqual(interval_parts(start, end, region, eps, region.bounds, source_length=length), expected)
        with patch('app.services.pattern_layout.closed_loop.LineString') as line_string:
            self.assertEqual(interval_parts((-10, 5), (-5, 5), region, eps, region.bounds, source_length=5), [])
            self.assertEqual(interval_parts((5, 5), (5, 5), region, eps, region.bounds, source_length=0), [])
            line_string.assert_not_called()

    def test_borrowed_edge_checks_reuse_intersections_and_respect_exclusions(self):
        from app.services.pattern_layout.independent_shells import _borrowed_root_edge_check

        roots = [
            {'poly': box(50, 0, 60, 10), 'source_ids': ['remote']},
            {'poly': box(0, 0, 10, 10), 'source_ids': ['same']},
            {'poly': box(10, 0, 20, 10), 'source_ids': ['edge-a', 'edge-b']},
            {'poly': box(10, 10, 20, 20), 'source_ids': ['corner']},
        ]
        candidate = box(0, 0, 10, 10)
        check = _borrowed_root_edge_check(roots, 1e-9)
        original_intersection = BaseGeometry.intersection
        compared = []

        def count_intersection(left, right, *args, **kwargs):
            compared.append(left.bounds)
            return original_intersection(left, right, *args, **kwargs)

        with patch.object(BaseGeometry, 'intersection', count_intersection):
            self.assertTrue(check(candidate, frozenset()))
            self.assertFalse(check(candidate, {'edge-b'}))
            self.assertTrue(check(box(0, 0, 10, 10), {'remote'}))
            self.assertFalse(check(box(0, 0, 10, 10), {'edge-a'}))

        self.assertEqual(compared, [roots[i]['poly'].bounds for i in (1, 2, 3)])

    def test_dense_borrowed_edge_hits_use_bounded_storage(self):
        from app.services.pattern_layout.independent_shells import _borrowed_root_edge_check

        roots = [{'poly': box(i / 100, 0, 100 + i / 100, 100), 'source_ids': [i]}
                 for i in range(300)]
        candidate = box(0, 0, 20, 20)
        check = _borrowed_root_edge_check(roots, 1e-9)
        original_intersection = BaseGeometry.intersection

        with patch.object(BaseGeometry, 'intersection', autospec=True, side_effect=original_intersection) as intersect:
            self.assertTrue(check(candidate, frozenset()))
            self.assertTrue(check(candidate, frozenset()))
        self.assertEqual(intersect.call_count, 2, 'dense hits must not retain a dictionary of every possible pair')

    def test_disjoint_scenarios_reuse_original_path_buffer_union(self):
        # Eight independent outlines, each with an interior stroke.
        source = ['IN;']
        for i in range(8):
            x = 8000 * i
            source.append(f'PU{x},0;PD{x+4000},0,{x+4000},3000,{x},3000,{x},0;')
            source.append(f'PU{x+600},600;PD{x+1200},620;')
        raw = ''.join(source).encode()
        import shapely
        original_union = shapely.union_all
        large_buffer_unions = []

        def count_large_buffer_unions(geometries, *args, **kwargs):
            if (isinstance(geometries, list) and len(geometries) >= 8 and
                    all(g.geom_type == 'Polygon' and 0 < g.area < 0.01 for g in geometries)):
                large_buffer_unions.append(len(geometries))
            return original_union(geometries, *args, **kwargs)

        with patch.object(shapely, 'union_all', count_large_buffer_unions):
            result = recognize_source(raw, include_full_support_audit=False)

        audit = result['source_discovery_audit']
        self.assertEqual(len(audit['scenario_audit']), 10)
        self.assertTrue(audit['source_support_coverage_audit'])
        self.assertTrue(all(r['supported_missing_length_mm'] == 0
                            for r in audit['source_support_coverage_audit']))
        self.assertEqual(len([p for p in result['pieces'] if p['status'] == 'closed_shell']), 8)
        self.assertEqual(large_buffer_unions, [16])

    def test_cut_coverage_rechecks_only_when_exclusion_matters(self):
        from app.services.pattern_layout.scenario_coverage import ScenarioCoverage

        boundary = LineString([(0, 0), (10, 0)])
        covers = {
            'full': box(-1, -1, 11, 1),
            'duplicate': box(-1, -1, 11, 1),
            'partial': box(-1, -1, 5, 1),
        }
        coverage = ScenarioCoverage(covers, 0.01)

        # The excluded buffer touched the boundary, but another one still covers it.
        covered = coverage.for_paths(['duplicate', 'partial'], {'full'})
        self.assertEqual(covered(boundary), 0.0)

        # Without either full buffer, the original scenario union measures the gap.
        uncovered = coverage.for_paths(['partial'], {'full', 'duplicate'})
        self.assertEqual(uncovered(boundary), 5.0)

    def test_partial_gap_near_epsilon_uses_scenario_union(self):
        from app.services.pattern_layout.scenario_coverage import ScenarioCoverage

        eps = 0.125
        boundary = LineString([(0, 0), (10, 0)])
        for gap in (eps / 2, eps, 2 * eps):
            with self.subTest(gap=gap):
                partial = box(-1, -1, 10 - gap, 1)
                coverage = ScenarioCoverage({'partial': partial, 'remote': box(20, 20, 21, 21)}, eps)
                with patch('app.services.pattern_layout.scenario_coverage.unary_union', wraps=unary_union) as merge:
                    measure = coverage.for_paths(['partial'], {'remote'})
                    self.assertEqual(measure(boundary), gap)
                    self.assertEqual(measure(boundary), gap)
                    merge.assert_called_once_with([partial])

    def test_extreme_coordinates_use_original_coverage(self):
        from app.services.pattern_layout.scenario_coverage import ScenarioCoverage

        eps = 2.5e-9
        x = 1000000.0725528472
        lower_left = (x, x)
        lower_right = (x + 1, x)
        upper_right = (x + 1, x + 1)
        upper_left = (x, x + 1)
        edges = [
            (lower_left, lower_right), (lower_right, upper_right),
            (upper_right, upper_left), (upper_left, lower_left),
        ]
        crossing = 0.37538165934952716
        slope = -4.592725499853601e-09
        split = 8.445510547777967e-09 / 2
        detail = [
            ((x, x + crossing), (x + 1, x + crossing + slope)),
            ((x + crossing, x), (x + crossing - slope, x + 1)),
            (lower_left, upper_right),
            (lower_left, (x + 0.5 - split, x)),
            ((x + 0.5 + split, x), lower_right),
        ]
        covers = {i: LineString(points).buffer(eps) for i, points in enumerate(edges + detail + edges)}
        excluded = {1, 10, 12}
        retained = [key for key in covers if key not in excluded]
        boundary = LineString(edges[0])
        reference = unary_union([covers[key] for key in retained])
        expected = 0.0 if reference.covers(boundary) else boundary.difference(reference).length

        measure = ScenarioCoverage(covers, eps).for_paths(retained, excluded)
        self.assertEqual(measure(boundary), expected)

    def test_interval_parts_bounds_matches_full_region(self):
        region=box(0,0,10,10);eps=1e-9;bounds=region.bounds
        cases=[((-100,5),(-50,5),[]),((-5,5),(5,5),[(.5,1.)]),((2,2),(8,8),[(0.,1.)]),((5,5),(5,5),[])]
        for a,b,expected in cases:
            full=interval_parts(a,b,region,eps)
            self.assertEqual(full,expected)
            self.assertEqual(interval_parts(a,b,region,eps,bounds),full)

    def test_nested_loop_and_inner_lines_keep_expected_candidates_and_fragments(self):
        source=b'IN;PU0,0;PD8000,0,8000,8000,0,8000,0,0;PU400,400;PD2400,400,2400,2400,400,2400,400,400;PU1000,1000;PD1800,1800;PU3000,3000;PD3500,3000;PU;'
        result=recognize_source(source)
        pieces={p['id']:p for p in result['pieces']}
        self.assertEqual(list(pieces),['P001','S-shape-1','S-shape-2'])
        self.assertEqual({f['path_id'] for f in pieces['P001']['fragments']},{'shape-1','shape-2','shape-3','shape-4'})
        self.assertEqual({f['path_id'] for f in pieces['S-shape-1']['fragments']},{'shape-1','shape-2','shape-3','shape-4'})
        self.assertEqual({(f['path_id'],f['segment_index'],f['t0'],f['t1']) for f in pieces['S-shape-2']['fragments']},
                         {('shape-2',i,0.,1.) for i in range(4)}|{('shape-3',0,0.,1.)})
        self.assertEqual([a['status'] for a in result['auxiliary']],['nested_content_loop'])

    def test_coincident_real_contours_candidate_count_matches_existing_behavior(self):
        source=b'IN;PU0,0;PD4000,0,4000,4000,0,4000,0,0;PU0,0;PD4000,0,4000,4000,0,4000,0,0;PU800,800;PD2400,2400;PU;'
        result=recognize_source(source)
        independent=[p for p in result['pieces'] if p['id'].startswith('S-')]
        self.assertEqual(len(independent),2)
        for piece in independent:
            self.assertEqual({f['path_id'] for f in piece['fragments']},{'shape-1','shape-2','shape-3'})

    def test_empty_microscopic_boundary_buffer_keeps_other_candidates(self):
        rectangle = b'IN;PU0,0;PD4000,0,4000,2400,0,2400,0,0;PU;'
        tiny = (b'PU8000,1200;PD8000.00000001,1200,8000.00000001,1200.00000001,'
                b'8000,1200.00000001,8000,1200;PU;')
        reference = recognize_source(rectangle)
        original_buffer = LineString.buffer

        def buffer_with_empty_microscopic_ring(line, distance, *args, **kwargs):
            # GEOS versions differ on whether a tiny ring has an empty positive buffer.
            if line.is_ring and 0 < line.length <= distance:
                return Polygon()
            return original_buffer(line, distance, *args, **kwargs)

        with patch.object(LineString, 'buffer', buffer_with_empty_microscopic_ring):
            result = recognize_source(rectangle + tiny)

        self.assertEqual(result['paths'][0], reference['paths'][0])
        self.assertEqual(len(result['paths']), 2)
        self.assertEqual(len(result['pieces']), len(reference['pieces']))
        for actual, expected in zip(result['pieces'], reference['pieces']):
            for field in ('id', 'status', 'outer', 'fragments'):
                self.assertEqual(actual[field], expected[field])
        faces = result['source_discovery_audit']['atomic_faces']
        microscopic = [face for face in faces if 0 < face['area_mm2'] < 1e-18]
        self.assertEqual(len(microscopic), 1)
        self.assertEqual(microscopic[0]['complete_boundary_source_path_ids'], [])

if __name__=='__main__':unittest.main()
