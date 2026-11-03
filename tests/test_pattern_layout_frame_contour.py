"""Stepped drawing frames in source contour alternatives are separated for review."""
import unittest
from app.services.pattern_layout_api import recognize_source
from app.services.pattern_layout.closed_loop import PARAMS

UNITS_PER_MM = 40


def contour(points):
    start = points[0]
    return (f'PU{int(round(start[0] * UNITS_PER_MM))},{int(round(start[1] * UNITS_PER_MM))};PD'
            + ','.join(f'{int(round(x * UNITS_PER_MM))},{int(round(y * UNITS_PER_MM))}' for x, y in points[1:]) + ';')


def source(*contours):
    return ('IN;' + ''.join(contour(points) for points in contours) + 'PU;').encode()


def square(x, y, size=100):
    return [(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]


def frame_with_step():
    return [(20, 0), (20, 30), (0, 30), (0, 400), (300, 400), (300, 0), (20, 0)]


def s_candidate(result, path_id):
    return next((candidate for candidate in result['pieces'] if candidate['id'] == 'S-' + path_id), None)


def auxiliary_candidate(result, path_id):
    return next((candidate for candidate in result['auxiliary'] if candidate['id'] == 'S-' + path_id), None)


class FrameContourTests(unittest.TestCase):
    def test_stepped_frame_moves_to_auxiliary_with_body_witnesses(self):
        result = recognize_source(source(frame_with_step(), square(50, 50), square(170, 50)))
        frame = auxiliary_candidate(result, 'shape-1')
        self.assertIsNotNone(frame)
        self.assertIsNone(s_candidate(result, 'shape-1'))
        self.assertEqual(frame['status'], 'suspected_layout_frame')
        self.assertEqual(frame['issues'][0], {'code': 'suspected_layout_frame_contains_other_pieces', 'witness_count': 2})
        self.assertEqual(frame['source_closed_path_ids'], ['shape-1'])
        for path_id in ('shape-2', 'shape-3'):
            self.assertIsNotNone(s_candidate(result, path_id))
        audit = result['source_contour_frame_audit']
        self.assertEqual(audit['frame_candidate_ids'], ['S-shape-1'])
        self.assertEqual(audit['min_envelope_fill'], PARAMS['frame_min_envelope_fill'])
        self.assertEqual(audit['boundary_fraction'], PARAMS['frame_boundary_fraction'])
        self.assertEqual(audit['witness_area_mm2'], PARAMS['frame_body_witness_area_mm2'])

    def test_single_piece_with_inner_line_is_not_a_frame(self):
        result = recognize_source(source(square(0, 0, 200), [(50, 50), (150, 150)]))
        self.assertIsNotNone(s_candidate(result, 'shape-1'))
        self.assertIsNone(auxiliary_candidate(result, 'shape-1'))

    def test_single_body_witness_is_not_a_frame(self):
        result = recognize_source(source(frame_with_step(), square(50, 50)))
        self.assertIsNotNone(s_candidate(result, 'shape-1'))
        self.assertIsNone(auxiliary_candidate(result, 'shape-1'))

    def test_low_envelope_fill_is_not_a_frame(self):
        l_shape = [(0, 0), (300, 0), (300, 150), (150, 150), (150, 400), (0, 400), (0, 0)]
        result = recognize_source(source(l_shape, square(25, 25), square(25, 200)))
        self.assertIsNotNone(s_candidate(result, 'shape-1'))
        self.assertIsNone(auxiliary_candidate(result, 'shape-1'))


if __name__ == '__main__':
    unittest.main()
