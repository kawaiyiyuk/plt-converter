import io
import unittest
from unittest.mock import patch

from app.services.plt_metadata import inspect_plt


class PltMetadataTest(unittest.TestCase):
    def test_arc_bounds_match_rendered_parser(self):
        from app.services.plt_parser import parse_plt

        for command in ('AA0,0,90', 'AR-100,0,90'):
            with self.subTest(command=command):
                source = f'IN;PU100,0;PD;{command};PU;'.encode()
                preview = inspect_plt(source)
                rendered = parse_plt(source)['metrics']
                self.assertAlmostEqual(preview['min_x'], rendered['min_x'])
                self.assertAlmostEqual(preview['min_y'], rendered['min_y'])
                self.assertAlmostEqual(preview['max_x'], rendered['max_x'])
                self.assertAlmostEqual(preview['max_y'], rendered['max_y'])
                self.assertAlmostEqual(preview['width_mm'], rendered['width_mm'], places=3)
                self.assertAlmostEqual(preview['height_mm'], rendered['height_mm'], places=3)
                self.assertEqual(preview['path_count'], rendered['path_count'])

    def test_arc_at_end_of_file_matches_rendered_parser(self):
        from app.services.plt_parser import parse_plt

        for command in ('AA0,0,90', 'AR-100,0,90'):
            with self.subTest(command=command):
                source = f'IN;PU100,0;PD;{command}'.encode()
                preview = inspect_plt(source)
                rendered = parse_plt(source)['metrics']
                self.assertAlmostEqual(preview['min_x'], rendered['min_x'])
                self.assertAlmostEqual(preview['min_y'], rendered['min_y'])
                self.assertAlmostEqual(preview['max_x'], rendered['max_x'])
                self.assertAlmostEqual(preview['max_y'], rendered['max_y'])
                self.assertEqual(preview['path_count'], rendered['path_count'])

    def test_label_only_bounds_match_rendered_parser(self):
        from app.services.plt_parser import parse_plt

        for source in (b'IN;PU123,456;LBsample\x03', b'IN;PU123,456;LBsample'):
            with self.subTest(source=source):
                preview = inspect_plt(source)
                rendered = parse_plt(source)['metrics']
                self.assertEqual(preview['min_x'], rendered['min_x'])
                self.assertEqual(preview['min_y'], rendered['min_y'])
                self.assertEqual(preview['max_x'], rendered['max_x'])
                self.assertEqual(preview['max_y'], rendered['max_y'])
                self.assertEqual(preview['path_count'], rendered['path_count'])

    def test_preview_route_accepts_arc_and_label_content(self):
        from app import create_app

        sources = (
            b'IN;PU100,0;PD;AA0,0,90',
            b'IN;PU100,0;PD;AR-100,0,90',
            b'IN;PU123,456;LBsample\x03',
        )
        client = create_app().test_client()
        for index, source in enumerate(sources):
            with self.subTest(index=index), patch('app.routes.enforce_rate_limit'):
                response = client.post(
                    '/api/v1/plt/preview',
                    data={'file': (io.BytesIO(source), f'sample-{index}.plt')},
                    content_type='multipart/form-data',
                )
                self.assertEqual(response.status_code, 200, response.get_json())

    def test_preview_route_exposes_renderer_ink_bounds_for_labels(self):
        from app import create_app
        from app.services.pdf_renderer import measure_display_bounds
        from app.services.plt_parser import parse_plt

        source = b'IN;PU123,456;LBsample\x03PU8000,456;LBlong label\x03'
        expected = measure_display_bounds(parse_plt(source))
        with patch('app.routes.enforce_rate_limit'):
            response = create_app().test_client().post(
                '/api/v1/plt/preview',
                data={'file': (io.BytesIO(source), 'labels.plt')},
                content_type='multipart/form-data',
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['display_bounds_pt'], expected)
        self.assertEqual(len(payload['display_bounds_pt']['text_bounds_pt']), 2)
        self.assertEqual([label['text'] for label in payload['labels']], ['sample', 'long label'])
        self.assertEqual(payload['metadata'], inspect_plt(source))

    def test_preview_route_returns_decoded_gb18030_label(self):
        from app import create_app

        source = 'IN;PU0,0;LB中文纸样\x03'.encode('gb18030')
        with patch('app.routes.enforce_rate_limit'):
            response = create_app().test_client().post(
                '/api/v1/plt/preview',
                data={'file': (io.BytesIO(source), 'chinese.plt')},
                content_type='multipart/form-data',
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['labels'][0]['text'], '中文纸样')

    def test_rejects_excessive_label_content(self):
        source = b'IN;PU0,0;LB' + (b'a' * 1001) + b'\x03'
        with patch.dict('os.environ', {'PLT_MAX_TEXT_CHARS': '1000'}):
            with self.assertRaisesRegex(ValueError, '文本内容过多'):
                inspect_plt(source)

    def test_circle_bounds_match_rendered_parser(self):
        from app.services.plt_parser import parse_plt

        source = b'IN;PU100,100;CI50;'
        preview = inspect_plt(source)
        rendered = parse_plt(source)['metrics']
        self.assertAlmostEqual(preview['width_mm'], rendered['width_mm'], places=3)
        self.assertAlmostEqual(preview['height_mm'], rendered['height_mm'], places=3)
        self.assertEqual(preview['path_count'], rendered['path_count'])
        self.assertGreater(preview['width_mm'], 0)

    def test_first_pen_down_includes_current_origin(self):
        from app.services.plt_parser import parse_plt

        source = b'IN;PD100,100;'
        preview = inspect_plt(source)
        rendered = parse_plt(source)['metrics']
        self.assertAlmostEqual(preview['width_mm'], rendered['width_mm'], places=3)
        self.assertAlmostEqual(preview['height_mm'], rendered['height_mm'], places=3)

    def test_trailing_pen_up_move_does_not_expand_drawn_bounds(self):
        from app.services.plt_parser import parse_plt

        source = b'IN;PU0,0;PD100,100;PU10000,10000;'
        preview = inspect_plt(source)
        rendered = parse_plt(source)['metrics']
        self.assertAlmostEqual(preview['width_mm'], rendered['width_mm'], places=3)
        self.assertAlmostEqual(preview['height_mm'], rendered['height_mm'], places=3)

    def test_pen_up_pa_and_pr_moves_do_not_expand_drawn_bounds(self):
        from app.services.plt_parser import parse_plt

        source = b'IN;PA10000,10000;PR-5000,-5000;PU0,0;PD100,100;'
        preview = inspect_plt(source)
        rendered = parse_plt(source)['metrics']
        self.assertAlmostEqual(preview['width_mm'], rendered['width_mm'], places=3)
        self.assertAlmostEqual(preview['height_mm'], rendered['height_mm'], places=3)

    def test_empty_pen_down_draws_following_pa_and_pr_coordinates(self):
        from app.services.plt_parser import parse_plt

        source = b'IN;PU100,100;PD;PA200,200;PR50,-50;PU;'
        preview = inspect_plt(source)
        rendered = parse_plt(source)['metrics']
        self.assertAlmostEqual(preview['width_mm'], rendered['width_mm'], places=3)
        self.assertAlmostEqual(preview['height_mm'], rendered['height_mm'], places=3)
        self.assertEqual(preview['path_count'], rendered['path_count'])

    def test_consecutive_pen_down_commands_form_one_path_until_pen_up(self):
        from app.services.plt_parser import parse_plt

        source = b'IN;PD100,100;PD200,200;PU;PD300,300;PU;'
        preview = inspect_plt(source)
        rendered = parse_plt(source)['metrics']
        self.assertEqual(preview['path_count'], rendered['path_count'])

    def test_pen_switch_starts_new_path(self):
        from app.services.plt_parser import parse_plt

        source = b'IN;PD100,100;SP2;PD200,200;'
        preview = inspect_plt(source)
        rendered = parse_plt(source)['metrics']
        self.assertEqual(preview['path_count'], rendered['path_count'])

    def test_initialize_resets_relative_position_for_bounds(self):
        from app.services.plt_parser import parse_plt

        source = b'IN;PR100,100;PD100,100;IN;PU10,10;PD20,20;'
        preview = inspect_plt(source)
        rendered = parse_plt(source)['metrics']
        self.assertAlmostEqual(preview['width_mm'], rendered['width_mm'], places=3)
        self.assertAlmostEqual(preview['height_mm'], rendered['height_mm'], places=3)

    def test_reads_absolute_coordinates(self):
        metadata = inspect_plt(
            b'IN;PU0,0;PD1016,0,1016,2032,0,2032,0,0;'
        )

        self.assertEqual(metadata['point_count'], 5)
        self.assertEqual(metadata['path_count'], 1)
        self.assertAlmostEqual(metadata['width_mm'], 25.4, places=3)
        self.assertAlmostEqual(metadata['height_mm'], 50.8, places=3)

    def test_rejects_command_complexity(self):
        source = b''.join(b'PU0,0;' for _ in range(1000)) + b'PD1,1;'
        with patch.dict('os.environ', {'PLT_MAX_COMMANDS': '1000'}):
            with self.assertRaisesRegex(ValueError, '命令数量'):
                inspect_plt(source)

    def test_rejects_point_complexity_before_building_unbounded_list(self):
        coordinates = ','.join(f'{index},{index}' for index in range(1001))
        with patch.dict('os.environ', {'PLT_MAX_POINTS': '1000'}):
            with self.assertRaisesRegex(ValueError, '参数数量|坐标点过多'):
                inspect_plt(f'IN;PD{coordinates};'.encode())

    def test_rejects_non_finite_bounds_after_finite_coordinates(self):
        with self.assertRaisesRegex(ValueError, '尺寸'):
            inspect_plt(b'IN;PU-1e308,0;PD1e308,0;')

    def test_rejects_scale_that_cannot_be_converted_to_float(self):
        with self.assertRaisesRegex(ValueError, 'units_per_inch'):
            inspect_plt(b'IN;PU0,0;PD1016,1016;', units_per_inch=10 ** 309)

    def test_ignores_non_finite_coordinates(self):
        metadata = inspect_plt(b'IN;PU0,0;PDNaN,1,Infinity,2,1016,1016;')

        self.assertTrue(all(
            value == value and value not in (float('inf'), float('-inf'))
            for value in (metadata['min_x'], metadata['min_y'], metadata['max_x'], metadata['max_y'])
        ))


if __name__ == '__main__':
    unittest.main()
