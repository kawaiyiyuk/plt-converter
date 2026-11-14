"""短端线只用于区分识别轮廓，导出保留同笔原线。"""
import math
import unittest

from app.services.pattern_layout_api import export_project, recognize_source, source_paths

TIP_ISSUE = 'source_short_tip_separated_for_recognition'


def stroke(points):
    coordinates = [f'{x},{y}' for x, y in points]
    return ('PU' + coordinates[0] + ';PD' + ','.join(coordinates[1:]) + ';PU;').encode()


def rectangle_with_tip(x, side, length, gap=3):
    outline = [(x, 1000), (x + 4000, 1000), (x + 4000, 3400), (x, 3400), (x, 1000 + gap)]
    if side == 'start':
        return [(x, 1000 - length)] + outline
    return outline + [(x - length, 1000 + gap)]


def separated_candidates(result):
    return [piece for piece in result['pieces'] if any(issue['code'] == TIP_ISSUE for issue in piece['issues'])]


def normalized(stroke_points):
    x0, y0 = (min(point[axis] for point in stroke_points) for axis in (0, 1))
    return [(round(x - x0, 8), round(y - y0, 8)) for x, y in stroke_points]


class ShortStrokeTipTests(unittest.TestCase):
    def test_short_start_and_end_keep_all_original_segments(self):
        for side in ('start', 'end'):
            with self.subTest(side=side):
                short = rectangle_with_tip(1000, side, 200)
                long = rectangle_with_tip(7000, side, 800)
                raw = b'IN;SP1;PA;' + stroke(short) + stroke(long)
                result = recognize_source(raw)
                pieces = separated_candidates(result)
                self.assertEqual(len(pieces), 1)
                self.assertEqual(len(result['pieces']), 1)
                piece = pieces[0]
                self.assertEqual(piece['status'], 'independent_source_contour_needs_review')
                self.assertEqual(piece['source_closed_path_ids'], ['shape-1'])
                self.assertEqual(piece['outer'][0], piece['outer'][-1])
                self.assertEqual([path['raw_points'] for path in result['paths']],
                                 [[list(point) for point in points] for points in (short, long)])
                self.assertEqual([(f['path_id'], f['segment_index'], f['t0'], f['t1'])
                                  for f in piece['fragments']],
                                 [('shape-1', index, 0., 1.) for index in range(len(short) - 1)])
                export = export_project(raw, {
                    'schema_version': 1, 'source_sha256': result['source']['sha256'],
                    'candidates': [piece], 'placements': [{'piece_id': piece['id'], 'x': 20, 'y': 30}],
                })
                _, exported_paths = source_paths(export)
                self.assertEqual(len(exported_paths), 1)
                self.assertEqual(normalized(exported_paths[0]['raw_points']), normalized(short))

    def test_removing_tip_does_not_hide_a_larger_gap(self):
        raw = b'IN;SP1;PA;' + stroke(rectangle_with_tip(1000, 'start', 200, gap=5))
        self.assertFalse(separated_candidates(recognize_source(raw)))

    def test_straight_or_interior_end_segments_are_not_separated(self):
        straight = [(800, 1000)] + rectangle_with_tip(1000, 'start', 200)[1:]
        # 第一个起笔同向，第二个使用与左边垂直的片内短收笔。
        inside = rectangle_with_tip(1000, 'start', 200)[1:] + [(1200, 1003)]
        for points in (straight, inside):
            with self.subTest(points=points):
                result = recognize_source(b'IN;SP1;PA;' + stroke(points))
                self.assertFalse(separated_candidates(result))

    def test_both_tips_can_be_separated_when_only_the_core_closes(self):
        points = rectangle_with_tip(1000, 'start', 200) + [(800, 1003)]
        result = recognize_source(b'IN;SP1;PA;' + stroke(points))
        pieces = separated_candidates(result)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(len(pieces[0]['fragments']), len(points) - 1)
        original = result['paths'][0]['points']
        self.assertGreater(math.dist(original[0], original[-1]), 1)
