import unittest

from shapely.geometry import box

from app.services.pattern_layout.closed_loop import interval_parts
from app.services.pattern_layout_api import recognize_source

class PatternLayoutSpeedupTests(unittest.TestCase):
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
        from shapely.ops import unary_union
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

if __name__=='__main__':unittest.main()
