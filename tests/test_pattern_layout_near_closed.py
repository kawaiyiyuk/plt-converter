"""Near-closed source contours within 1 mm are offered as review-only alternatives."""
import unittest
from app.services.pattern_layout_api import recognize_source

UNITS_PER_MM = 40


def square(size_mm, gap_mm=0.):
    side = int(round(size_mm * UNITS_PER_MM)); gap = int(round(gap_mm * UNITS_PER_MM))
    return f'IN;PU0,0;PD{side},0,{side},{side},0,{side},0,{gap};PU;'.encode()


def s_candidate(result, path_id='shape-1'):
    return next((candidate for candidate in result['pieces'] if candidate['id'] == 'S-' + path_id), None)


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


if __name__ == '__main__':
    unittest.main()
