"""Real upload -> bounded parser -> internal PDF -> tile PLT regression checks."""
import io
import math
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import pymupdf as fitz
import test_projection as fixtures

BASE = fixtures.BASE


class ProjectionPltTests(unittest.TestCase):
    setUp = fixtures.ProjectionTests.setUp
    metadata = fixtures.ProjectionTests.metadata
    identity = fixtures.ProjectionTests.identity
    upload = fixtures.ProjectionTests.upload
    tile = fixtures.ProjectionTests.tile
    renew = fixtures.ProjectionTests.renew

    def setUp(self):
        fixtures.ProjectionTests.setUp(self)
        self.ledger.policy.derived_max_bytes = 64 * 1024 * 1024
        self.ledger.policy.owner_derived_bytes = 128 * 1024 * 1024

    def test_plt_upload_is_single_page_real_scale_without_print_guides(self):
        source = b'IN;SP2;PU0,0;PD4000,0,4000,2000,0,2000,0,0;PU;'
        response = self.upload(source, 'pattern.plt')
        self.assertEqual(response.status_code, 201, response.get_json())
        body = response.get_json()
        self.assertEqual(body['source_format'], 'plt')
        self.assertEqual(body['rendering_warnings'], [])
        self.assertEqual(body['page_count'], 1)
        self.assertFalse(body['requires_metadata_choice'])
        self.assertAlmostEqual(body['width_mm'], 100, places=3)
        self.assertAlmostEqual(body['height_mm'], 50, places=3)
        document_id = body['document_id']
        source_path = self.ledger.source_path(document_id)
        with fitz.open(source_path) as document:
            self.assertEqual(document.metadata['subject'], '')
            self.assertEqual(document[0].get_text(), '')
            for drawing in document[0].get_drawings():
                self.assertEqual(drawing['color'], (0, 0, 0))
        self.assertFalse(list((self.ledger.root / 'staging').iterdir()))
        self.assertEqual(self.tile(document_id, width_mm=100, height_mm=50, width_px=400, height_px=200).status_code, 200)
        summary = self.client.get(f'{BASE}/{document_id}', headers={'Authorization': 'x'}).get_json()
        self.assertEqual(summary['source_format'], 'plt')
        self.assertEqual(self.renew(document_id).status_code, 200)
        self.assertEqual(self.client.delete(f'{BASE}/{document_id}', headers={'Authorization': 'x'}).status_code, 204)
        self.assertFalse(source_path.exists())

    def test_hpgl_and_degenerate_geometry_and_label_have_positive_page(self):
        for source in (b'IN;PU0,0;PD4000,0;', b'IN;PU0,0;PD0,4000;', b'IN;PU0,0;LBhello\x03;'):
            with self.subTest(source=source):
                response = self.upload(source, 'pattern.HPGL')
                self.assertEqual(response.status_code, 201, response.get_json())
                body = response.get_json()
                self.assertGreater(body['width_mm'], 0)
                self.assertGreater(body['height_mm'], 0)
                with fitz.open(self.ledger.source_path(body['document_id'])) as document:
                    if b'LB' in source:
                        self.assertIn('hello', document[0].get_text())
                        self.assertTrue(any('8 Tf' in document.xref_stream(x).decode() for x in document[0].get_contents()))

    def test_unknown_geometric_commands_rejected_without_leaks(self):
        for command in ('SC0,100,0,100', 'IP0,0,4000,4000', 'ZZ1', 'RO90', 'AA1,2'):
            response = self.upload(('IN;PU0,0;PD4000,4000;' + command + ';').encode(), 'bad.plt')
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertIn(command[:2], response.get_json()['error'])
            self.assertFalse(list((self.ledger.root / 'staging').iterdir()))
            self.assertFalse(list((self.ledger.root / 'documents').iterdir()))
            with self.ledger.transaction() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 0)

    def test_plt_skips_layout_analysis(self):
        body = self.upload(b'IN;PU0,0;PD4000,4000;', 'pattern.plt').get_json()
        self.assertIn('document_id', body)
        with patch('app.projection_layout.submit_analysis') as submit:
            response = self.client.post(f"{BASE}/{body['document_id']}/layout-analysis", headers={'Authorization': 'x'})
        self.assertEqual(response.status_code, 409)
        submit.assert_not_called()

    def test_arc_and_circle_chord_error_at_most_point_zero_five_mm(self):
        from app.services.plt_parser import parse_plt
        for source in ('IN;PU40000,0;PD;AA0,0,90,90;', 'IN;PU40000,0;PD;AR-40000,0,90,90;', 'IN;PU0,0;CI40000,90;'):
            document = parse_plt(source, strict=True, max_chord_error_mm=0.05)
            paths = [s for s in document['shapes'] if s['type'] == 'path']
            self.assertTrue(paths)
            for shape in paths:
                for first, second in zip(shape['points'], shape['points'][1:]):
                    chord = math.hypot(first['x']-second['x'], first['y']-second['y'])
                    error = (40000-math.sqrt(max(0,40000**2-(chord/2)**2))) * 25.4 / 1016
                    self.assertLessEqual(error, 0.050000001)

    def assert_empty(self):
        for folder in ('staging', 'operations', 'documents', 'metadata'):
            self.assertFalse(list((self.ledger.root / folder).iterdir()), folder)
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_original_filename_selects_format_and_pdf_compatibility(self):
        source = b'IN;PU0,0;PD4000,2000;'
        response = self.client.post(BASE, headers={'Authorization': 'x'}, data={
            'file': (io.BytesIO(source), 'wxfile.tmp'), 'original_filename': 'original.HPGL', 'source_format': 'plt'})
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()['filename'], 'original.hpgl')
        self.assertEqual(response.get_json()['source_format'], 'plt')
        response = self.client.post(BASE, headers={'Authorization': 'x'}, data={
            'file': (io.BytesIO(source), 'wxfile.tmp'), 'original_filename': 'original.plt', 'source_format': 'pdf'})
        self.assertEqual(response.status_code, 400)
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['source_format'], 'pdf')

    def test_missing_derived_policy_rejects_only_plt(self):
        self.ledger.policy.derived_max_bytes = 0
        self.assertEqual(self.upload(b'IN;PD100,100;', 'x.plt').status_code, 503)
        self.assert_empty()
        self.assertEqual(self.upload().status_code, 201)

    def test_owner_and_global_derived_reservation_and_release(self):
        from app.projection_store import ProjectionRejected
        first = self.ledger.reserve(self.owner, 'upload')
        self.ledger.reserve_derived(first)
        first_budget = self.ledger.lease(first)['budget']
        self.assertGreaterEqual(first_budget, self.ledger.policy.upload_bytes + 64 * 1024 * 1024)
        second = self.ledger.reserve(self.owner, 'upload')
        self.ledger.reserve_derived(second)
        third = self.ledger.reserve(self.owner, 'upload')
        with self.assertRaises(ProjectionRejected) as rejected:
            self.ledger.reserve_derived(third)
        self.assertEqual(rejected.exception.code, 'projection_owner_derived_capacity')
        for lease in (first, second, third):
            self.ledger.release(lease)
        lease = self.ledger.reserve(self.owner, 'upload')
        self.ledger.policy.global_disk_bytes = self.ledger.policy.system_bytes + self.ledger.policy.render_pool_bytes + self.ledger.policy.upload_bytes + 1024
        with self.assertRaises(ProjectionRejected) as rejected:
            self.ledger.reserve_derived(lease)
        self.assertEqual(rejected.exception.code, 'projection_capacity')
        self.ledger.release(lease)
        self.assert_empty()

    def test_derived_session_bytes_expiry_and_replacement_reclaim(self):
        source = b'IN;PU0,0;PD4000,2000;'
        body = self.upload(source, 'x.plt').get_json()
        document = body['document_id']
        path = self.ledger.source_path(document)
        with self.ledger.transaction() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (document,)).fetchone()
            self.assertEqual(row['source_bytes'], 0)
            self.assertEqual(row['derived_bytes'], path.stat().st_size)
            self.assertGreaterEqual(row['budget'], path.stat().st_size)
        self.assertEqual(self.upload(source, 'replacement.plt').status_code, 201)
        self.assertFalse(path.exists())
        with self.ledger.transaction() as db:
            db.execute('UPDATE sessions SET expires=0')
        self.ledger.reconcile()
        self.assert_empty()

    def test_derived_failure_timeout_and_publish_failure_reclaim(self):
        from app.projection_store import ProjectionRejected
        for method in ('oversize', 'timeout', 'rename'):
            with self.subTest(method=method):
                self.ledger.policy.derived_max_bytes = 64 * 1024 * 1024
                if method == 'oversize':
                    self.ledger.policy.derived_max_bytes = 128
                    response = self.upload(b'IN;PD4000,2000;', 'x.plt')
                    self.assertEqual(response.status_code, 400, response.get_json())
                else:
                    target = 'app.projection_runtime.run' if method == 'timeout' else 'app.projection_store.os.replace'
                    error = ProjectionRejected('projection_child_timeout', 'fixture timeout', 503) if method == 'timeout' else OSError('disk full')
                    with patch(target, side_effect=error):
                        response = self.upload(b'IN;PD4000,2000;', 'x.plt')
                    self.assertEqual(response.status_code, 503)
                self.assert_empty()

    def test_child_limit_environment_and_hard_strict_point_cap(self):
        from app.projection_runtime import _child_environment
        from app.services.plt_parser import parse_plt
        values = {'PLT_MAX_COMMANDS': '1000', 'PLT_MAX_PATHS': '100', 'PLT_MAX_POINTS': '1000',
                  'PLT_MAX_TEXT_CHARS': '1000', 'CONVERSION_SERVICE_TOKEN': 'secret'}
        with patch.dict(os.environ, values):
            child = _child_environment()
            for key in values:
                self.assertEqual(child.get(key), None if key == 'CONVERSION_SERVICE_TOKEN' else values[key])
            # A real supervised child inherits the parser limits, and rejects
            # arc expansion before building an over-budget list of points.
            response = self.upload(b'IN;PU400000,0;PD;AA0,0,360,0.1;', 'x.plt')
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertIn('坐标点', response.get_json()['error'])
        self.assert_empty()
        with patch.dict(os.environ, {'PLT_MAX_POINTS': '99999999'}):
            with self.assertRaisesRegex(ValueError, '500000'):
                parse_plt('IN;PU1e12,0;PD;AA0,0,360;', strict=True, max_chord_error_mm=0.05)

    def test_safe_ignored_command_warns_and_malformed_numbers_reject(self):
        response = self.upload(b'IN;VS10;PD100,100;', 'x.plt')
        self.assertEqual(response.status_code, 201)
        self.assertIn('VS', response.get_json()['rendering_warnings'][0])
        for argument in ('1e,1', '1,inf', '1e999,1', '1,2,3', '1e,1e,500,600',
                         'inf,inf,500,600', '1e999,1e999,500,600', '1.2.3,4.5.6,500,600'):
            response = self.upload(('IN;PD' + argument + ';').encode(), 'x.plt')
            self.assertEqual(response.status_code, 400, response.get_json())
        self.assert_empty()

    def test_sp0_does_not_add_phantom_geometry_and_large_arc_rejects(self):
        response = self.upload(b'IN;PD4000,2000;SP0;PD9000,9000;CI1000;LBhidden\x03;', 'x.plt')
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertAlmostEqual(response.get_json()['width_mm'], 100, places=3)
        self.assertAlmostEqual(response.get_json()['height_mm'], 50, places=3)
        for command in ('PU1e308,0;CI1e308', 'PU1e308,0;PD;AA0,0,90', 'PU1,0;PD;AA0,0,1e308'):
            response = self.upload(('IN;' + command + ';').encode(), 'x.plt')
            self.assertEqual(response.status_code, 400, response.get_json())
        self.assert_empty()

    def test_plt_real_supervised_timeout_kills_reaps_and_releases_derived(self):
        import subprocess
        import sys
        import json
        real_popen = subprocess.Popen
        processes = []
        specification = {}
        def hanging(*args, **kwargs):
            specification.update(json.loads(Path(args[0][-1]).read_text()))
            process = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            processes.append(process)
            return process
        self.ledger.policy.parse_wall_seconds = 0.15
        with patch('app.projection_runtime.subprocess.Popen', side_effect=hanging):
            response = self.upload(b'IN;PD4000,2000;', 'x.plt')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['error_code'], 'projection_child_timeout')
        self.assertEqual(specification['limits']['file'], 64 * 1024 * 1024)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        self.assert_empty()

    def test_strict_point_budget_includes_duplicated_path_starts(self):
        from app.services.plt_parser import parse_plt
        with patch.dict(os.environ, {'PLT_MAX_POINTS': '1000'}):
            with self.assertRaisesRegex(ValueError, '坐标点过多'):
                parse_plt('IN;' + 'SP1;PD1,1,2,2,3,3;' * 260, strict=True, max_chord_error_mm=0.05)

    def test_strict_rejects_compound_numeric_tokens_without_dropping_coordinates(self):
        source = b'IN;PU0,0;PD100-200,300-400,500,600;'
        response = self.upload(source, 'compound.plt')
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn('PD', response.get_json()['error'])
        self.assertIn('参数格式无效', response.get_json()['error'])
        self.assert_empty()

    def test_legacy_compound_numeric_token_behavior_is_unchanged(self):
        from app.services.plt_parser import parse_plt
        document = parse_plt(b'IN;PU0,0;PD100-200,300-400,500,600;')
        self.assertEqual(document['shapes'][0]['points'], [{'x': 0, 'y': 0}, {'x': 500, 'y': 600}])

    def test_strict_accepts_separated_signed_decimal_and_exponent_tokens(self):
        from app.services.plt_parser import parse_plt
        document = parse_plt('IN;PU0,0;PD+100,-200 3e2,-4e2\t+.5,6.;', strict=True)
        self.assertEqual(document['shapes'][0]['points'], [
            {'x': 0, 'y': 0}, {'x': 100, 'y': -200},
            {'x': 300, 'y': -400}, {'x': 0.5, 'y': 6},
        ])

    def test_projection_rectangles_keep_all_edges_at_original_scale(self):
        from app.services.plt_parser import parse_plt
        from app.services.pdf_renderer import render_projection_pdf
        for width, height, x, y in ((100, 100, 0, 0), (10000, 10000, 0, 0),
                                    (10000, 10000, 1234, -5678), (4000, 2000, 0, 0),
                                    (4001, 2001, -2500, 500)):
            with self.subTest(width=width, height=height, origin=(x, y)):
                source = (f'IN;SP1;PU{x},{y};PD{x+width},{y},{x+width},{y+height},'
                          f'{x},{y+height},{x},{y};')
                parsed = parse_plt(source, strict=True, max_chord_error_mm=0.049)
                with fitz.open(stream=render_projection_pdf(parsed), filetype='pdf') as pdf:
                    page = pdf[0]
                    self.assertAlmostEqual(page.rect.width * 25.4 / 72, width * 0.025, places=3)
                    self.assertAlmostEqual(page.rect.height * 25.4 / 72, height * 0.025, places=3)
                    pixels = page.get_pixmap(matrix=fitz.Matrix(4, 4), colorspace=fitz.csGRAY)
                    edge_centers = ((0, pixels.height // 2), (pixels.width - 1, pixels.height // 2),
                                    (pixels.width // 2, 0), (pixels.width // 2, pixels.height - 1))
                    for position in edge_centers:
                        self.assertLess(pixels.pixel(*position)[0], 128, f'missing edge at {position}')

    def test_strict_parameterless_sp_matches_sp0_for_every_shape_type(self):
        from app.services.plt_parser import parse_plt
        prefix = 'IN;SP1;PU0,0;PD100,0;'
        suffix = 'PD10000,10000;CI400;LBhidden\x03;SP2;PU200,200;PD300,300;'
        empty = parse_plt(prefix + 'SP;' + suffix, strict=True, max_chord_error_mm=0.049)
        zero = parse_plt(prefix + 'SP0;' + suffix, strict=True, max_chord_error_mm=0.049)
        self.assertEqual(empty, zero)
        self.assertEqual([shape['type'] for shape in empty['shapes']], ['path', 'path'])
        self.assertAlmostEqual(empty['metrics']['width_mm'], 7.5)

    def test_legacy_parameterless_sp_keeps_existing_pen_one(self):
        from app.services.plt_parser import parse_plt
        parsed = parse_plt('IN;SP2;PU0,0;PD100,0;SP;PD200,200;')
        self.assertEqual([shape['pen'] for shape in parsed['shapes']], [2, 1])

    def test_strict_consecutive_labels_advance_in_rendered_pdf(self):
        from app.services.plt_parser import parse_plt
        from app.services.pdf_renderer import render_projection_pdf
        parsed = parse_plt('IN;SP1;PU0,0;LBAAA\x03;LBBBB\x03;', strict=True)
        labels = [shape for shape in parsed['shapes'] if shape['type'] == 'text']
        self.assertAlmostEqual(labels[1]['point']['x'], 24 * 1016 / 72)
        with fitz.open(stream=render_projection_pdf(parsed), filetype='pdf') as pdf:
            chars = [char for block in pdf[0].get_text('rawdict')['blocks'] if block['type'] == 0
                     for line in block['lines'] for span in line['spans'] for char in span['chars']]
            self.assertEqual(''.join(char['c'] for char in chars), 'AAABBB')
            self.assertAlmostEqual(chars[3]['origin'][0] - chars[0]['origin'][0], 24, places=3)

    def test_strict_pd_and_pr_start_after_label(self):
        from app.services.plt_parser import parse_plt
        advance = 16 * 1016 / 72
        absolute = parse_plt('IN;SP1;PU0,0;LBAB\x03;PD2000,0;', strict=True)
        self.assertAlmostEqual(absolute['shapes'][-1]['points'][0]['x'], advance)
        relative = parse_plt('IN;SP1;PU0,0;LBAB\x03;PR400,200;PD400,200;', strict=True)
        points = relative['shapes'][-1]['points']
        self.assertAlmostEqual(points[0]['x'], advance + 400)
        self.assertAlmostEqual(points[1]['x'], advance + 800)
        self.assertEqual([point['y'] for point in points], [200, 400])

    def test_strict_label_breaks_existing_stroke_without_connector(self):
        from app.services.plt_parser import parse_plt
        from app.services.pdf_renderer import render_projection_pdf
        parsed = parse_plt('IN;SP1;PU0,0;PD400,0;LBAB\x03;PD2000,0;', strict=True)
        paths = [shape for shape in parsed['shapes'] if shape['type'] == 'path']
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0]['points'], [{'x': 0, 'y': 0}, {'x': 400, 'y': 0}])
        self.assertAlmostEqual(paths[1]['points'][0]['x'], 400 + 16 * 1016 / 72)
        with fitz.open(stream=render_projection_pdf(parsed), filetype='pdf') as pdf:
            lines = [item for drawing in pdf[0].get_drawings() for item in drawing['items']]
            self.assertEqual(len(lines), 2)
            self.assertEqual([item[0] for item in lines], ['l', 'l'])
            self.assertAlmostEqual(lines[1][1].x - lines[0][2].x, 16, places=3)

    def test_strict_sp0_label_moves_position_without_rendering(self):
        from app.services.plt_parser import parse_plt
        parsed = parse_plt('IN;SP0;PU0,0;LBAB\x03;SP1;PR400,200;PD400,200;', strict=True)
        self.assertEqual([shape['type'] for shape in parsed['shapes']], ['path'])
        self.assertAlmostEqual(parsed['shapes'][0]['points'][0]['x'], 16 * 1016 / 72 + 400)

    def test_legacy_labels_keep_existing_position_and_stroke_behavior(self):
        from app.services.plt_parser import parse_plt
        parsed = parse_plt('IN;SP1;PU0,0;PD400,0;LBAB\x03LBCD\x03PD2000,0;')
        self.assertEqual([shape['point']['x'] for shape in parsed['shapes'] if shape['type'] == 'text'], [400, 400])
        paths = [shape for shape in parsed['shapes'] if shape['type'] == 'path']
        self.assertEqual(len(paths), 1)
        self.assertEqual([point['x'] for point in paths[0]['points']], [0, 400, 2000])

    def test_strict_label_controls_keep_positions_and_following_pen_move(self):
        from app.services.plt_parser import parse_plt
        from app.services.pdf_renderer import render_projection_pdf
        cell = 8 * 1016 / 72
        source = 'IN;SP1;PU100,100;LBA\rB\nC\tD\bE\x03;PD2000,100;'
        parsed = parse_plt(source, strict=True)
        labels = [shape for shape in parsed['shapes'] if shape['type'] == 'text']
        self.assertEqual([shape['text'] for shape in labels], list('ABCDE'))
        expected = [(100, 100), (100, 100), (100 + cell, 100 - cell),
                    (100 + 8 * cell, 100 - cell), (100 + 8 * cell, 100 - cell)]
        for shape, (x, y) in zip(labels, expected):
            self.assertAlmostEqual(shape['point']['x'], x)
            self.assertAlmostEqual(shape['point']['y'], y)
        self.assertAlmostEqual(parsed['shapes'][-1]['points'][0]['x'], 100 + 9 * cell)
        self.assertAlmostEqual(parsed['shapes'][-1]['points'][0]['y'], 100 - cell)
        with fitz.open(stream=render_projection_pdf(parsed), filetype='pdf') as pdf:
            chars = [char for block in pdf[0].get_text('rawdict')['blocks'] if block['type'] == 0
                     for line in block['lines'] for span in line['spans'] for char in span['chars']]
            self.assertEqual(''.join(char['c'] for char in chars), 'ABCDE')
            self.assertAlmostEqual(chars[2]['origin'][0] - chars[1]['origin'][0], 8, places=3)
            self.assertAlmostEqual(chars[2]['origin'][1] - chars[1]['origin'][1], 8, places=3)
            self.assertAlmostEqual(chars[3]['origin'][0] - chars[2]['origin'][0], 56, places=3)

    def test_strict_label_carriage_origin_survives_labels_and_resets_on_moves(self):
        from app.services.plt_parser import parse_plt
        cell = 8 * 1016 / 72
        parsed = parse_plt('IN;SP1;PU100,100;LBA\n\x03;LB\rB\x03;PU1000,2000;LB\rC\x03;', strict=True)
        labels = [shape for shape in parsed['shapes'] if shape['type'] == 'text']
        self.assertEqual([shape['text'] for shape in labels], list('ABC'))
        self.assertAlmostEqual(labels[1]['point']['x'], 100)
        self.assertAlmostEqual(labels[1]['point']['y'], 100 - cell)
        self.assertEqual(labels[2]['point'], {'x': 1000, 'y': 2000})

    def test_strict_label_backspace_tab_and_other_controls(self):
        from app.services.plt_parser import parse_plt
        cell = 8 * 1016 / 72
        parsed = parse_plt('IN;SP1;PU0,0;LB\bA\x01\x04\tB\x03;', strict=True)
        labels = [shape for shape in parsed['shapes'] if shape['type'] == 'text']
        self.assertEqual([shape['text'] for shape in labels], ['A', 'B'])
        self.assertEqual(labels[0]['point'], {'x': 0, 'y': 0})
        self.assertAlmostEqual(labels[1]['point']['x'], 8 * cell)
        parsed = parse_plt('IN;SP1;LB12345678\tZ\x03;', strict=True)
        self.assertAlmostEqual(parsed['shapes'][-1]['point']['x'], 16 * cell)

    def test_strict_astral_label_uses_warned_replacement_without_losing_suffix(self):
        from app.services.plt_parser import parse_plt
        from app.services.pdf_renderer import render_projection_pdf
        parsed = parse_plt('IN;SP1;PU0,0;LBA😀B\x03;PD2000,0;', strict=True)
        self.assertEqual(parsed['shapes'][0]['text'], 'A□B')
        self.assertIn('不支持的增补字符以□兼容显示', parsed['warnings'])
        self.assertAlmostEqual(parsed['shapes'][-1]['points'][0]['x'], 24 * 1016 / 72)
        with fitz.open(stream=render_projection_pdf(parsed), filetype='pdf') as pdf:
            page = pdf[0]
            self.assertIn('A□B', page.get_text())
            chars = [char for block in page.get_text('rawdict')['blocks'] if block['type'] == 0
                     for line in block['lines'] for span in line['spans'] for char in span['chars']]
            self.assertAlmostEqual(chars[2]['origin'][0] - chars[0]['origin'][0], 16, places=3)
            pixels = page.get_pixmap(matrix=fitz.Matrix(8, 8), colorspace=fitz.csGRAY)
            # The middle cell must contain real visible placeholder ink.
            x_start = int((chars[1]['origin'][0] + 1) * 8)
            x_end = int((chars[2]['origin'][0] - 1) * 8)
            self.assertTrue(any(pixels.pixel(x, y)[0] < 128
                                for y in range(pixels.height) for x in range(x_start, x_end)))

    def test_strict_pa_pr_without_coordinates_reset_label_carriage_origin(self):
        from app.services.plt_parser import parse_plt
        for command in ('PA', 'PR'):
            with self.subTest(command=command):
                parsed = parse_plt('IN;SP1;PU100,100;LBA\x03;' + command + ';LB\rB\x03;', strict=True)
                labels = [shape for shape in parsed['shapes'] if shape['type'] == 'text']
                self.assertAlmostEqual(labels[1]['point']['x'], 100 + 8 * 1016 / 72)
                self.assertEqual(labels[1]['point']['y'], 100)

    def test_strict_ci_preserves_label_carriage_origin(self):
        from app.services.plt_parser import parse_plt
        parsed = parse_plt('IN;SP1;PU100,100;LBA\x03;CI10;LB\rB\x03;', strict=True)
        labels = [shape for shape in parsed['shapes'] if shape['type'] == 'text']
        self.assertEqual(labels[1]['point'], {'x': 100, 'y': 100})
