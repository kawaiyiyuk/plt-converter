import unittest

from shapely.geometry import box

from app.services.pattern_layout.closed_loop import interval_parts
from app.services.pattern_layout_api import recognize_source

class PatternLayoutSpeedupTests(unittest.TestCase):
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
