import unittest
from unittest.mock import patch

import pymupdf as fitz

from app.services import pdf_layout_optimizer as opt


def tiled_pdf(rotation=0, frame=True, first_unrelated=False, uniform=False):
    doc = fitz.open()
    if first_unrelated:
        doc.new_page(width=300, height=300)
    rect = fitz.Rect(56.69, 32.12, 538.58, 778.58)
    if uniform:
        margin = 8 * opt.MM_TO_PT
        rect = fitz.Rect(margin, margin, 595.28-margin, 841.89-margin)
    for index in range(9):
        page = doc.new_page(width=595.28, height=841.89)
        if frame:
            page.draw_rect(rect)
        row, col = divmod(index, 3)
        # Each seam has a unique position; neighboring pages share that position.
        if col:
            y = rect.y0 + 70 + (row * 2 + col - 1) * 50
            page.draw_line((rect.x0, y), (rect.x0 + 20, y + 7))
        if col < 2:
            y = rect.y0 + 70 + (row * 2 + col) * 50
            page.draw_line((rect.x1 - 20, y - 7), (rect.x1, y))
        if row:
            x = rect.x0 + 60 + ((row - 1) * 3 + col) * 45
            page.draw_line((x, rect.y0), (x + 7, rect.y0 + 20))
        if row < 2:
            x = rect.x0 + 60 + (row * 3 + col) * 45
            page.draw_line((x - 7, rect.y1 - 20), (x, rect.y1))
        page.set_rotation(rotation)
    if rotation == 90:
        doc.select([6, 3, 0, 7, 4, 1, 8, 5, 2])
    data = doc.tobytes()
    doc.close()
    return data


