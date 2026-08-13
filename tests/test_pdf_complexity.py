import os
import unittest
from unittest.mock import patch

from app.services.pdf_to_plt import validate_pdf_complexity


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


if __name__ == '__main__':
    unittest.main()
