import unittest
from unittest.mock import patch

from app.routes import parse_pdf_render_options
from app.services.pdf_to_pdf import source_conversion_options
from app.services.pdf_to_plt import convert_pdf_to_plt, load_fitz, transparent_preview


class PdfOuterEdgesTest(unittest.TestCase):
    def source(self, sizes=((100, 120),) * 4, red_guide=False):
        fitz = load_fitz()
        document = fitz.open()
        try:
            for width, height in sizes:
                page = document.new_page(width=width * 72 / 25.4, height=height * 72 / 25.4)
                page.draw_rect(fitz.Rect(1 * 72 / 25.4, 1 * 72 / 25.4,
                                         (width - 1) * 72 / 25.4, (height - 1) * 72 / 25.4))
                page.draw_line(fitz.Point(0, 60 * 72 / 25.4), fitz.Point(width * 72 / 25.4, 60 * 72 / 25.4))
                if red_guide:
                    page.draw_line(fitz.Point(3 * 72 / 25.4, 0),
                                   fitz.Point(3 * 72 / 25.4, height * 72 / 25.4), color=(1, 0, 0))
            return document.tobytes()
        finally:
            document.close()

    def options(self, **changes):
        return {
            'units_per_inch': 25400, 'rows': 2, 'columns': 2,
            'page_slots': [0, 1, 2, 3],
            'crop_left_mm': 3, 'crop_right_mm': 7,
            'crop_top_mm': 5, 'crop_bottom_mm': 11,
            'preserve_outer_edges': True, **changes,
        }

    def geometry(self, source, options):
        captured = []
        with patch('app.services.pdf_to_plt.serialize_hpgl', side_effect=lambda paths, *_args: captured.extend(paths) or b'IN;'):
            _plt, result = convert_pdf_to_plt(source, options)
        return captured, result

    def test_invisible_fill_background_does_not_become_a_cutting_outline(self):
        fitz = load_fitz()
        for fill, opacity, stroke, invisible in (
            ((.999, .999, .999), 1, None, False),
            ((1, 1, 1), 1, None, True),
            ((0, 0, 0), 0, None, True),
            ((.99, .99, .99), 1, None, False),
            ((1, .99, 1), 1, None, False),
            ((.999, .999, .999), 1, (0, 0, 0), False),
        ):
            with self.subTest(fill=fill, opacity=opacity, stroke=stroke):
                document = fitz.open()
                page = document.new_page(width=200, height=200)
                page.draw_rect(page.rect, color=stroke, fill=fill, fill_opacity=opacity)
                page.draw_line((30, 90), (170, 110), color=(.999, .999, .999))
                pixmap = page.get_pixmap(alpha=False)
                # Confirm what the actual preview renderer sees in the background.
                self.assertEqual(pixmap.pixel(50, 50) == (255, 255, 255), invisible)
                preview = transparent_preview(pixmap, fitz)
                self.assertEqual(preview.pixel(50, 50)[3] == 0, invisible)
                if fill == (.999, .999, .999) and opacity == 1:
                    self.assertEqual(preview.pixel(50, 50)[3], 1)
                source = document.tobytes()
                document.close()
                paths, result = self.geometry(source, {
                    'rows': 1, 'columns': 1, 'preserve_outer_edges': True,
                    'units_per_inch': 25400,
                })
                # The pale stroked line stays; a visible fill or stroke keeps its rectangle.
                self.assertEqual(len(paths), 1 if invisible else 2)
                self.assertEqual((result['width_mm'], result['height_mm']), (70.56, 70.56))

    def test_two_by_two_keeps_outer_content_without_moving_internal_seams(self):
        paths, result = self.geometry(self.source(), self.options())
        self.assertEqual(result['width_mm'], 190)
        self.assertEqual(result['height_mm'], 224)
        horizontal = [path for path in paths if len(path) == 2 and abs(path[0]['y'] - path[1]['y']) < 1]
        endpoints = {(round(p[0]['x'] / 1000), round(p[-1]['x'] / 1000)) for p in horizontal}
        self.assertIn((0, 93), endpoints)
        self.assertIn((93, 190), endpoints)
        # Page borders at 1mm from the exposed source edges remain visible.
        self.assertTrue(any(abs(point['x'] / 1000 - 1) < .002 for path in paths for point in path))

    def test_strict_boolean_is_shared_by_options_and_http_parser(self):
        self.assertIs(parse_pdf_render_options({'preserve_outer_edges': 'false'})['preserve_outer_edges'], False)
        self.assertIs(parse_pdf_render_options({'preserve_outer_edges': 'true'})['preserve_outer_edges'], True)
        for value in ('yes', '1', '', 1, None, 'invalid'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_pdf_render_options({'preserve_outer_edges': value})
                with self.assertRaises(ValueError):
                    convert_pdf_to_plt(self.source(sizes=((100, 120),)),
                                       self.options(rows=1, columns=1, page_slots=[0], preserve_outer_edges=value))

    def test_holes_expose_edges_and_deleted_pages_are_not_reextracted(self):
        from app.services.pdf_to_plt import _extract_page
        with patch('app.services.pdf_to_plt._extract_page', wraps=_extract_page) as extract:
            _plt, result = convert_pdf_to_plt(self.source(), self.options(page_slots=[0, 1, None, 2]))
        self.assertEqual(extract.call_count, 3)
        self.assertEqual(result['selected_count'], 3)
        self.assertEqual(result['empty_cells'], 1)
        # left/top/right/bottom clipping, measured in original page coordinates.
        expected = ((0, 0, 93, 120), (3, 0, 100, 109), (0, 5, 100, 120))
        for call, wanted in zip(extract.call_args_list, expected):
            crop = call.args[4]
            for actual, target in zip((crop.x0, crop.y0, crop.x1, crop.y1), wanted):
                self.assertAlmostEqual(actual * 25.4 / 72, target, delta=.00002)
            guide = call.kwargs['guide_crop_rect']
            self.assertAlmostEqual(guide.x0 * 25.4 / 72, 3, delta=.00002)
            self.assertAlmostEqual(guide.y0 * 25.4 / 72, 5, delta=.00002)

    def test_single_page_restores_closed_border_and_complete_dimensions(self):
        paths, result = self.geometry(self.source(sizes=((100, 120),)),
                                      self.options(rows=1, columns=1, page_slots=[0]))
        self.assertEqual((result['width_mm'], result['height_mm']), (100, 120))
        closed = [path for path in paths if path[0] == path[-1] and len(path) >= 5]
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(min(p['y'] for p in closed[0]) / 1000, 1, delta=.002)
        self.assertAlmostEqual(max(p['y'] for p in closed[0]) / 1000, 119, delta=.002)

    def test_mixed_sizes_use_base_tile_and_keep_gap_and_rotation(self):
        source = self.source(sizes=((100, 120), (120, 150)))
        options = self.options(rows=1, columns=2, page_slots=[0, 1],
                               crop_left_mm=10, crop_right_mm=10,
                               crop_top_mm=10, crop_bottom_mm=10, margin_mm=5)
        _paths, result = self.geometry(source, options)
        self.assertEqual((result['width_mm'], result['height_mm']), (225, 150))
        for rotation, wanted in ((90, (150, 225)), (180, (225, 150)), (270, (150, 225))):
            with self.subTest(rotation=rotation):
                paths, rotated = self.geometry(source, {**options, 'output_rotation': rotation})
                self.assertEqual((rotated['width_mm'], rotated['height_mm']), wanted)
                self.assertTrue(all(p['x'] >= 0 and p['y'] >= 0 for path in paths for p in path))

    def test_legacy_omitted_false_and_string_false_keep_identical_plt_and_dimensions(self):
        source = self.source()
        options = self.options(preserve_outer_edges=False)
        omitted = dict(options)
        del omitted['preserve_outer_edges']
        default = convert_pdf_to_plt(source, omitted)
        self.assertEqual(convert_pdf_to_plt(source, options), default)
        self.assertEqual(convert_pdf_to_plt(source, {**options, 'preserve_outer_edges': 'false'}), default)
        self.assertEqual((default[1]['width_mm'], default[1]['height_mm']), (180, 208))

    def test_expansion_does_not_revive_red_crop_guide(self):
        paths, _result = self.geometry(self.source(sizes=((100, 120),), red_guide=True),
                                       self.options(rows=1, columns=1, page_slots=[0]))
        self.assertFalse(any(len(path) == 2 and all(abs(p['x'] / 1000 - 3) < .002 for p in path)
                             for path in paths))

    def test_current_content_with_preservation_keeps_red_boundary_lines(self):
        paths, _result = self.geometry(self.source(sizes=((100, 120),), red_guide=True),
                                       self.options(rows=1, columns=1, page_slots=[0], metadata_mode='current'))
        self.assertTrue(any(len(path) == 2 and all(abs(p['x'] / 1000 - 3) < .002 for p in path)
                            for path in paths))

    def test_explicit_preservation_bypasses_original_metadata_normalization(self):
        metadata = {'complete_layout': True}
        with patch('app.services.pdf_to_plt.read_pdf_layout_metadata_from_document', return_value=metadata), \
                patch('app.services.pdf_to_plt.embedded_layout_matches_options', return_value=True), \
                patch('app.services.pdf_to_plt.normalize_generated_pdf_paths', side_effect=AssertionError('must preserve')):
            _paths, result = self.geometry(self.source(sizes=((100, 120),)),
                                          self.options(rows=1, columns=1, page_slots=[0], metadata_mode='original'))
        self.assertFalse(result['embedded_layout_applied'])
        self.assertEqual((result['width_mm'], result['height_mm']), (100, 120))

    def test_pdf_repage_forwards_preservation_without_truthy_string_coercion(self):
        for value in (True, False, 'false'):
            with self.subTest(value=value):
                options = source_conversion_options({'rows': 1, 'columns': 1,
                                                       'page_slots': [0], 'preserve_outer_edges': value})
                self.assertEqual(options['preserve_outer_edges'], value)
        self.assertIs(source_conversion_options({'rows': 1, 'columns': 1})['preserve_outer_edges'], False)

    def clipped_source(self, rotation=0):
        fitz = load_fitz()
        document = fitz.open()
        try:
            page = document.new_page(width=100, height=100)
            stream = (b'q 10 10 80 80 re W n '
                      b'0 20 m 100 20 l S '
                      b'0 5 m 100 5 l S '
                      b'q 20 20 60 60 re W n 0 30 m 100 30 l S Q '
                      b'0 40 m 100 40 l S Q '
                      b'0 50 m 100 50 l S')
            xref = document.get_new_xref()
            document.update_object(xref, '<<>>')
            document.update_stream(xref, stream)
            page.set_contents(xref)
            page.set_rotation(rotation)
            return document.tobytes()
        finally:
            document.close()

    def test_native_rectangular_clips_nested_scope_and_scope_exit_remain_visible_only(self):
        paths, _result = self.geometry(self.clipped_source(), self.options(
            units_per_inch=72, rows=1, columns=1, page_slots=[0],
            crop_left_mm=1, crop_right_mm=1, crop_top_mm=1, crop_bottom_mm=1))
        segments = {(p[0]['x'], p[0]['y'], p[-1]['x'], p[-1]['y']) for p in paths}
        self.assertEqual(segments, {(10, 20, 90, 20), (20, 30, 80, 30),
                                    (10, 40, 90, 40), (0, 50, 100, 50)})

    def test_native_rectangular_clip_rotates_with_pdf_page(self):
        paths, _result = self.geometry(self.clipped_source(rotation=90), self.options(
            units_per_inch=72, rows=1, columns=1, page_slots=[0],
            crop_left_mm=1, crop_right_mm=1, crop_top_mm=1, crop_bottom_mm=1))
        segments = {(p[0]['x'], p[0]['y'], p[-1]['x'], p[-1]['y']) for p in paths}
        self.assertEqual(segments, {(20, 90, 20, 10), (30, 80, 30, 20),
                                    (40, 90, 40, 10), (50, 100, 50, 0)})

    def test_native_clip_legacy_get_drawings_fallback_keeps_unclipped_page_behavior(self):
        from app.services.pdf_to_plt import _extract_page
        fitz = load_fitz()
        document = fitz.open(stream=self.source(sizes=((100, 120),)), filetype='pdf')
        try:
            page = document[0]
            class LegacyPage:
                rect = page.rect
                rotation_matrix = page.rotation_matrix
                def get_drawings(self):
                    return page.get_drawings()
            old = _extract_page(LegacyPage(), 72, fitz, 10000, page.rect)
            compatible = _extract_page(LegacyPage(), 72, fitz, 10000, page.rect,
                                       respect_native_clips=True)
            self.assertEqual(compatible, old)
        finally:
            document.close()

    def test_clip_only_records_still_use_existing_raster_fallback(self):
        from app.services.pdf_to_plt import _extract_page
        fitz = load_fitz()
        class ClipOnlyPage:
            rect = fitz.Rect(0, 0, 100, 100)
            def get_drawings(self, extended=False):
                return [{'type': 'clip', 'level': 0, 'scissor': fitz.Rect(10, 10, 90, 90)}]
        with patch('app.services.pdf_to_plt.rasterize_page', return_value=[[{'x': 1, 'y': 1}, {'x': 2, 'y': 2}]]) as raster:
            extracted = _extract_page(ClipOnlyPage(), 72, fitz, 100, respect_native_clips=True)
        self.assertEqual(extracted['source_type'], 'raster')
        raster.assert_called_once()

    def test_repeated_exact_one_mm_base_crop_preserves_source_dimensions(self):
        fitz = load_fitz()
        source = self.source(sizes=((100.004, 150.004),))
        document = fitz.open(stream=source, filetype='pdf')
        try:
            width_mm = document[0].rect.width * 25.4 / 72
            height_mm = document[0].rect.height * 25.4 / 72
        finally:
            document.close()
        options = self.options(rows=1, columns=1, page_slots=[0],
                               crop_left_mm=50, crop_right_mm=width_mm - 51,
                               crop_top_mm=50, crop_bottom_mm=height_mm - 51)
        first = convert_pdf_to_plt(source, options)
        for _ in range(3):
            self.assertEqual(convert_pdf_to_plt(source, options), first)
        self.assertEqual((first[1]['width_mm'], first[1]['height_mm']), (100, 150))

    def test_white_fill_only_background_does_not_become_black_outer_frame(self):
        fitz = load_fitz()
        document = fitz.open()
        try:
            page = document.new_page(width=100, height=100)
            page.draw_rect(fitz.Rect(0, 0, 100, 100), color=None,
                           fill=(1, 1, 1), fill_opacity=1)
            page.draw_rect(fitz.Rect(5, 5, 95, 95), color=(0, 0, 0))
            source = document.tobytes()
        finally:
            document.close()
        paths, _result = self.geometry(source, self.options(
            units_per_inch=72, rows=1, columns=1, page_slots=[0],
            crop_left_mm=3, crop_right_mm=3, crop_top_mm=3, crop_bottom_mm=3))
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0][0], paths[0][-1])
        self.assertEqual({(p['x'], p['y']) for p in paths[0]},
                         {(5, 5), (95, 5), (95, 95), (5, 95)})

    def test_white_fill_skip_does_not_change_legacy_nonwhite_fill_or_black_stroke(self):
        from app.services.pdf_to_plt import _extract_page
        fitz = load_fitz()
        document = fitz.open()
        try:
            page = document.new_page(width=100, height=100)
            page.draw_rect(fitz.Rect(0, 0, 100, 100), color=None, fill=(1, 1, 1))
            page.draw_rect(fitz.Rect(5, 5, 95, 95), color=None, fill=(.5, .5, .5))
            page.draw_rect(fitz.Rect(10, 10, 90, 90), color=(0, 0, 0), fill=(1, 1, 1))
            legacy = _extract_page(page, 72, fitz, 1000, page.rect)
            preserved = _extract_page(page, 72, fitz, 1000, page.rect, respect_native_clips=True)
            self.assertEqual(len(legacy['shapes']), 3)
            self.assertEqual(len(preserved['shapes']), 2)
            self.assertEqual(preserved['shapes'], legacy['shapes'][1:])
        finally:
            document.close()