class AsymmetricOptimizerTest(unittest.TestCase):
    def test_four_different_margins_and_original_subset_indexes(self):
        result = opt.optimize_pdf_layout(tiled_pdf(first_unrelated=True), page_indexes=list(range(9, 0, -1)))
        self.assertEqual(result['confidence'], 'high')
        self.assertEqual((result['rows'], result['columns'], result['order']), (3, 3, 'row'))
        self.assertEqual(result['page_slots'], list(range(1, 10)))
        self.assertEqual(result['perfect_seams'], 12)
        expected = {'left': 56.69, 'top': 32.12, 'right': 56.70, 'bottom': 63.31}
        for side, points in expected.items():
            self.assertAlmostEqual(result['crop_margins_mm'][side], points / opt.MM_TO_PT, places=4)

    def test_normalizes_source_order_without_coercing_invalid_values(self):
        self.assertEqual(opt.normalize_pdf_page_indexes([2, 0], 3), [0, 2])
        self.assertEqual(opt.normalize_pdf_page_indexes(None, 3), [0, 1, 2])
        for indexes in ([], [True], [1.0], ['1'], [-1], [3], [1, 1], '1', (1,)):
            with self.subTest(indexes=indexes), self.assertRaises(ValueError):
                opt.normalize_pdf_page_indexes(indexes, 3)

    def test_rotated_frame_is_transformed_with_vectors(self):
        result = opt.optimize_pdf_layout(tiled_pdf(rotation=90))
        self.assertEqual(result['confidence'], 'high')
        self.assertEqual(result['perfect_seams'], 12)
        self.assertAlmostEqual(result['crop_margins_mm']['left'], 63.31 / opt.MM_TO_PT, places=4)
        self.assertAlmostEqual(result['crop_margins_mm']['top'], 56.69 / opt.MM_TO_PT, places=4)

    def test_rectangles_alone_do_not_invent_seams(self):
        doc = fitz.open()
        for _ in range(9):
            page = doc.new_page(width=600, height=800)
            page.draw_rect(fitz.Rect(60, 30, 540, 740))
        result = opt.optimize_pdf_layout(doc.tobytes())
        doc.close()
        self.assertEqual(result['confidence'], 'low')

    def test_no_frame_uses_existing_uniform_search(self):
        result = opt.optimize_pdf_layout(tiled_pdf(frame=False, uniform=True))
        self.assertEqual(result['confidence'], 'high')
        self.assertEqual(result['perfect_seams'], 12)
        for margin in result['crop_margins_mm'].values():
            self.assertEqual(margin, 8.0)

    def test_frame_edges_contribute_no_corner_crossings(self):
        segments = [[((10., 20.), (90., 20.)), ((90., 20.), (90., 80.)),
                     ((90., 80.), (10., 80.)), ((10., 80.), (10., 20.))]]
        margins = dict(left=10/opt.MM_TO_PT, right=10/opt.MM_TO_PT,
                       top=20/opt.MM_TO_PT, bottom=20/opt.MM_TO_PT)
        result = opt.build_edge_signatures(segments, 100., 100., margins)
        self.assertEqual(result, [dict.fromkeys(opt.EDGE_NAMES, [])])

    def test_mixed_selected_sizes_and_vectorless_selection_are_insufficient(self):
        data = tiled_pdf(first_unrelated=True)
        self.assertEqual(opt.optimize_pdf_layout(data)['confidence'], 'low')
        self.assertEqual(opt.optimize_pdf_layout(data, page_indexes=[0])['confidence'], 'low')

    def test_candidate_domain_and_real_clip_priority_over_many_internal_frames(self):
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        for inset in range(10, 60):
            page.draw_rect(fitz.Rect(inset, inset, 600-inset, 800-inset))
        page.wrap_contents()
        xref = page.get_contents()[0]
        original = doc.xref_stream(xref)
        doc.update_stream(xref, b'q 100 100 400 600 re W n\n' + original + b'\nQ')
        candidates = opt._extract_crop_candidates(doc, fitz, [0])
        doc.close()
        self.assertEqual(len(candidates), 16)
        self.assertAlmostEqual(candidates[0]['left'], 100 / opt.MM_TO_PT)
        self.assertAlmostEqual(candidates[0]['bottom'], 100 / opt.MM_TO_PT)
        for margins in candidates:
            self.assertTrue(all(0 <= margin <= 100 for margin in margins.values()))
            self.assertGreaterEqual(600-(margins['left']+margins['right'])*opt.MM_TO_PT, opt.MM_TO_PT)
            self.assertGreaterEqual(800-(margins['top']+margins['bottom'])*opt.MM_TO_PT, opt.MM_TO_PT)

    def test_uniform_candidate_beats_false_repeated_inner_frame(self):
        doc = fitz.open(stream=tiled_pdf(frame=False, uniform=True), filetype='pdf')
        for page in doc:
            page.draw_rect(fitz.Rect(60, 40, 530, 750))
        result = opt.optimize_pdf_layout(doc.tobytes())
        doc.close()
        self.assertEqual(result['confidence'], 'high')
        self.assertEqual(result['perfect_seams'], 12)
        self.assertEqual(set(result['crop_margins_mm'].values()), {8.0})

    def test_original_file_page_limit_remains_even_for_small_selection(self):
        doc = fitz.open()
        for _ in range(200):
            doc.new_page(width=100, height=100)
        self.assertEqual(opt.optimize_pdf_layout(doc.tobytes(), page_indexes=[199])['confidence'], 'low')
        doc.new_page(width=100, height=100)
        with self.assertRaisesRegex(ValueError, '200'):
            opt.optimize_pdf_layout(doc.tobytes(), page_indexes=[200])
        doc.close()

    def test_invalid_page_count_is_rejected(self):
        for count in (0, -1, True, 1.0, '1'):
            with self.subTest(count=count), self.assertRaises(ValueError):
                opt.normalize_pdf_page_indexes(None, count)

    def test_vector_suggestion_preserves_exposed_outer_edges(self):
        result = opt.optimize_pdf_layout(tiled_pdf())
        self.assertEqual(result['confidence'], 'high')
        self.assertIs(result['preserve_outer_edges'], True)
        self.assertEqual(opt.PDF_LAYOUT_ALGORITHM_VERSION, '4-asymmetric-selection-blank8-visible-clips')

    def test_original_metadata_suggestion_keeps_original_crop_semantics(self):
        metadata = {'rows': 2, 'columns': 2, 'order': 'row',
                    'page_slots': [0, 1, 2, 3], 'crop_margins_mm': dict.fromkeys(opt.EDGE_NAMES, 8.)}
        result = opt._embedded_layout_suggestion(metadata, [0, 3])
        self.assertIs(result['preserve_outer_edges'], False)
        self.assertEqual(result['page_slots'], [0, None, None, 3])
        self.assertEqual(result['crop_margins_mm'], metadata['crop_margins_mm'])

    def test_subset_metadata_preserves_holes_and_never_restores_deleted_pages(self):
        metadata = {'complete_layout': True, 'rows': 2, 'columns': 2, 'order': 'row',
                    'page_slots': [0, 1, 2, 3], 'crop_margins_mm': dict.fromkeys(opt.EDGE_NAMES, 0)}
        doc = fitz.open()
        for _ in range(4):
            doc.new_page()
        with patch.object(opt, 'read_pdf_layout_metadata_from_document', return_value=metadata):
            result = opt.optimize_pdf_layout(doc.tobytes(), metadata_mode='original', page_indexes=[0, 3])
        doc.close()
        self.assertEqual(result['page_slots'], [0, None, None, 3])
        self.assertEqual((result['rows'], result['columns']), (2, 2))


if __name__ == '__main__':
    unittest.main()
