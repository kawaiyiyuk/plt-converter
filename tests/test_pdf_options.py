import unittest
from unittest.mock import patch

from app.services.pdf_to_plt import convert_pdf_to_plt, serialize_hpgl


class FakePage:
    def __init__(self):
        self.rect = type('Rect', (), {'width': 100, 'height': 100})()

    def get_drawings(self):
        return []


class FakeDocument:
    page_count = 2

    def load_page(self, _index):
        return FakePage()

    def close(self):
        return None


def extracted_page():
    return {
        'width_units': 100,
        'height_units': 100,
        'shapes': [[{'x': 0, 'y': 0}, {'x': 10, 'y': 10}]],
        'source_type': 'vector',
    }


def extracted_landscape_page():
    return {
        'width_units': 100,
        'height_units': 200,
        'shapes': [[{'x': 0, 'y': 0}, {'x': 10, 'y': 20}]],
        'source_type': 'vector',
    }


class PdfOptionsTest(unittest.TestCase):
    def convert(self, options):
        with patch('app.services.pdf_to_plt.open_pdf_document', return_value=FakeDocument()), \
                patch('app.services.pdf_to_plt.load_fitz', return_value=object()), \
                patch('app.services.pdf_to_plt.build_crop_rect', return_value=None), \
                patch('app.services.pdf_to_plt._extract_page', side_effect=lambda *args, **kwargs: extracted_page()):
            return convert_pdf_to_plt(b'%PDF', options)

    def test_rejects_page_slots_beyond_grid_capacity(self):
        with self.assertRaisesRegex(ValueError, 'page_slots 数量'):
            self.convert({'rows': 1, 'columns': 1, 'page_slots': [0, 1]})

    def test_rejects_out_of_range_page_slot(self):
        with self.assertRaisesRegex(ValueError, '无效页面'):
            self.convert({'rows': 1, 'columns': 1, 'page_slots': [2]})

    def test_rejects_duplicate_page_slots(self):
        with self.assertRaisesRegex(ValueError, '不能重复'):
            self.convert({'rows': 1, 'columns': 2, 'page_slots': [0, 0]})

    def test_rejects_empty_page_selection(self):
        with self.assertRaisesRegex(ValueError, '至少保留一个 PDF 页面'):
            self.convert({'rows': 1, 'columns': 2, 'enabled_pages': []})

    def test_rejects_enabled_pages_beyond_capacity(self):
        with self.assertRaisesRegex(ValueError, '不能超过行列总格数'):
            self.convert({'rows': 1, 'columns': 1, 'enabled_pages': [0, 1]})

    def test_rotates_complete_pdf_layout_clockwise_after_assembly(self):
        with patch('app.services.pdf_to_plt.open_pdf_document', return_value=FakeDocument()), \
                patch('app.services.pdf_to_plt.load_fitz', return_value=object()), \
                patch('app.services.pdf_to_plt.build_crop_rect', return_value=None), \
                patch('app.services.pdf_to_plt._extract_page', side_effect=lambda *args, **kwargs: extracted_landscape_page()):
            plt, layout = convert_pdf_to_plt(b'%PDF', {
                'rows': 1,
                'columns': 1,
                'enabled_pages': [0],
                'output_rotation': 90,
            })

        self.assertIn(b'PU0,100;PD0,100,20,90;', plt)
        self.assertEqual(layout['output_rotation'], 90)
        self.assertEqual(layout['width_mm'], 5.0)
        self.assertEqual(layout['height_mm'], 2.5)

    def test_rejects_generated_plt_with_more_paths_than_parser_accepts(self):
        too_many_paths = [
            [{'x': index, 'y': 0}, {'x': index, 'y': 10}]
            for index in range(101)
        ]
        page = extracted_page()
        page['shapes'] = too_many_paths

        with patch.dict('os.environ', {'PLT_MAX_PATHS': '100'}), \
                patch('app.services.pdf_to_plt.open_pdf_document', return_value=FakeDocument()), \
                patch('app.services.pdf_to_plt.load_fitz', return_value=object()), \
                patch('app.services.pdf_to_plt.build_crop_rect', return_value=None), \
                patch('app.services.pdf_to_plt._extract_page', return_value=page):
            with self.assertRaisesRegex(ValueError, 'PLT 路径过多'):
                convert_pdf_to_plt(b'%PDF', {
                    'rows': 1,
                    'columns': 1,
                    'enabled_pages': [0],
                })

    def test_rejects_generated_plt_above_existing_dimension_limit(self):
        page = extracted_page()
        page['width_units'] = 5000
        page['shapes'] = [[{'x': 0, 'y': 0}, {'x': 5000, 'y': 10}]]

        with patch.dict('os.environ', {'PLT_MAX_DIMENSION_MM': '100'}), \
                patch('app.services.pdf_to_plt.open_pdf_document', return_value=FakeDocument()), \
                patch('app.services.pdf_to_plt.load_fitz', return_value=object()), \
                patch('app.services.pdf_to_plt.build_crop_rect', return_value=None), \
                patch('app.services.pdf_to_plt._extract_page', return_value=page):
            with self.assertRaisesRegex(ValueError, 'PLT 尺寸过大'):
                convert_pdf_to_plt(b'%PDF', {
                    'rows': 1,
                    'columns': 1,
                    'enabled_pages': [0],
                })

    def test_hpgl_serializer_does_not_emit_redundant_pen_up_per_path(self):
        shapes = [
            [{'x': 0, 'y': 0}, {'x': 10, 'y': 10}],
            [{'x': 20, 'y': 20}, {'x': 30, 'y': 30}],
        ]

        result = serialize_hpgl(shapes, units_per_inch=1016, line_width_mm=0.265)

        self.assertEqual(result.count(b'PU;'), 1)
        self.assertIn(b'PD0,0,10,10;PU20,20;PD20,20,30,30;PU;SP0;', result)


if __name__ == '__main__':
    unittest.main()
