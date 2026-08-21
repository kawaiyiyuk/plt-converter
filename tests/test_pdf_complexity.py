import os
import unittest
from unittest.mock import patch

import pymupdf

from app.services.pdf_to_plt import (
    _extract_page,
    build_crop_rect,
    clip_page_polyline,
    convert_pdf_to_plt,
    validate_pdf_complexity,
)


class Rect:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class Page:
    def __init__(self, width, height, drawing_count):
        self.rect = Rect(width, height)
        self.drawing_count = drawing_count

    def get_drawings(self):
        return [{}] * self.drawing_count


class Document:
    def __init__(self, pages):
        self.pages = pages
        self.page_count = len(pages)

    def load_page(self, index):
        return self.pages[index]


class PdfComplexityTest(unittest.TestCase):
    def test_rejects_total_pixels(self):
        document = Document([Page(1000, 1001, 0)])
        with patch.dict(os.environ, {'PDF_MAX_TOTAL_PIXELS': '1000000'}):
            with self.assertRaisesRegex(ValueError, '总像素量'):
                validate_pdf_complexity(document)

    def test_rejects_vector_drawings(self):
        document = Document([Page(10, 10, 1001)])
        with patch.dict(os.environ, {'PDF_MAX_DRAWINGS': '1000'}):
            with self.assertRaisesRegex(ValueError, '矢量路径数量'):
                validate_pdf_complexity(document)

    def test_clips_hidden_and_crossing_pdf_lines(self):
        hidden = [pymupdf.Point(-20, -20), pymupdf.Point(-10, -10)]
        crossing = [pymupdf.Point(-10, 50), pymupdf.Point(110, 50)]

        self.assertEqual(clip_page_polyline(hidden, 100, 100, pymupdf), [])
        visible = clip_page_polyline(crossing, 100, 100, pymupdf)
        self.assertEqual(len(visible), 1)
        self.assertAlmostEqual(visible[0][0].x, 0)
        self.assertAlmostEqual(visible[0][1].x, 100)

    def test_preserves_contiguous_visible_polyline(self):
        points = [
            pymupdf.Point(-10, 50),
            pymupdf.Point(50, 50),
            pymupdf.Point(110, 50),
        ]

        visible = clip_page_polyline(points, 100, 100, pymupdf)

        self.assertEqual(len(visible), 1)
        self.assertEqual(len(visible[0]), 3)
        self.assertAlmostEqual(visible[0][0].x, 0)
        self.assertAlmostEqual(visible[0][1].x, 50)
        self.assertAlmostEqual(visible[0][2].x, 100)

    def test_clips_polyline_to_non_zero_crop_rectangle(self):
        points = [pymupdf.Point(0, 50), pymupdf.Point(100, 50)]
        crop_rect = pymupdf.Rect(10, 20, 90, 80)

        visible = clip_page_polyline(points, 100, 100, pymupdf, crop_rect)

        self.assertEqual(len(visible), 1)
        self.assertAlmostEqual(visible[0][0].x, 10)
        self.assertAlmostEqual(visible[0][1].x, 90)

    def test_rejects_crop_that_leaves_less_than_one_millimeter(self):
        page = type('CropPage', (), {'rect': pymupdf.Rect(0, 0, 100, 100)})()
        margins = {'left': 20, 'right': 20, 'top': 0, 'bottom': 0}

        with self.assertRaisesRegex(ValueError, '至少保留 1mm'):
            build_crop_rect(page, margins, pymupdf, 1)

    def test_does_not_fallback_to_raster_when_crop_removes_all_vector_lines(self):
        class VectorPage:
            rect = pymupdf.Rect(0, 0, 100, 100)

            def get_drawings(self):
                return [{'items': [('l', pymupdf.Point(0, 5), pymupdf.Point(100, 5))]}]

        crop_rect = pymupdf.Rect(10, 20, 90, 80)
        with patch('app.services.pdf_to_plt.rasterize_page') as rasterize:
            result = _extract_page(VectorPage(), 1016, pymupdf, 100, crop_rect)

        self.assertEqual(result['source_type'], 'vector')
        self.assertEqual(result['shapes'], [])
        rasterize.assert_not_called()

    def test_crop_clips_and_moves_vector_coordinates_to_new_origin(self):
        class VectorPage:
            rect = pymupdf.Rect(0, 0, 100, 100)

            def get_drawings(self):
                return [{'items': [('l', pymupdf.Point(0, 50), pymupdf.Point(100, 50))]}]

        result = _extract_page(
            VectorPage(),
            72,
            pymupdf,
            100,
            pymupdf.Rect(10, 20, 90, 80),
        )

        self.assertEqual(result['source_type'], 'vector')
        self.assertAlmostEqual(result['width_units'], 80)
        self.assertAlmostEqual(result['height_units'], 60)
        self.assertAlmostEqual(result['shapes'][0][0]['x'], 0)
        self.assertAlmostEqual(result['shapes'][0][1]['x'], 80)
        self.assertAlmostEqual(result['shapes'][0][0]['y'], 30)

    def test_real_pdf_crop_updates_hpgl_origin_and_layout_size(self):
        document = pymupdf.open()
        page = document.new_page(width=100, height=100)
        page.draw_line(pymupdf.Point(0, 50), pymupdf.Point(100, 50))
        source = document.tobytes()
        document.close()
        crop_mm = 10 * 25.4 / 72

        plt, layout = convert_pdf_to_plt(source, {
            'units_per_inch': 72,
            'rows': 1,
            'columns': 1,
            'crop_left_mm': crop_mm,
            'crop_right_mm': crop_mm,
            'crop_top_mm': crop_mm,
            'crop_bottom_mm': crop_mm,
        })

        self.assertIn(b'PU0,40;PD0,40,80,40;', plt)
        self.assertAlmostEqual(layout['width_mm'], 80 * 25.4 / 72, places=2)
        self.assertAlmostEqual(layout['height_mm'], 80 * 25.4 / 72, places=2)

    def test_rotated_vector_pdf_uses_display_coordinates_before_crop(self):
        document = pymupdf.open()
        page = document.new_page(width=100, height=200)
        page.draw_line(pymupdf.Point(0, 100), pymupdf.Point(100, 100), width=2)
        page.set_rotation(90)
        source = document.tobytes()
        document.close()
        crop_mm = 10 * 25.4 / 72

        plt, layout = convert_pdf_to_plt(source, {
            'units_per_inch': 72,
            'rows': 1,
            'columns': 1,
            'crop_left_mm': crop_mm,
            'crop_right_mm': crop_mm,
            'crop_top_mm': crop_mm,
            'crop_bottom_mm': crop_mm,
        })

        self.assertIn(b'PU90,80;PD90,80,90,0;', plt)
        self.assertAlmostEqual(layout['width_mm'], 180 * 25.4 / 72, places=2)
        self.assertAlmostEqual(layout['height_mm'], 80 * 25.4 / 72, places=2)


if __name__ == '__main__':
    unittest.main()
