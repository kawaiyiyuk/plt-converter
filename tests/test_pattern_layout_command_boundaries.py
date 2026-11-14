"""网页排版指令边界和标签结束符的保真检查。"""
from hashlib import sha256
import unittest

from app.services.pattern_layout_api import export_project, recognize_source, source_paths
from app.services.plt_parser import parse_plt

HEADER = b'IN;SP1;PA;'
RECTANGLE = b'PU0,0;PD4000,0,4000,2400,0,2400,0,0;PU;'
STANDARD = HEADER + RECTANGLE
EXPECTED = [[0, 0], [4000, 0], [4000, 2400], [0, 2400], [0, 0]]
DT_SAMPLE = HEADER + b'DT@;PU600,600;LBTEST@' + RECTANGLE


def labels(parsed):
    return [(shape['text'], shape['point']) for shape in parsed['shapes'] if shape['type'] == 'text']


def translated(points):
    min_x, min_y = (min(point[axis] for point in points) for axis in (0, 1))
    return [[round(x - min_x, 8), round(y - min_y, 8)] for x, y in points]


class LayoutCommandBoundariesTests(unittest.TestCase):
    def test_line_breaks_and_compact_commands_preserve_the_same_rectangle(self):
        for delimiter in (b'\r\n', b'\n', b'\r', b''):
            with self.subTest(delimiter=delimiter):
                raw = STANDARD.replace(b';', delimiter)
                parsed, paths = source_paths(raw)
                self.assertEqual([path['raw_points'] for path in paths], [EXPECTED])
                self.assertTrue(parsed['layout_command_boundaries_applied'])
                self.assertEqual(parsed['source_sha256'], sha256(raw).hexdigest())
                self.assertEqual(parsed['warnings'], [])

    def test_wrapping_coordinate_values_does_not_end_the_command(self):
        raw = HEADER + b'PU0,0;PD4000,0,\r\n 4000,2400,\n0,2400,0,0;PU;'
        parsed, paths = source_paths(raw)
        self.assertEqual([path['raw_points'] for path in paths], [EXPECTED])
        self.assertNotIn('layout_command_boundaries_applied', parsed)

    def test_compact_signed_numbers_and_exponents(self):
        raw = b'INSP1PAPU-1.5E2,+2e2PD40,2e2PU'
        _, paths = source_paths(raw)
        self.assertEqual([p['raw_points'] for p in paths], [[[-150, 200], [40, 200]]])

    def test_custom_label_terminator_does_not_consume_next_pen_move(self):
        for raw in (DT_SAMPLE, DT_SAMPLE.replace(b'DT@;', b'DT@')):
            with self.subTest(raw=raw):
                parsed, paths = source_paths(raw)
                self.assertEqual([p['raw_points'] for p in paths], [EXPECTED])
                self.assertEqual(labels(parsed), [('TEST', {'x': 600, 'y': 600})])
                self.assertTrue(parsed['layout_command_boundaries_applied'])
                self.assertEqual(parsed['warnings'], ['LB 文字按固定 8pt 兼容显示'])

    def test_label_words_and_quoted_arguments_do_not_become_commands(self):
        raw = HEADER + b'CO"PU9000,0;PD9999,9999\r\nPA";PU600,600;LBPU PD TEST\x03;' + RECTANGLE
        parsed, paths = source_paths(raw)
        self.assertEqual([p['raw_points'] for p in paths], [EXPECTED])
        self.assertEqual(labels(parsed), [('PU PD TEST', {'x': 600, 'y': 600})])
        self.assertTrue(parsed['layout_command_boundaries_applied'])

    def test_explicit_etx_can_restore_default_terminator(self):
        raw = HEADER + b'DT@;PU600,600;LBONE@DT\x03;PU700,700;LBTWO\x03' + RECTANGLE
        parsed, paths = source_paths(raw)
        self.assertEqual([p['raw_points'] for p in paths], [EXPECTED])
        self.assertEqual(labels(parsed), [('ONE', {'x': 600, 'y': 600}), ('TWO', {'x': 700, 'y': 700})])

    def test_incomplete_compact_coordinates_do_not_invent_a_move(self):
        raw = HEADER + b'PU0,0;PD42PU100,100;PD400,0;PU;'
        parsed, paths = source_paths(raw)
        self.assertEqual([p['raw_points'] for p in paths], [[[0, 0], [400, 0]]])
        self.assertNotIn('layout_command_boundaries_applied', parsed)

    def test_boundary_and_page_offset_modes_can_both_apply(self):
        raw = STANDARD + b'PG12000\r\nDT@\r\nPU600,600\r\nLBTEST@' + RECTANGLE.replace(b';', b'')
        parsed, paths = source_paths(raw)
        self.assertEqual([p['raw_points'][0] for p in paths], [[0, 0], [12000, 0]])
        self.assertEqual(labels(parsed), [('TEST', {'x': 12600, 'y': 600})])
        self.assertTrue(parsed['layout_command_boundaries_applied'])
        self.assertTrue(parsed['page_x_offsets_applied'])

    def test_default_projection_keeps_existing_parser(self):
        parsed = parse_plt(DT_SAMPLE, projection=True)
        self.assertEqual(labels(parsed), [('TEST@PU0,0', {'x': 600, 'y': 600})])
        self.assertNotIn('layout_command_boundaries_applied', parsed)

    def test_export_uses_the_same_boundary_rules_and_exact_source_lines(self):
        for raw in (STANDARD.replace(b';', b'\r\n'), STANDARD.replace(b';', b''), DT_SAMPLE):
            with self.subTest(raw=raw):
                recognized = recognize_source(raw)
                piece = recognized['pieces'][0]
                self.assertTrue(recognized['source']['layout_command_boundaries_applied'])
                self.assertEqual(len(piece['fragments']), 4)
                doc = {'schema_version':1, 'source_sha256':recognized['source']['sha256'],
                       'source_metadata':recognized['source'], 'candidates':[piece],
                       'placements':[{'piece_id':piece['id'],'x':20,'y':30}]}
                output = export_project(raw, doc)
                parsed, paths = source_paths(output)
                self.assertEqual([translated(p['raw_points']) for p in paths], [EXPECTED])
                if raw == DT_SAMPLE:
                    min_x = min(p[0] for p in paths[0]['raw_points'])
                    min_y = min(p[1] for p in paths[0]['raw_points'])
                    self.assertEqual([(word,round(point['x']-min_x,8),round(point['y']-min_y,8))
                                      for word,point in labels(parsed)], [('TEST',600,600)])
