import unittest
from unittest.mock import patch

import pymupdf as fitz

from app.services import pdf_layout_optimizer as opt
from app.services.pdf_to_plt import _extract_page
from test_pdf_layout_asymmetric import tiled_pdf


def stream_page(document, stream, rotation=0):
    page = document.new_page(width=100, height=100)
    xref = document.get_new_xref()
    document.update_object(xref, '<<>>')
    document.update_stream(xref, stream)
    page.set_contents(xref)
    page.set_rotation(rotation)
    return page


def hidden_seam_document():
    document = fitz.open()
    for index in range(7):
        stream_page(document, (
            b'q 40 40 20 20 re W n 0 10 m 100 10 l S 0 20 m 100 20 l S '
            + f'45 {45 + index % 4} m 55 {46 + index % 4} l S Q'.encode()))
    return document


class NativeClipOptimizerTest(unittest.TestCase):
    def test_invisible_seams_never_produce_high_confidence(self):
        with hidden_seam_document() as document:
            result = opt.optimize_pdf_layout(document.tobytes())
        self.assertEqual(result['confidence'], 'low')
        self.assertEqual(result.get('perfect_seams', 0), 0)

    def test_only_visible_center_segments_are_extracted(self):
        with hidden_seam_document() as document:
            _sizes, pages = opt._extract_vector_segments(document, fitz)
        self.assertEqual(pages, [
            [((45., float(55 - index % 4)), (55., float(54 - index % 4)))]
            for index in range(7)
        ])

    def test_visible_native_clip_seams_still_produce_high_confidence(self):
        with fitz.open(stream=tiled_pdf(), filetype='pdf') as document:
            for page in document:
                original = b'\n'.join(document.xref_stream(xref) for xref in page.get_contents())
                xref = document.get_new_xref()
                document.update_object(xref, '<<>>')
                document.update_stream(xref, b'q 0 0 595.28 841.89 re W n\n' + original + b'\nQ')
                page.set_contents(xref)
            result = opt.optimize_pdf_layout(document.tobytes())
        self.assertEqual(result['confidence'], 'high')
        self.assertEqual(result['perfect_seams'], 12)

    def test_nested_clip_scope_exit_and_all_rotations_match_preserved_conversion(self):
        expected = [((10, 80), (90, 80)), ((20, 70), (80, 70)),
                    ((10, 60), (90, 60)), ((0, 50), (100, 50))]
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), fitz.open() as document:
                page = stream_page(document, (
                    b'q 10 10 80 80 re W n 0 20 m 100 20 l S '
                    b'0 5 m 100 5 l S '
                    b'q 20 20 60 60 re W n 0 30 m 100 30 l S Q '
                    b'0 40 m 100 40 l S Q 0 50 m 100 50 l S'), rotation)
                _sizes, pages = opt._extract_vector_segments(document, fitz)
                transformed = [tuple(tuple(point * page.rotation_matrix)
                                     for point in map(fitz.Point, segment)) for segment in expected]
                self.assertEqual(pages[0], transformed)
                converted = _extract_page(page, 72, fitz, 1000, respect_native_clips=True)
                displayed = [[(point['x'], page.rect.height - point['y']) for point in shape]
                             for shape in converted['shapes']]
                self.assertEqual([tuple(points) for points in displayed], transformed)

    def test_deleted_pages_stay_excluded_and_page_boundary_clips_segments(self):
        with fitz.open() as document:
            stream_page(document, b'0 10 m 100 10 l S')
            stream_page(document, b'q -10 -10 120 120 re W n -20 50 m 120 50 l S Q')
            _sizes, pages = opt._extract_vector_segments(document, fitz, page_indexes=[1])
        self.assertEqual(pages, [[((0., 50.), (100., 50.))]])

    def test_legacy_page_without_extended_keyword_remains_compatible(self):
        with fitz.open() as document:
            page = stream_page(document, b'0 50 m 100 50 l S')
            class LegacyPage:
                rect = page.rect
                rotation_matrix = page.rotation_matrix
                def get_drawings(self):
                    return page.get_drawings()
            class LegacyDocument:
                page_count = 1
                def load_page(self, index):
                    return LegacyPage()
            self.assertEqual(opt._extract_vector_segments(LegacyDocument(), fitz)[1],
                             [[((0., 50.), (100., 50.))]])

    def test_fully_hidden_vectors_keep_formal_vector_fallback_and_legacy_geometry(self):
        with fitz.open() as document:
            page = stream_page(document, b'q 40 40 20 20 re W n 0 10 m 100 10 l S Q')
            with patch('app.services.pdf_to_plt.rasterize_page') as raster:
                visible = _extract_page(page, 72, fitz, 1000, respect_native_clips=True)
                legacy = _extract_page(page, 72, fitz, 1000)
                raster.assert_not_called()
            self.assertEqual(visible['source_type'], 'vector')
            self.assertEqual(visible['shapes'], [])
            self.assertEqual(legacy['shapes'], [[{'x': 0., 'y': 10.}, {'x': 100., 'y': 10.}]])
