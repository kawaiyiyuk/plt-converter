import tempfile
import unittest

import pymupdf as fitz

from app.services.pdf_cover_filter import detect_leading_cover_pages
from app.services.pdf_layout_optimizer import optimize_pdf_layout
from app.services.pdf_to_pdf import automatic_layout_suggestion, convert_pdf_to_pdf
from app.services.pdf_to_plt import inspect_pdf


BODY_WIDTH = 595.28
BODY_HEIGHT = 841.89
BODY_RECT = fitz.Rect(56.69, 32.12, 538.58, 778.58)


def raster_pixmap():
    pixmap = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 120, 160), False)
    for y in range(160):
        for x in range(120):
            pixmap.set_pixel(x, y, ((x + y) * 255 // 278,))
    return pixmap


def add_cover_page(document, width=BODY_WIDTH, height=BODY_HEIGHT, lines=0):
    page = document.new_page(width=width, height=height)
    page.insert_image(page.rect, pixmap=raster_pixmap())
    for index in range(lines):
        page.draw_line((20, 20 + index * 30), (width - 20, 30 + index * 30))
    return page


def add_body_page(document, index):
    """Same seam fixture as the optimizer tests, so a 3x3 grid is detected."""
    page = document.new_page(width=BODY_WIDTH, height=BODY_HEIGHT)
    page.draw_rect(BODY_RECT)
    row, column = divmod(index, 3)
    if column:
        y = BODY_RECT.y0 + 70 + (row * 2 + column - 1) * 50
        page.draw_line((BODY_RECT.x0, y), (BODY_RECT.x0 + 20, y + 7))
    if column < 2:
        y = BODY_RECT.y0 + 70 + (row * 2 + column) * 50
        page.draw_line((BODY_RECT.x1 - 20, y - 7), (BODY_RECT.x1, y))
    if row:
        x = BODY_RECT.x0 + 60 + ((row - 1) * 3 + column) * 45
        page.draw_line((x, BODY_RECT.y0), (x + 7, BODY_RECT.y0 + 20))
    if row < 2:
        x = BODY_RECT.x0 + 60 + (row * 3 + column) * 45
        page.draw_line((x - 7, BODY_RECT.y1 - 20), (x, BODY_RECT.y1))
    return page


def build_document(cover_count=0, body_count=9, cover_width=BODY_WIDTH,
                   cover_height=BODY_HEIGHT):
    document = fitz.open()
    for _ in range(cover_count):
        add_cover_page(document, cover_width, cover_height)
    for index in range(body_count):
        add_body_page(document, index)
    return document


class PdfCoverFilterTest(unittest.TestCase):
    def detect(self, source):
        with fitz.open(stream=source, filetype='pdf') as document:
            return detect_leading_cover_pages(document, fitz)

    def document_bytes(self, document):
        source = document.tobytes()
        document.close()
        return source

    def test_different_size_image_cover_is_skipped_by_layout_suggestion(self):
        document = build_document(cover_count=1, cover_width=203 * 72 / 25.4,
                                  cover_height=244.5 * 72 / 25.4)
        source = self.document_bytes(document)

        self.assertEqual(self.detect(source), [0])
        suggestion = automatic_layout_suggestion(source)

        used_pages = [index for index in suggestion['page_slots'] if index is not None]
        self.assertNotIn(0, used_pages)
        self.assertEqual(used_pages, list(range(1, 10)))
        self.assertEqual(suggestion['skipped_page_indexes'], [0])
        self.assertIn(suggestion['confidence'], {'high', 'medium'})

    def test_same_size_image_cover_is_detected(self):
        source = self.document_bytes(build_document(cover_count=1))

        self.assertEqual(self.detect(source), [0])

    def test_two_leading_image_pages_are_both_detected(self):
        document = fitz.open()
        add_cover_page(document)
        add_cover_page(document, 203 * 72 / 25.4, 244.5 * 72 / 25.4)
        for index in range(9):
            add_body_page(document, index)
        source = self.document_bytes(document)

        self.assertEqual(self.detect(source), [0, 1])

    def test_image_page_with_three_vector_lines_is_not_a_cover(self):
        document = fitz.open()
        add_cover_page(document, lines=3)
        for index in range(9):
            add_body_page(document, index)
        source = self.document_bytes(document)

        self.assertEqual(self.detect(source), [])

    def test_image_page_with_one_compound_vector_path_is_not_a_cover(self):
        document = fitz.open()
        page = add_cover_page(document)
        shape = page.new_shape()
        for index in range(12):
            shape.draw_line((20, 20 + index * 30), (BODY_WIDTH - 20, 30 + index * 30))
        shape.finish(color=(0, 0, 0))
        shape.commit()
        self.assertEqual(len(page.get_drawings()), 1)
        for index in range(9):
            add_body_page(document, index)
        source = self.document_bytes(document)

        self.assertEqual(self.detect(source), [])

    def test_body_scan_stops_after_bounded_page_window(self):
        document = fitz.open()
        add_cover_page(document)
        for _ in range(12):
            document.new_page(width=BODY_WIDTH, height=BODY_HEIGHT)
        for index in range(9):
            add_body_page(document, index)
        source = self.document_bytes(document)

        self.assertEqual(self.detect(source), [])

    def test_all_image_document_is_not_filtered(self):
        document = fitz.open()
        for _ in range(6):
            add_cover_page(document)
        source = self.document_bytes(document)

        self.assertEqual(self.detect(source), [])

    def test_document_below_four_pages_is_not_filtered(self):
        document = fitz.open()
        for _ in range(3):
            add_cover_page(document)
        source = self.document_bytes(document)

        self.assertEqual(self.detect(source), [])

    def test_non_leading_image_page_is_not_filtered(self):
        document = fitz.open()
        add_body_page(document, 0)
        add_cover_page(document)
        for index in range(1, 9):
            add_body_page(document, index)
        source = self.document_bytes(document)

        self.assertEqual(self.detect(source), [])

    def test_inspect_pdf_marks_only_cover_pages(self):
        source = self.document_bytes(build_document(cover_count=1))
        with tempfile.TemporaryDirectory() as folder:
            pages = inspect_pdf(source, folder, 'cover-preview')

        self.assertEqual(pages[0]['suggested_exclusion'], 'cover')
        self.assertNotIn('suggested_exclusion', pages[1])
        editor_indexes = [page['index'] for page in pages if page.get('editor_preview_name')]
        self.assertEqual(editor_indexes, [0, 1])

    def test_inspect_pdf_without_cover_keeps_single_editor_preview(self):
        source = self.document_bytes(build_document())
        with tempfile.TemporaryDirectory() as folder:
            pages = inspect_pdf(source, folder, 'plain-preview')

        self.assertFalse(any('suggested_exclusion' in page for page in pages))
        editor_indexes = [page['index'] for page in pages if page.get('editor_preview_name')]
        self.assertEqual(editor_indexes, [0])

    def test_convert_pdf_to_pdf_skips_leading_cover(self):
        source = self.document_bytes(build_document(cover_count=1))

        _pdf, result = convert_pdf_to_pdf(source, {'paper_size': 'A4'})

        self.assertEqual(result['skipped_page_indexes'], [0])
        self.assertEqual(result['source_page_count'], 10)
        self.assertEqual(result['source_layout']['selected_count'], 9)

    def test_size_mismatch_reason_names_the_different_page(self):
        document = fitz.open()
        page = document.new_page(width=300, height=300)
        for index in range(3):
            page.draw_line((20, 20 + index * 30), (280, 30 + index * 30))
        for index in range(6):
            add_body_page(document, index)
        source = self.document_bytes(document)

        result = optimize_pdf_layout(source)

        self.assertEqual(result['confidence'], 'low')
        self.assertIn('第 1 页尺寸与其他页不同', result['reason'])


if __name__ == '__main__':
    unittest.main()
