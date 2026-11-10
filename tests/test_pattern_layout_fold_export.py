"""Focused v2 fold export geometry: exact fold-edge skipping and reflection."""
import copy
import hashlib
import math
import unittest

from app.services.pattern_layout_api import (_fold_reflect, PatternLayoutExportError, export_project,
                                             recognize_source, source_paths)
from app.services.plt_parser import parse_plt

UNITS_PER_MM = 1016 / 25.4


def half_piece_source(inner=False, label=False):
    source = b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
    if inner:
        source += b'PU40,40;PD80,80;PU2000,0;PD2400,0,2400,400,2000,400,2000,0;PU;'
    if label:
        source += b'PU40,40;LBABC\x03'
    return source


def duplicate_fold_support_source():
    return b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU0,400;PD400,400;PU;'


def source_candidate(source):
    result = recognize_source(source)
    candidate = next(piece for piece in result['pieces']
                     if piece['id'].startswith('S-') and piece.get('source_closed_path_ids') == ['shape-1'])
    return result, candidate


def fold_edge(fragments, a=(0, 0), b=(10, 0)):
    return {'a': list(a), 'b': list(b), 'fragments': fragments}


def segments(raw):
    parsed = parse_plt(raw, projection=True)
    return [((shape['points'][index]['x'] / UNITS_PER_MM, -shape['points'][index]['y'] / UNITS_PER_MM),
             (shape['points'][index + 1]['x'] / UNITS_PER_MM, -shape['points'][index + 1]['y'] / UNITS_PER_MM))
            for shape in parsed['shapes'] if shape.get('type') == 'path'
            for index in range(len(shape['points']) - 1)]


def labels(raw):
    parsed = parse_plt(raw, projection=True)
    return [{'text': shape['text'],
             'point': (shape['point']['x'] / UNITS_PER_MM, -shape['point']['y'] / UNITS_PER_MM)}
            for shape in parsed['shapes'] if shape.get('type') == 'text']


