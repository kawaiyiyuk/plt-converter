"""Observable PDF -> PLT -> PDF regressions using anonymous vector fixtures."""

import unittest
from unittest.mock import patch

import pymupdf

from app.services.pdf_layout_optimizer import optimize_pdf_layout
from app.services.pdf_renderer import render_pdf
from app.services.pdf_to_pdf import convert_pdf_to_pdf
from app.services.pdf_to_plt import read_pdf_layout_metadata
from app.services.plt_parser import parse_plt
from test_pdf_layout_asymmetric import tiled_pdf


MM_TO_PT = 72 / 25.4


class PdfToPdfOuterEdgesTest(unittest.TestCase):
    def source(self):
        # Three 103x120mm pages in [0, 1, None, 2]. Asymmetric crops leave
        # 93x104mm base tiles; preserved source windows span 196x224mm.
        with pymupdf.open() as document:
            markers = (
                ((1, 30, 2, 30), (101, 50, 102, 50)),
                ((101, 40, 102, 40), (1, 70, 2, 70), (50, 118, 50, 119)),
                ((1, 60, 2, 60), (30, 118, 30, 119)),
            )
            for lines in markers:
                page = document.new_page(width=103 * MM_TO_PT, height=120 * MM_TO_PT)
                page.draw_rect(pymupdf.Rect(MM_TO_PT, MM_TO_PT, 102 * MM_TO_PT, 119 * MM_TO_PT))
                # Interior cross-lines keep legacy output non-empty and anchor
                # its clipped bounds independently of the outer markers.
                page.draw_line((MM_TO_PT, 60 * MM_TO_PT), (102 * MM_TO_PT, 60 * MM_TO_PT))
                page.draw_line((60 * MM_TO_PT, MM_TO_PT), (60 * MM_TO_PT, 119 * MM_TO_PT))
                for x0, y0, x1, y1 in lines:
                    page.draw_line((x0 * MM_TO_PT, y0 * MM_TO_PT),
                                   (x1 * MM_TO_PT, y1 * MM_TO_PT))
            return document.tobytes()

    def suggestion(self, **changes):
        return {
            'confidence': 'high', 'source': 'vector_seams',
            'rows': 2, 'columns': 2, 'order': 'row',
            'page_slots': [0, 1, None, 2],
            'crop_margins_mm': {'left': 3, 'right': 7, 'top': 5, 'bottom': 11},
            'output_rotation': 0, 'preserve_outer_edges': True, **changes,
        }

    def convert_with_suggestion(self, source, paper_size, suggestion):
        # Only freeze the layout decision. Extraction, HPGL serialization and
        # parsing, PDF rendering and metadata encoding all remain real.
        with patch('app.services.pdf_to_pdf.optimize_pdf_layout', return_value=suggestion):
            return convert_pdf_to_pdf(source, {'paper_size': paper_size})

    def assert_ink(self, document, x_mm, y_mm, present, page_index=0):
        # Read rasterized output, so paths hidden by a PDF clip cannot pass.
        page = document[page_index]
        clip = pymupdf.Rect((x_mm - .2) * MM_TO_PT, (y_mm - .2) * MM_TO_PT,
                            (x_mm + .2) * MM_TO_PT, (y_mm + .2) * MM_TO_PT)
        pixels = page.get_pixmap(matrix=pymupdf.Matrix(4, 4), clip=clip,
                                 colorspace=pymupdf.csGRAY, alpha=False)
        self.assertEqual(any(value < 100 for value in pixels.samples), present,
                         (page_index, x_mm, y_mm))

    def assert_output_metadata(self, output, result, width, height, pages):
        metadata = read_pdf_layout_metadata(output)
        self.assertTrue(metadata['complete_layout'])
        self.assertAlmostEqual(metadata['drawing_width_mm'], width, places=2)
        self.assertAlmostEqual(metadata['drawing_height_mm'], height, places=2)
        self.assertEqual(metadata['page_slots'], list(range(pages)))
        self.assertEqual(metadata, result['target_layout']['embedded_layout'])

    def test_preserved_outer_and_hole_edges_are_visible_and_internal_edges_are_cut(self):
        source = self.source()
        for paper_size, pages, page_size in (('SINGLE', 1, (214, 242)), ('A4', 2, (210, 297))):
            with self.subTest(paper_size=paper_size):
                output, result = self.convert_with_suggestion(source, paper_size, self.suggestion())
                self.assertEqual(result['source_layout']['empty_cells'], 1)
                self.assertEqual((result['source_layout']['width_mm'], result['source_layout']['height_mm']),
                                 (196, 224))
                self.assertEqual(result['output_page_count'], pages)
                self.assert_output_metadata(output, result, 194, 222, pages)
                with pymupdf.open(stream=output, filetype='pdf') as document:
                    self.assertEqual(document.page_count, pages)
                    for page in document:
                        self.assertAlmostEqual(page.rect.width / MM_TO_PT, page_size[0], places=2)
                        self.assertAlmostEqual(page.rect.height / MM_TO_PT, page_size[1], places=2)
                    # Reconstruct ink bounds across the A4 tiles. These are
                    # actual rendered black paths, in millimetres at 1:1.
                    rectangles = [
                        (item['rect'].x0 / MM_TO_PT + index * 190,
                         item['rect'].y0 / MM_TO_PT,
                         item['rect'].x1 / MM_TO_PT + index * 190,
                         item['rect'].y1 / MM_TO_PT)
                        for index, page in enumerate(document)
                        for item in page.get_drawings() if item['color'] == (0., 0., 0.)
                    ]
                    bounds = (min(rect[0] for rect in rectangles), min(rect[1] for rect in rectangles),
                              max(rect[2] for rect in rectangles), max(rect[3] for rect in rectangles))
                    for actual, expected in zip(bounds, (10, 10, 204, 232)):
                        self.assertAlmostEqual(actual, expected, places=2)
                    # 1mm outer border + 10mm target margin gives a 9mm offset.
                    self.assert_ink(document, 10.5, 39, True)  # top-left outer side
                    self.assert_ink(document, 103.5, 173, True)  # left side beside hole
                    self.assert_ink(document, 132, 231.5, True)  # bottom outer side
                    self.assert_ink(document, 203.5 if pages == 1 else 13.5, 49, True,
                                    page_index=0 if pages == 1 else 1)
                    self.assert_ink(document, 110.5, 59, False)  # page 0 right seam
                    self.assert_ink(document, 103.5, 79, False)  # page 1 left seam
                    self.assert_ink(document, 152, 127.5, False)  # page 1 bottom seam
                self.assertIs(result['source_layout']['preserve_outer_edges'], True)

    def test_false_and_missing_flag_keep_legacy_geometry_and_a4_page_count(self):
        source = self.source()
        missing = self.suggestion()
        del missing['preserve_outer_edges']
        for paper_size, page_size in (('SINGLE', (206, 228)), ('A4', (210, 297))):
            results = []
            for suggestion in (missing, self.suggestion(preserve_outer_edges=False)):
                output, result = self.convert_with_suggestion(source, paper_size, suggestion)
                self.assertIs(result['source_layout']['preserve_outer_edges'], False)
                self.assertEqual(result['output_page_count'], 1)
                self.assert_output_metadata(output, result, 186, 208, 1)
                with pymupdf.open(stream=output, filetype='pdf') as document:
                    self.assertEqual(document.page_count, 1)
                    self.assertAlmostEqual(document[0].rect.width / MM_TO_PT, page_size[0], places=2)
                    self.assertAlmostEqual(document[0].rect.height / MM_TO_PT, page_size[1], places=2)
                results.append(result)
            self.assertEqual(results[0], results[1])

    def test_real_optimizer_suggestion_preserves_outer_ink_through_composite_conversion(self):
        # Existing anonymous 3x3 fixture supplies twelve unique vector seams.
        # Add a short outer mark, away from all seam evidence/crop frames.
        with pymupdf.open(stream=tiled_pdf(), filetype='pdf') as document:
            document[0].draw_line((MM_TO_PT, 5 * MM_TO_PT), (2 * MM_TO_PT, 6 * MM_TO_PT))
            source = document.tobytes()
        with patch('app.services.pdf_to_pdf.optimize_pdf_layout', wraps=optimize_pdf_layout) as optimize:
            output, result = convert_pdf_to_pdf(source, {'paper_size': 'SINGLE'})
        optimize.assert_called_once()
        self.assertEqual(result['layout_source'], 'vector_seams')
        self.assertEqual(result['layout_confidence'], 'high')
        self.assertEqual((result['source_layout']['rows'], result['source_layout']['columns']), (3, 3))
        with pymupdf.open(stream=output, filetype='pdf') as document:
            self.assertEqual(document.page_count, 1)
            # This mark lies wholly outside the detected left/top crop and
            # becomes the upper-left ink bound: removal loses real geometry.
            self.assert_ink(document, 10.5, 10.5, True)
        self.assertIs(result['source_layout']['preserve_outer_edges'], True)
        metadata = read_pdf_layout_metadata(output)
        self.assertEqual(metadata, result['target_layout']['embedded_layout'])
        self.assertGreater(metadata['drawing_width_mm'], 500)
        self.assertGreater(metadata['drawing_height_mm'], 700)

    def test_single_source_and_complete_metadata_do_not_enable_preservation(self):
        with pymupdf.open() as document:
            page = document.new_page(width=100 * MM_TO_PT, height=120 * MM_TO_PT)
            page.draw_rect(pymupdf.Rect(MM_TO_PT, MM_TO_PT, 99 * MM_TO_PT, 119 * MM_TO_PT))
            single = document.tobytes()
        with patch('app.services.pdf_to_pdf.optimize_pdf_layout') as optimize:
            output, result = convert_pdf_to_pdf(single, {'paper_size': 'SINGLE'})
        optimize.assert_not_called()
        self.assertEqual(result['layout_source'], 'single_page')
        self.assertIs(result['source_layout']['preserve_outer_edges'], False)
        self.assert_output_metadata(output, result, 98, 118, 1)

        drawing = parse_plt(b'IN;PU0,0;PD18000,0,18000,24000,0,24000,0,0;')
        generated, generated_layout = render_pdf(drawing, {'paper_size': 'A4', 'margin_mm': 10})
        self.assertGreater(generated_layout['page_count'], 1)
        output, result = convert_pdf_to_pdf(generated, {'paper_size': 'SINGLE', 'metadata_mode': 'original'})
        self.assertEqual(result['layout_source'], 'embedded_metadata')
        self.assertIs(result['source_layout']['preserve_outer_edges'], False)
        self.assertIs(result['source_layout']['embedded_layout_applied'], True)
        self.assert_output_metadata(output, result, 450, 600, 1)
        with pymupdf.open(stream=output, filetype='pdf') as document:
            self.assertEqual(document.page_count, 1)
            self.assertAlmostEqual(document[0].rect.width / MM_TO_PT, 470, places=2)
            self.assertAlmostEqual(document[0].rect.height / MM_TO_PT, 620, places=2)


if __name__ == '__main__':
    unittest.main()
