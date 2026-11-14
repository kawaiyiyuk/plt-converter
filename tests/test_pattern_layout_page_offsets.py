"""网页排版的横向偏移和旧工程导出兼容性。"""
import unittest
from hashlib import sha256

from app.services.pattern_layout_api import export_project, recognize_source, source_paths
from app.services.plt_parser import parse_plt

HEADER = b'IN;SP1;PA;'
RECT = b'PU0,0;PD4000,0,4000,2400,0,2400,0,0;PU;'


def origins(paths):
    return [tuple(path['raw_points'][0]) for path in paths]


def export_document(raw, paths, warnings):
    piece = {'id': 'p', 'fragments': [
        {'path_id': path['id'], 'segment_index': index, 't0': 0., 't1': 1.}
        for path in paths for index in range(len(path['raw_points']) - 1)
    ]}
    return {
        'schema_version': 1, 'source_sha256': sha256(raw).hexdigest(), 'candidates': [piece],
        'placements': [{'piece_id': 'p', 'x': 0, 'y': 0}], 'acknowledged_source_warnings': warnings,
    }


class LayoutPageOffsetsTests(unittest.TestCase):
    def test_pg_moves_only_later_absolute_points(self):
        parsed, paths = source_paths(HEADER + RECT + b'PG12000;' + RECT)
        self.assertEqual(origins(paths), [(0, 0), (12000, 0)])
        self.assertAlmostEqual(parsed['metrics']['width_mm'], 400)
        self.assertTrue(parsed['page_x_offsets_applied'])
        self.assertEqual(parsed['warnings'], [])

    def test_fl_sets_step_ff_accumulates_and_in_resets_offset(self):
        raw = HEADER + b'FL12000;' + RECT + b'FF;' + RECT + b'FF;' + RECT + HEADER + RECT + b'FF;' + RECT
        _, paths = source_paths(raw)
        self.assertEqual(origins(paths), [(0, 0), (12000, 0), (24000, 0), (0, 0), (12000, 0)])

    def test_signed_integer_arguments_and_unsupported_forms(self):
        raw = HEADER + RECT + b'PG-12000;' + RECT + b'PG;PG12.5;FLwrong;FF1;' + RECT
        parsed, paths = source_paths(raw)
        self.assertEqual(origins(paths), [(0, 0), (-12000, 0), (-12000, 0)])
        self.assertTrue(any('FF、FL、PG' in warning for warning in parsed['warnings']))

    def test_offsets_shift_absolute_arc_center_and_keep_relative_moves(self):
        raw = HEADER + b'PG1000;PU40,0;PD;AA30,0,90;PU;PR;PU10,20;PD0,15;PU;'
        _, paths = source_paths(raw)
        self.assertEqual(origins(paths), [(1040, 0), (1040, 30)])
        self.assertAlmostEqual(paths[0]['raw_points'][-1][0], 1030)
        self.assertAlmostEqual(paths[0]['raw_points'][-1][1], 10)
        self.assertEqual(paths[1]['raw_points'][-1], [1040, 45])

    def test_default_projection_and_bare_pg_keep_legacy_behavior(self):
        raw = HEADER + RECT + b'PG12000;' + RECT
        generic = parse_plt(raw, projection=True)
        self.assertEqual([shape['points'][0]['x'] for shape in generic['shapes']], [0, 0])
        parsed, paths = source_paths(HEADER + RECT + b'PG;')
        self.assertEqual(origins(paths), [(0, 0)])
        self.assertNotIn('page_x_offsets_applied', parsed)
        self.assertTrue(any('PG' in warning for warning in parsed['warnings']))

    def test_new_documents_and_legacy_documents_reexport_with_matching_coordinates(self):
        raw = HEADER + RECT + b'PG12000;' + RECT
        result = recognize_source(raw)
        self.assertIs(result['source'].get('page_x_offsets_applied'), True)
        legacy = export_document(raw, result['paths'], parse_plt(raw, projection=True)['warnings'])
        current = {**export_document(raw, result['paths'], result['source']['warnings']),
                   'page_x_offsets_applied': True}
        _, legacy_paths = source_paths(export_project(raw, legacy))
        _, current_paths = source_paths(export_project(raw, current))
        self.assertEqual([x for x, _ in origins(legacy_paths)], [0, 0])
        self.assertEqual([x for x, _ in origins(current_paths)], [0, 12000])
