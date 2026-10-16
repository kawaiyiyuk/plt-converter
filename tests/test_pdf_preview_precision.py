import tempfile
import unittest

from app.services.pdf_to_plt import build_crop_rect, inspect_pdf, load_fitz


class PdfPreviewPrecisionTest(unittest.TestCase):
    def test_preview_retains_physical_page_dimensions_beyond_two_decimals(self):
        fitz = load_fitz()
        document = fitz.open()
        try:
            document.new_page(width=100.004 * 72 / 25.4, height=150.004 * 72 / 25.4)
            source = document.tobytes()
        finally:
            document.close()
        with tempfile.TemporaryDirectory() as preview_folder:
            pages = inspect_pdf(source, preview_folder, 'precision')
        self.assertEqual(len(pages), 1)
        self.assertAlmostEqual(pages[0]['width_mm'], 100.004, delta=0.00001)
        self.assertAlmostEqual(pages[0]['height_mm'], 150.004, delta=0.00001)

    def test_exact_one_mm_crop_survives_rect_float_quantization(self):
        fitz = load_fitz()
        document = fitz.open()
        try:
            page = document.new_page(width=100.004 * 72 / 25.4, height=150.004 * 72 / 25.4)
            width_mm = page.rect.width * 25.4 / 72
            height_mm = page.rect.height * 25.4 / 72
            for axis in ('width', 'height'):
                with self.subTest(axis=axis):
                    margins = {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
                    if axis == 'width':
                        margins.update(left=50, right=width_mm - 51)
                    else:
                        margins.update(top=50, bottom=height_mm - 51)
                    rect = build_crop_rect(page, margins, fitz)
                    extent = rect.width if axis == 'width' else rect.height
                    self.assertAlmostEqual(extent * 25.4 / 72, 1, delta=0.00001)
        finally:
            document.close()

    def test_less_than_one_mm_crop_still_rejects_each_axis(self):
        fitz = load_fitz()
        document = fitz.open()
        try:
            page = document.new_page(width=100.004 * 72 / 25.4, height=150.004 * 72 / 25.4)
            width_mm = page.rect.width * 25.4 / 72
            height_mm = page.rect.height * 25.4 / 72
            for axis in ('width', 'height'):
                with self.subTest(axis=axis):
                    margins = {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
                    if axis == 'width':
                        margins.update(left=50, right=width_mm - 50.999)
                    else:
                        margins.update(top=50, bottom=height_mm - 50.999)
                    with self.assertRaisesRegex(ValueError, '至少保留 1mm'):
                        build_crop_rect(page, margins, fitz)
        finally:
            document.close()
