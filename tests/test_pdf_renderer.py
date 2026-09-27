import base64
import json
import re
import unittest
import zlib
from unittest.mock import patch

import pymupdf

from app.services.pdf_renderer import clip_segment, measure_display_bounds, render_pdf, utf16be_hex
from app.services.pdf_metadata import (
    ROUNDTRIP_METADATA_PREFIX,
    decode_pdf_layout_metadata,
    normalize_pdf_layout_metadata,
)
from app.services.pdf_to_plt import convert_pdf_to_plt, read_pdf_layout_metadata
from app.services.plt_parser import parse_plt


class PdfRendererTest(unittest.TestCase):
    def test_text_display_bounds_use_pdf_ink_without_changing_geometry(self):
        document = parse_plt(b'IN;PU123,456;LBsample\x03;')
        self.assertEqual(document['metrics']['width_mm'], 0)
        self.assertEqual(document['metrics']['height_mm'], 0)
        display = measure_display_bounds(document)
        self.assertLess(display['min_x_pt'], 0)
        self.assertLess(display['min_y_pt'], 0)
        self.assertGreater(display['max_x_pt'], 8)
        self.assertGreater(display['max_y_pt'], 8)
        self.assertEqual(len(display['text_bounds_pt']), 1)

    def test_non_inking_lb_does_not_reject_line_or_shift_next_label(self):
        for control in ('\t', '\n', '\r'):
            with self.subTest(control=repr(control)):
                source = ('IN;PU0,0;PD1016,0;PU0,0;LB' + control +
                          '\x03PU0,0;LB中文\x03').encode('utf-8')
                document = parse_plt(source)
                self.assertEqual(
                    [shape['text'] for shape in document['shapes'] if shape['type'] == 'text'],
                    [control, '中文'],
                )
                display = measure_display_bounds(document)
                self.assertEqual(len(display['text_bounds_pt']), 2)
                self.assertIsNone(display['text_bounds_pt'][0])
                self.assertGreater(display['text_bounds_pt'][1][2], 10)
                pdf, layout = render_pdf(document, {
                    'paper_size': 'A4', 'margin_mm': 10,
                    'show_page_number': False,
                })
                self.assertEqual(layout['page_count'], 1)
                with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
                    self.assertIn('中文', rendered[0].get_text())

    def test_non_inking_only_lb_keeps_existing_default_page(self):
        document = parse_plt(b'IN;PU0,0;LB\t\x03')
        display = measure_display_bounds(document)
        self.assertEqual(display['text_bounds_pt'], [None])
        pdf, layout = render_pdf(document, {
            'paper_size': 'A4', 'margin_mm': 10,
        })
        self.assertEqual(layout['page_count'], 1)
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            self.assertEqual(rendered.page_count, 1)

    def test_text_only_single_page_zero_margin_has_visible_ink(self):
        source = b'IN;PU123,456;LBsample\x03;'
        pdf, layout = render_pdf(parse_plt(source), {
            'single_page_output': True, 'margin_mm': 0,
            'show_page_number': False,
        })
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            page = rendered[0]
            self.assertIn('sample', page.get_text())
            ink = page.get_bboxlog()[0][1]
            self.assertGreaterEqual(ink[0], -0.01)
            self.assertGreaterEqual(ink[1], -0.01)
            self.assertLessEqual(ink[2], page.rect.width + 0.01)
            self.assertLessEqual(ink[3], page.rect.height + 0.01)
        self.assertGreater(layout['page_width_pt'], 10)
        self.assertGreater(layout['page_height_pt'], 8)
        self.assertEqual(layout['drawing_width_mm'], 0)
        self.assertEqual(layout['drawing_height_mm'], 0)

    def test_long_text_pages_beyond_its_anchor(self):
        label = '中sample文' * 70
        source = ('IN;PU0,0;LB' + label + '\x03;').encode('utf-8')
        pdf, layout = render_pdf(parse_plt(source), {
            'paper_size': 'A4', 'margin_mm': 10,
            'show_page_number': False,
        })
        self.assertGreater(layout['page_count'], 1)
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            self.assertEqual(rendered.page_count, layout['page_count'])
            self.assertTrue(any(
                any(kind == 'fill-text' for kind, _box in page.get_bboxlog())
                for page in list(rendered)[1:]
            ))

    def test_english_and_chinese_text_ink_fits_single_page_without_margin(self):
        for label in ('sample', '中文纸样'):
            with self.subTest(label=label):
                source = ('IN;PU0,0;LB' + label + '\x03;').encode('utf-8')
                pdf, _layout = render_pdf(parse_plt(source), {
                    'single_page_output': True, 'margin_mm': 0,
                    'show_page_number': False,
                })
                with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
                    page = rendered[0]
                    self.assertIn(label, page.get_text())
                    ink = next(box for kind, box in page.get_bboxlog() if kind == 'fill-text')
                    self.assertGreaterEqual(ink[0], -0.01)
                    self.assertGreaterEqual(ink[1], -0.01)
                    self.assertLessEqual(ink[2], page.rect.width + 0.01)
                    self.assertLessEqual(ink[3], page.rect.height + 0.01)

    def test_text_display_does_not_replace_geometry_roundtrip_metadata(self):
        original = parse_plt(b'IN;PU0,0;PD1016,0;PU0,0;LBsample\x03;')
        pdf, _layout = render_pdf(original, {
            'paper_size': 'A4', 'margin_mm': 10,
            'show_page_number': False,
        })
        metadata = read_pdf_layout_metadata(pdf)
        self.assertAlmostEqual(metadata['drawing_width_mm'], 25.4, places=2)
        self.assertEqual(metadata['drawing_height_mm'], 0)
        crop = metadata['crop_margins_mm']
        roundtrip, result = convert_pdf_to_plt(pdf, {
            'metadata_mode': 'original',
            'rows': metadata['rows'], 'columns': metadata['columns'],
            'order': metadata['order'], 'page_slots': metadata['page_slots'],
            'crop_left_mm': crop['left'], 'crop_right_mm': crop['right'],
            'crop_top_mm': crop['top'], 'crop_bottom_mm': crop['bottom'],
        })
        restored = parse_plt(roundtrip)['metrics']
        self.assertTrue(result['embedded_layout_applied'])
        self.assertEqual(restored['path_count'], 1)
        self.assertAlmostEqual(restored['width_mm'], 25.4, places=2)
        self.assertEqual(restored['height_mm'], 0)

    def test_top_text_expansion_keeps_geometry_on_page_edge(self):
        source = b'IN;PU0,0;PD1016,0,1016,1016;PU0,1016;LBsample\x03;'
        pdf, layout = render_pdf(parse_plt(source), {
            'single_page_output': True, 'margin_mm': 0,
            'show_page_number': False,
        })
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            page = rendered[0]
            self.assertIn('sample', page.get_text())
            paths = [item for item in page.get_drawings() if item['color'] == (0, 0, 0)]
            self.assertTrue(paths)
            self.assertAlmostEqual(paths[0]['rect'].width, 72, places=2)
            self.assertAlmostEqual(paths[0]['rect'].height, 72, places=2)
            self.assertAlmostEqual(paths[0]['rect'].y1, page.rect.height, places=2)
        self.assertAlmostEqual(layout['drawing_width_mm'], 25.4, places=2)
        self.assertAlmostEqual(layout['drawing_height_mm'], 25.4, places=2)

    def test_single_page_disabled_tile_clears_only_selected_text_region(self):
        source = ('IN;PU0,0;LB' + 'sample' * 45 + '\x03;').encode('utf-8')
        document = parse_plt(source)
        options = {
            'paper_size': 'A4', 'margin_mm': 10,
            'single_page_output': True, 'show_page_number': False,
        }
        complete, complete_layout = render_pdf(document, options)
        removed, removed_layout = render_pdf(document, {**options, 'disabled_pages': [1]})
        self.assertEqual(complete_layout['tiled_columns'], 5)
        self.assertEqual(removed_layout['selected_tile_count'], 4)

        def dark_pixels(pdf):
            with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
                tile = rendered[0].get_pixmap(
                    clip=pymupdf.Rect(650, 31, 1000, 38), alpha=False,
                )
                return sum(
                    max(tile.samples[index:index + 3]) < 150
                    for index in range(0, len(tile.samples), 3)
                )

        self.assertGreater(dark_pixels(complete), 0)
        self.assertEqual(dark_pixels(removed), 0)

    def test_roundtrip_metadata_rejects_infinite_grid_without_uncaught_error(self):
        metadata = {
            'version': 1,
            'rows': float('inf'),
            'columns': 1,
            'order': 'row',
            'page_slots': [0],
            'crop_margins_mm': {'top': 0, 'right': 0, 'bottom': 0, 'left': 0},
            'drawing_width_mm': 100,
            'drawing_height_mm': 100,
            'complete_layout': True,
        }
        with self.assertRaisesRegex(ValueError, 'rows'):
            normalize_pdf_layout_metadata(metadata)
        payload = base64.urlsafe_b64encode(json.dumps(metadata).encode('ascii')).decode('ascii').rstrip('=')
        self.assertIsNone(decode_pdf_layout_metadata(ROUNDTRIP_METADATA_PREFIX + payload))

    def test_roundtrip_metadata_requires_boolean_complete_flag(self):
        metadata = {
            'version': 1,
            'rows': 1,
            'columns': 1,
            'order': 'row',
            'page_slots': [0],
            'crop_margins_mm': {'top': 0, 'right': 0, 'bottom': 0, 'left': 0},
            'drawing_width_mm': 100,
            'drawing_height_mm': 100,
            'complete_layout': 'false',
        }

        with self.assertRaisesRegex(ValueError, '完整状态'):
            normalize_pdf_layout_metadata(metadata)

    def test_roundtrip_metadata_rejects_boolean_and_fractional_integers(self):
        metadata = {
            'version': 1,
            'rows': 1,
            'columns': 1,
            'order': 'row',
            'page_slots': [0],
            'crop_margins_mm': {'top': 0, 'right': 0, 'bottom': 0, 'left': 0},
            'drawing_width_mm': 100,
            'drawing_height_mm': 100,
            'complete_layout': True,
        }
        for field, value in (('version', True), ('rows', 1.9), ('page_slots', [False])):
            with self.subTest(field=field):
                malformed = {**metadata, field: value}
                with self.assertRaises(ValueError):
                    normalize_pdf_layout_metadata(malformed)

    def test_roundtrip_metadata_rejects_dimensions_above_plt_limit(self):
        metadata = {
            'version': 1,
            'rows': 1,
            'columns': 1,
            'order': 'row',
            'page_slots': [0],
            'crop_margins_mm': {'top': 0, 'right': 0, 'bottom': 0, 'left': 0},
            'drawing_width_mm': 10001,
            'drawing_height_mm': 100,
            'complete_layout': True,
        }

        with self.assertRaisesRegex(ValueError, 'drawing_width_mm'):
            normalize_pdf_layout_metadata(metadata)

    def test_roundtrip_metadata_accepts_zero_but_rejects_invalid_dimensions(self):
        metadata = {
            'version': 1, 'rows': 1, 'columns': 1, 'order': 'row',
            'page_slots': [0],
            'crop_margins_mm': {'top': 0, 'right': 0, 'bottom': 0, 'left': 0},
            'drawing_width_mm': 0, 'drawing_height_mm': 0,
            'complete_layout': True,
        }
        normalized = normalize_pdf_layout_metadata(metadata)
        self.assertEqual(normalized['drawing_width_mm'], 0.0)
        self.assertEqual(normalized['drawing_height_mm'], 0.0)
        for invalid in (-1, float('nan'), float('inf'), 10001):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, 'drawing_width_mm'):
                    normalize_pdf_layout_metadata({**metadata, 'drawing_width_mm': invalid})

    def test_zero_extent_plt_renders_a4_with_true_metadata(self):
        cases = (
            (b'IN;PU0,0;PD1016,0;', 25.4, 0),
            (b'IN;PU0,0;PD0,1016;', 0, 25.4),
            (b'IN;PU123,456;LBsample\x03', 0, 0),
            (b'IN;PU123,456;LBsample', 0, 0),
        )
        for source, width, height in cases:
            with self.subTest(source=source):
                pdf, layout = render_pdf(parse_plt(source), {
                    'paper_size': 'A4', 'orientation': 'portrait',
                    'margin_mm': 10, 'show_page_number': False,
                })
                metadata = read_pdf_layout_metadata(pdf)
                self.assertEqual(layout['page_count'], 1)
                self.assertEqual(metadata['drawing_width_mm'], width)
                self.assertEqual(metadata['drawing_height_mm'], height)
                with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
                    self.assertEqual(rendered.page_count, 1)
                    self.assertAlmostEqual(rendered[0].rect.width, 595.28, places=1)
                    self.assertAlmostEqual(rendered[0].rect.height, 841.89, places=1)

    def test_zero_height_line_roundtrip_excludes_page_guides(self):
        original = parse_plt(b'IN;PU0,0;PD1016,0;')
        pdf, _layout = render_pdf(original, {
            'paper_size': 'A4', 'orientation': 'portrait',
            'margin_mm': 10, 'show_page_number': False,
        })
        metadata = read_pdf_layout_metadata(pdf)
        crop = metadata['crop_margins_mm']
        roundtrip, result = convert_pdf_to_plt(pdf, {
            'metadata_mode': 'original',
            'rows': metadata['rows'], 'columns': metadata['columns'],
            'order': metadata['order'], 'page_slots': metadata['page_slots'],
            'crop_left_mm': crop['left'], 'crop_right_mm': crop['right'],
            'crop_top_mm': crop['top'], 'crop_bottom_mm': crop['bottom'],
        })
        restored = parse_plt(roundtrip)['metrics']
        self.assertTrue(result['embedded_layout_applied'])
        self.assertEqual(restored['path_count'], 1)
        self.assertAlmostEqual(restored['width_mm'], 25.4, places=2)
        self.assertAlmostEqual(restored['height_mm'], 0, places=2)

    @staticmethod
    def decoded_streams(pdf):
        streams = re.findall(
            rb'<< /Length \d+ /Filter /FlateDecode >>\nstream\n(.*?)\nendstream',
            pdf,
            flags=re.DOTALL,
        )
        return b'\n'.join(zlib.decompress(stream) for stream in streams)

    @staticmethod
    def rendered_samples(pdf, scale=0.2):
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            pixmap = rendered[0].get_pixmap(
                matrix=pymupdf.Matrix(scale, scale),
                alpha=False,
            )
            return bytes(pixmap.samples)

    def test_renders_tiled_pdf_and_selected_pages(self):
        document = parse_plt(
            b'IN;PU0,0;PD1016,0,1016,2032,0,2032,0,0;'
        )
        pdf, layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'enabled_pages': [0],
        })

        self.assertEqual(layout['page_count'], 1)
        self.assertEqual(pdf[:8], b'%PDF-1.4')
        self.assertIn(b'/Type /Catalog', pdf)
        self.assertIn(b'/MediaBox', pdf)
        self.assertIn(b'/Filter /FlateDecode', pdf)
        content = self.decoded_streams(pdf)
        self.assertIn(b'<312D31> Tj', content)
        self.assertIn(b'<41342031303025207C20312D31207C20312F31207C20706C742D67756964652D7631> Tj', content)
        self.assertIn(b'/F2 48.000 Tf', content)
        self.assertIn(b'1 0 0 RG', content)
        self.assertIn(b'<3520636D202F203530206D6D> Tj', content)
        self.assertNotIn(b'(1-1) Tj', content)
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            self.assertEqual(rendered.page_count, 1)
            self.assertAlmostEqual(rendered[0].rect.width, layout['page_width_pt'], places=2)
            self.assertAlmostEqual(rendered[0].rect.height, layout['page_height_pt'], places=2)

    def test_embeds_roundtrip_layout_metadata_and_uses_portable_pdf_font(self):
        document = parse_plt(
            b'IN;PU0,0;PD26526,0,26526,60326,0,60326,0,0;'
        )

        pdf, layout = render_pdf(document, {
            'paper_size': 'A3',
            'orientation': 'portrait',
            'margin_mm': 10,
            'show_page_number': True,
        })

        metadata = read_pdf_layout_metadata(pdf)
        self.assertEqual(metadata['rows'], 4)
        self.assertEqual(metadata['columns'], 3)
        self.assertEqual(metadata['order'], 'row')
        self.assertEqual(metadata['crop_margins_mm'], {
            'top': 10.0,
            'right': 10.0,
            'bottom': 10.0,
            'left': 10.0,
        })
        self.assertEqual(metadata['page_slots'], list(range(12)))
        self.assertTrue(metadata['complete_layout'])
        self.assertAlmostEqual(metadata['drawing_width_mm'], 663.15, places=2)
        self.assertAlmostEqual(metadata['drawing_height_mm'], 1508.15, places=2)
        self.assertIn(b'/BaseFont /Helvetica', pdf)
        self.assertNotIn(b'/BaseFont /STSong-Light', pdf)
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            self.assertIn('A3 100%', rendered[0].get_text())

    def test_generated_pdf_roundtrip_restores_original_bounds_without_guides(self):
        original = parse_plt(
            b'IN;PU0,0;PD26526,0,26526,60326,0,60326,0,0;'
        )
        pdf, _layout = render_pdf(original, {
            'paper_size': 'A3',
            'orientation': 'portrait',
            'margin_mm': 10,
            'show_page_number': True,
        })
        metadata = read_pdf_layout_metadata(pdf)

        roundtrip, result_layout = convert_pdf_to_plt(pdf, {
            'metadata_mode': 'original',
            'rows': metadata['rows'],
            'columns': metadata['columns'],
            'order': metadata['order'],
            'page_slots': metadata['page_slots'],
            'crop_left_mm': metadata['crop_margins_mm']['left'],
            'crop_right_mm': metadata['crop_margins_mm']['right'],
            'crop_top_mm': metadata['crop_margins_mm']['top'],
            'crop_bottom_mm': metadata['crop_margins_mm']['bottom'],
        })
        restored = parse_plt(roundtrip)

        self.assertTrue(result_layout['embedded_layout_applied'])
        self.assertAlmostEqual(
            restored['metrics']['width_mm'],
            original['metrics']['width_mm'],
            places=2,
        )
        self.assertAlmostEqual(
            restored['metrics']['height_mm'],
            original['metrics']['height_mm'],
            places=2,
        )
        self.assertLessEqual(restored['metrics']['path_count'], 12)

    def test_keeps_cid_font_when_plt_contains_text_shapes(self):
        document = parse_plt(b'IN;PU0,0;PD1016,1016;PU508,508;LBsample\x03;')

        pdf, _layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
        })

        self.assertIn(b'/BaseFont /STSong-Light', pdf)
        self.assertIn(b'/F1 ', self.decoded_streams(pdf))
        self.assertIn(b'/FontDescriptor', pdf)
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            self.assertIn('sample', rendered[0].get_text())

    def test_rejects_page_count_before_rendering(self):
        document = parse_plt(b'IN;PU0,0;PD50000,50000;')
        with patch.dict('os.environ', {'PLT_MAX_OUTPUT_PAGES': '1'}):
            with self.assertRaisesRegex(ValueError, '输出页数'):
                render_pdf(document, {
                    'paper_size': 'A4',
                    'orientation': 'portrait',
                    'margin_mm': 10,
                })

    def test_rejects_when_all_pages_are_disabled(self):
        document = parse_plt(b'IN;PU0,0;PD1016,1016;')
        with self.assertRaisesRegex(ValueError, '至少保留一个输出页面'):
            render_pdf(document, {
                'paper_size': 'A4',
                'orientation': 'portrait',
                'margin_mm': 10,
                'enabled_pages': [],
            })

    def test_single_page_output_rejects_disabled_page(self):
        document = parse_plt(b'IN;PU0,0;PD1016,1016;')
        with self.assertRaisesRegex(ValueError, '至少保留一个输出页面'):
            render_pdf(document, {
                'single_page_output': True,
                'enabled_pages': [],
            })

    def test_single_page_legacy_enabled_pages_keep_full_drawing(self):
        document = parse_plt(
            b'IN;PU0,0;PD20000,0,20000,5000,0,5000,0,0;'
        )
        complete, complete_layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'single_page_output': True,
        })
        legacy_client, legacy_layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'single_page_output': True,
            'enabled_pages': [0],
        })

        self.assertEqual(
            self.rendered_samples(complete),
            self.rendered_samples(legacy_client),
        )
        self.assertEqual(complete_layout['selected_tile_count'], 3)
        self.assertEqual(legacy_layout['selected_tile_count'], 3)

    def test_single_page_output_honors_disabled_tiled_regions(self):
        document = parse_plt(
            b'IN;PU0,0;PD20000,0,20000,5000,0,5000,0,0;'
        )
        first_removed, first_layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'single_page_output': True,
            'disabled_pages': [0],
        })
        second_removed, second_layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'single_page_output': True,
            'disabled_pages': [1],
        })

        self.assertNotEqual(
            self.rendered_samples(first_removed),
            self.rendered_samples(second_removed),
        )
        self.assertEqual(first_layout['selected_tile_count'], 2)
        self.assertEqual(second_layout['selected_tile_count'], 2)
        self.assertEqual(self.decoded_streams(first_removed).count(b'W*'), 2)

    def test_single_page_disabled_pages_preserve_unpreviewed_regions(self):
        document = parse_plt(
            b'IN;PU0,0;PD76000,0,76000,99720,0,99720,0,0;'
        )
        complete, complete_layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'single_page_output': True,
            'enabled_pages': list(range(80)),
            'disabled_pages': [],
        })
        with_one_removed, removed_layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'single_page_output': True,
            'enabled_pages': list(range(80)),
            'disabled_pages': [1],
        })

        self.assertEqual(complete_layout['tiled_columns'], 10)
        self.assertEqual(complete_layout['tiled_rows'], 9)
        self.assertEqual(complete_layout['selected_tile_count'], 90)
        self.assertEqual(removed_layout['selected_tile_count'], 89)
        self.assertNotEqual(
            self.rendered_samples(complete, scale=0.05),
            self.rendered_samples(with_one_removed, scale=0.05),
        )

    def test_single_page_rejects_when_disabled_pages_cover_every_region(self):
        document = parse_plt(b'IN;PU0,0;PD1016,1016;')
        with self.assertRaisesRegex(ValueError, '至少保留一个输出页面'):
            render_pdf(document, {
                'single_page_output': True,
                'enabled_pages': [0],
                'disabled_pages': [0],
            })

    def test_clips_segments_to_page_bounds(self):
        self.assertIsNone(clip_segment((-10, -10), (-1, -1), 0, 0, 100, 100))
        self.assertEqual(
            clip_segment((-10, 50), (110, 50), 0, 0, 100, 100),
            ((0, 50), (100, 50)),
        )


if __name__ == '__main__':
    unittest.main()
