import unittest
from unittest.mock import patch

from app.services.pdf_to_plt import convert_pdf_to_plt


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


class PdfOptionsTest(unittest.TestCase):
    def convert(self, options):
        with patch('app.services.pdf_to_plt.open_pdf_document', return_value=FakeDocument()), \
                patch('app.services.pdf_to_plt.load_fitz', return_value=object()), \
                patch('app.services.pdf_to_plt._extract_page', side_effect=lambda *args: extracted_page()):
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


if __name__ == '__main__':
    unittest.main()