class FoldExportTests(unittest.TestCase):
    def document(self, source, lay_mode, placements):
        return {'schema_version': 2, 'source_sha256': hashlib.sha256(source).hexdigest(),
                'fabric': {'lay_mode': lay_mode, 'width_mm': 20, 'length_mm': 100},
                'candidates': [self.candidate], 'placements': placements}

    def setUp(self):
        self.source = half_piece_source()
        self.result, self.candidate = source_candidate(self.source)
        self.edge = fold_edge([{'path_id': 'shape-1', 'segment_index': 2, 't0': 0, 't1': 1}])

    def assert_invalid(self, source, document):
        with self.assertRaises(PatternLayoutExportError) as raised:
            export_project(source, document)
        self.assertEqual(raised.exception.code, 'invalid_fold_edge')

    def test_v1_normal_export_keeps_the_original_closed_contour(self):
        document = {'schema_version': 1, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                    'candidates': [self.candidate],
                    'placements': [{'piece_id': self.candidate['id'], 'x': 30, 'y': 0}]}
        self.assertEqual(len(segments(export_project(self.source, document))), 4)

    def test_v1_fabric_width_and_length_do_not_change_legacy_output(self):
        base = {'schema_version': 1, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                'candidates': [self.candidate],
                'placements': [{'piece_id': self.candidate['id'], 'x': 30, 'y': 0}]}
        with_fabric = copy.deepcopy(base)
        with_fabric['fabric'] = {'width_mm': 1400, 'length_mm': 10000}
        self.assertEqual(export_project(self.source, with_fabric), export_project(self.source, base))
        with_fabric['fabric']['lay_mode'] = 'single'
        self.assert_invalid(self.source, with_fabric)
        cut_mode = copy.deepcopy(base)
        cut_mode['placements'][0]['cut_mode'] = 'normal'
        self.assert_invalid(self.source, cut_mode)
        fold_edge_doc = copy.deepcopy(base)
        fold_edge_doc['placements'][0]['fold_edge'] = self.edge
        self.assert_invalid(self.source, fold_edge_doc)

    def test_folded_on_fold_skips_only_the_fold_edge_and_normal_piece_stays_closed(self):
        document = self.document(self.source, 'folded', [
            {'piece_id': self.candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': self.edge},
            {'piece_id': self.candidate['id'], 'x': 30, 'y': 40, 'cut_mode': 'normal', 'fold_edge': self.edge},
        ])
        actual = segments(export_project(self.source, document))
        self.assertEqual(len(actual), 7)
        on_fold = [segment for segment in actual if min(segment[0][1], segment[1][1]) < 20]
        normal = [segment for segment in actual if min(segment[0][1], segment[1][1]) >= 20]
        self.assertEqual(len(on_fold), 3)
        self.assertEqual(len(normal), 4)
        self.assertFalse(any(abs(a[1]) < 1e-9 and abs(b[1]) < 1e-9 for a, b in on_fold))
        self.assertTrue(all(a[1] >= -1e-9 and b[1] >= -1e-9 for a, b in on_fold))

    def test_v3_bottom_fold_skips_the_bottom_edge_and_keeps_a_normal_piece_closed(self):
        edge = fold_edge([{'path_id': 'shape-1', 'segment_index': 0, 't0': 0, 't1': 1}], a=(0, 10), b=(10, 10))
        document = {'schema_version': 3, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                    'fabric': {'lay_mode': 'folded', 'fold_side': 'bottom', 'width_mm': 20, 'length_mm': 100},
                    'candidates': [self.candidate], 'placements': [
                        {'piece_id': self.candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': edge},
                        {'piece_id': self.candidate['id'], 'x': 30, 'y': 40, 'cut_mode': 'normal', 'fold_edge': edge},
                    ]}
        actual = segments(export_project(self.source, document))
        self.assertEqual(len(actual), 7)
        on_fold = [segment for segment in actual if max(segment[0][1], segment[1][1]) < 20]
        normal = [segment for segment in actual if min(segment[0][1], segment[1][1]) >= 20]
        self.assertEqual(len(on_fold), 3)
        self.assertEqual(len(normal), 4)
        self.assertFalse(any(abs(a[1] - 10) < 1e-9 and abs(b[1] - 10) < 1e-9 for a, b in on_fold))
        self.assertTrue(all(a[1] <= 10 + 1e-9 and b[1] <= 10 + 1e-9 for a, b in on_fold))

    def test_v3_top_fold_keeps_the_existing_top_rule(self):
        document = {'schema_version': 3, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                    'fabric': {'lay_mode': 'folded', 'fold_side': 'top', 'width_mm': 20, 'length_mm': 100},
                    'candidates': [self.candidate], 'placements': [
                        {'piece_id': self.candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': self.edge},
                    ]}
        actual = segments(export_project(self.source, document))
        self.assertEqual(len(actual), 3)
        self.assertFalse(any(abs(a[1]) < 1e-9 and abs(b[1]) < 1e-9 for a, b in actual))
        self.assertTrue(all(a[1] >= -1e-9 and b[1] >= -1e-9 for a, b in actual))

    def test_bottom_fold_rejects_a_top_edge_or_a_piece_beyond_the_bottom(self):
        document = {'schema_version': 3, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                    'fabric': {'lay_mode': 'folded', 'fold_side': 'bottom', 'width_mm': 20, 'length_mm': 100},
                    'candidates': [self.candidate]}
        top_edge = copy.deepcopy(document)
        top_edge['placements'] = [{'piece_id': self.candidate['id'], 'x': 30, 'y': 0,
                                   'cut_mode': 'on_fold', 'fold_edge': self.edge}]
        self.assert_invalid(self.source, top_edge)
        outside = copy.deepcopy(document)
        outside['placements'] = [{'piece_id': self.candidate['id'], 'x': 30, 'y': 10,
                                  'cut_mode': 'on_fold',
                                  'fold_edge': fold_edge([{'path_id': 'shape-1', 'segment_index': 2,
                                                           't0': 0, 't1': 1}])}]
        self.assert_invalid(self.source, outside)

    def test_bottom_fold_requires_schema_three_and_a_valid_explicit_fold_side(self):
        edge = fold_edge([{'path_id': 'shape-1', 'segment_index': 0, 't0': 0, 't1': 1}], a=(0, 10), b=(10, 10))
        base = {'schema_version': 3, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                'fabric': {'lay_mode': 'folded', 'fold_side': 'bottom', 'width_mm': 20, 'length_mm': 100},
                'candidates': [self.candidate],
                'placements': [{'piece_id': self.candidate['id'], 'x': 30, 'y': 0,
                                'cut_mode': 'on_fold', 'fold_edge': edge}]}
        legacy = copy.deepcopy(base)
        legacy['schema_version'] = 2
        self.assert_invalid(self.source, legacy)
        missing = copy.deepcopy(base)
        missing['fabric'].pop('fold_side')
        self.assert_invalid(self.source, missing)
        invalid = copy.deepcopy(base)
        invalid['fabric']['fold_side'] = 'middle'
        self.assert_invalid(self.source, invalid)

    def test_unfolded_reflects_non_fold_geometry_once_and_keeps_native_label_once(self):
        source = half_piece_source(label=True)
        _result, candidate = source_candidate(source)
        self.candidate = candidate
        document = self.document(source, 'single', [
            {'piece_id': candidate['id'], 'x': 30, 'y': 20, 'cut_mode': 'unfolded', 'fold_edge': self.edge},
        ])
        raw = export_project(source, document)
        actual = segments(raw)
        self.assertEqual(len(actual), 6)
        xs = [value for segment in actual for point in segment for value in (point[0],)]
        ys = [value for segment in actual for point in segment for value in (point[1],)]
        self.assertAlmostEqual(min(xs), 30)
        self.assertAlmostEqual(max(xs), 40)
        self.assertAlmostEqual(min(ys), 10)
        self.assertAlmostEqual(max(ys), 30)
        self.assertFalse(any(abs(a[1] - 20) < 1e-9 and abs(b[1] - 20) < 1e-9 for a, b in actual))
        exported_labels = labels(raw)
        self.assertEqual(len(exported_labels), 1)
        self.assertEqual(exported_labels[0]['text'], 'ABC')
        self.assertAlmostEqual(exported_labels[0]['point'][0], 31)
        self.assertAlmostEqual(exported_labels[0]['point'][1], 29)

    def test_unfolded_reflects_inner_lines_and_never_borrows_a_neighbor_piece(self):
        source = half_piece_source(inner=True)
        _result, candidate = source_candidate(source)
        if not any(fragment['path_id'] == 'shape-2' for fragment in candidate['fragments']):
            candidate['fragments'].append({'path_id': 'shape-2', 'segment_index': 0, 't0': 0, 't1': 1})
        self.candidate = candidate
        document = self.document(source, 'single', [
            {'piece_id': candidate['id'], 'x': 30, 'y': 20, 'cut_mode': 'unfolded', 'fold_edge': self.edge},
        ])
        actual = segments(export_project(source, document))
        self.assertEqual(len(actual), 8)
        self.assertTrue(any(abs(a[0] - 31) < 1e-9 and abs(b[0] - 32) < 1e-9 and abs(a[1] - 29) < 1e-9 for a, b in actual))
        self.assertTrue(any(abs(a[0] - 31) < 1e-9 and abs(b[0] - 32) < 1e-9 and abs(a[1] - 11) < 1e-9 for a, b in actual))
        self.assertFalse(any(max(a[0], b[0]) > 45 for a, b in actual))

    def test_fold_edge_must_cover_the_complete_continuous_collinear_outer_edge(self):
        source = b'IN;PU-200,0;PD600,0,600,400,-200,400,-200,0;PU40,40;PD80,80;PU2000,0;PD2400,0,2400,400,2000,400,2000,0;PU;'
        _result, candidate = source_candidate(source)
        if not any(fragment['path_id'] == 'shape-2' for fragment in candidate['fragments']):
            candidate['fragments'].append({'path_id': 'shape-2', 'segment_index': 0, 't0': 0, 't1': 1})
        # Compact exports may not carry private outer geometry.
        candidate.pop('outer', None)
        self.candidate = candidate
        partial = fold_edge([{'path_id': 'shape-1', 'segment_index': 2, 't0': .5, 't1': 1}], a=(0, 0), b=(10, 0))
        self.assert_invalid(source, self.document(source, 'folded', [
            {'piece_id': candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': partial},
        ]))
        complete = fold_edge([{'path_id': 'shape-1', 'segment_index': 2, 't0': 0, 't1': 1}], a=(0, 0), b=(20, 0))
        actual = segments(export_project(source, self.document(source, 'folded', [
            {'piece_id': candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': complete},
        ])))
        self.assertEqual(len(actual), 4)
        self.assertFalse(any(abs(a[1]) < 1e-9 and abs(b[1]) < 1e-9 for a, b in actual))
        self.assertTrue(any(abs(a[0] - 36) < 1e-9 and abs(b[0] - 37) < 1e-9 for a, b in actual))
        self.assertFalse(any(max(a[0], b[0]) > 55 for a, b in actual))

    def test_same_axis_overlapping_and_duplicate_source_support_are_all_skipped(self):
        source = duplicate_fold_support_source()
        _result, candidate = source_candidate(source)
        if not any(fragment['path_id'] == 'shape-2' for fragment in candidate['fragments']):
            candidate['fragments'].append({'path_id': 'shape-2', 'segment_index': 0, 't0': 0, 't1': 1})
        candidate.pop('outer', None)
        self.candidate = candidate
        supported = fold_edge([
            {'path_id': 'shape-1', 'segment_index': 2, 't0': 0, 't1': 1},
            {'path_id': 'shape-2', 'segment_index': 0, 't0': 0, 't1': 1},
            {'path_id': 'shape-1', 'segment_index': 2, 't0': 0, 't1': .6},
            {'path_id': 'shape-1', 'segment_index': 2, 't0': .4, 't1': 1},
        ])
        document = self.document(source, 'folded', [
            {'piece_id': candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': supported},
        ])
        self.assertEqual(len(segments(export_project(source, document))), 3)
        residual = copy.deepcopy(supported)
        residual['fragments'] = [fragment for fragment in residual['fragments'] if fragment['path_id'] != 'shape-2']
        self.assert_invalid(source, self.document(source, 'folded', [
            {'piece_id': candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': residual},
        ]))

    def test_reflection_formula_fixes_nonzero_and_oblique_axis_points(self):
        axis = ((2., 3.), (3 / 5, 4 / 5))
        for offset in (-4., 0., 2.5):
            point = (2. + offset * 3 / 5, 3. + offset * 4 / 5)
            reflected = _fold_reflect(point, axis)
            self.assertAlmostEqual(reflected[0], point[0], places=12)
            self.assertAlmostEqual(reflected[1], point[1], places=12)
        point = (1.8, 4.4)  # axis origin + one along + one perpendicular
        reflected = _fold_reflect(point, axis)
        self.assertAlmostEqual(reflected[0], 3.4, places=12)
        self.assertAlmostEqual(reflected[1], 3.2, places=12)
        again = _fold_reflect(reflected, axis)
        self.assertAlmostEqual(again[0], point[0], places=12)
        self.assertAlmostEqual(again[1], point[1], places=12)

    def test_unfolded_reflects_around_nonzero_y_axis_without_shifting(self):
        source = b'IN;PU0,3000;PD40,3000;PU0,200;PD400,200,400,600,0,600,0,200;PU;'
        _parsed, paths = source_paths(source)
        half = next(path for path in paths if len(path['points']) == 5)
        self.assertEqual(half['points'][2][1], 60)
        candidate = {'id': 'compact', 'fragments': [
            {'path_id': half['id'], 'segment_index': index, 't0': 0, 't1': 1} for index in range(4)
        ]}
        self.candidate = candidate
        edge = fold_edge([{'path_id': half['id'], 'segment_index': 2, 't0': 0, 't1': 1}], a=(0, 60), b=(10, 60))
        document = self.document(source, 'single', [
            {'piece_id': candidate['id'], 'x': 30, 'y': 20, 'cut_mode': 'unfolded', 'fold_edge': edge},
        ])
        actual = segments(export_project(source, document))
        self.assertEqual(len(actual), 6)
        ys = [point[1] for segment in actual for point in segment]
        xs = [point[0] for segment in actual for point in segment]
        self.assertAlmostEqual(min(ys), 70)
        self.assertAlmostEqual(max(ys), 90)
        self.assertAlmostEqual(min(xs), 30)
        self.assertAlmostEqual(max(xs), 40)
        self.assertFalse(any(abs(a[1] - 80) < 1e-9 and abs(b[1] - 80) < 1e-9 for a, b in actual))
        points = {(round(point[0], 9), round(point[1], 9)) for segment in actual for point in segment}
        self.assertIn((30., 80.), points)
        self.assertIn((40., 80.), points)
        upper = [segment for segment in actual if min(segment[0][1], segment[1][1]) >= 80 - 1e-9]
        lower = [segment for segment in actual if max(segment[0][1], segment[1][1]) <= 80 + 1e-9]
        self.assertEqual((len(upper), len(lower)), (3, 3))
        self.assertEqual(sorted(round(math.dist(*segment), 9) for segment in upper),
                         sorted(round(math.dist(*segment), 9) for segment in lower))

    def test_v2_requires_valid_lay_mode_and_positive_fabric_dimensions(self):
        for fabric in (None, {}, {'lay_mode': 'single', 'width_mm': 20},
                       {'lay_mode': 'single', 'width_mm': 20, 'length_mm': 0},
                       {'lay_mode': 'invalid', 'width_mm': 20, 'length_mm': 100},
                       {'lay_mode': 'single', 'width_mm': True, 'length_mm': 100},
                       {'lay_mode': 'single', 'width_mm': 20, 'length_mm': -1}):
            document = {'schema_version': 2, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                        'candidates': [self.candidate],
                        'placements': [{'piece_id': self.candidate['id'], 'x': 30, 'y': 0}]}
            if fabric is not None:
                document['fabric'] = fabric
            with self.subTest(fabric=fabric):
                self.assert_invalid(self.source, document)
        valid = self.document(self.source, 'single', [
            {'piece_id': self.candidate['id'], 'x': 30, 'y': 0},
        ])
        self.assertEqual(len(segments(export_project(self.source, valid))), 4)
        bool_schema = {'schema_version': True, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                       'candidates': [self.candidate],
                       'placements': [{'piece_id': self.candidate['id'], 'x': 30, 'y': 0}]}
        with self.assertRaises(PatternLayoutExportError) as raised:
            export_project(self.source, bool_schema)
        self.assertEqual(raised.exception.code, 'invalid_source_identity')

    def test_forged_broken_wrong_axis_wrong_side_and_mode_mismatch_are_rejected(self):
        broken = copy.deepcopy(self.edge)
        broken['fragments'][0]['t1'] = .5
        wrong_axis = fold_edge(self.edge['fragments'], a=(0, 5), b=(10, 5))
        wrong_side = {'piece_id': self.candidate['id'], 'x': 30, 'y': 0, 'rotation_deg': 180,
                      'cut_mode': 'on_fold', 'fold_edge': self.edge}
        for placement in (
                {'piece_id': self.candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': broken},
                {'piece_id': self.candidate['id'], 'x': 30, 'y': 0, 'cut_mode': 'on_fold', 'fold_edge': wrong_axis},
                wrong_side):
            with self.subTest(placement=placement):
                self.assert_invalid(self.source, self.document(self.source, 'folded', [placement]))
        self.assert_invalid(self.source, self.document(self.source, 'folded', [
            {'piece_id': self.candidate['id'], 'x': 30, 'y': 20, 'cut_mode': 'unfolded', 'fold_edge': self.edge},
        ]))
        self.assert_invalid(self.source, {'schema_version': 2, 'source_sha256': hashlib.sha256(self.source).hexdigest(),
                                          'candidates': [self.candidate],
                                          'placements': [{'piece_id': self.candidate['id'], 'x': 30, 'y': 0,
                                                          'cut_mode': 'on_fold', 'fold_edge': self.edge}]})


if __name__ == '__main__':
    unittest.main()
