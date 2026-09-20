import os
import unittest
from unittest.mock import patch

import pymupdf

from app.services.pdf_layout_optimizer import (
    _candidate_grids,
    _extract_vector_segments,
    _match_score,
    _rank_result,
    build_edge_signatures,
    choose_best_layout,
    optimize_pdf_layout,
)


def connected_signatures(rows, columns, slots, order):
    signatures = [
        {'left': [], 'right': [], 'top': [], 'bottom': []}
        for _ in range(sum(value is not None for value in slots))
    ]

    def page_at(row, column):
        slot_index = column * rows + row if order == 'column' else row * columns + column
        return slots[slot_index]

    seam = 10.0
    for row in range(rows):
        for column in range(columns):
            page = page_at(row, column)
            if page is None:
                continue
            if column + 1 < columns:
                right = page_at(row, column + 1)
                if right is not None:
                    signatures[page]['right'].append(seam)
                    signatures[right]['left'].append(seam)
                    seam += 7.0
            if row + 1 < rows:
                lower = page_at(row + 1, column)
                if lower is not None:
                    signatures[page]['bottom'].append(seam)
                    signatures[lower]['top'].append(seam)
                    seam += 7.0
    return signatures


class PdfLayoutOptimizerTest(unittest.TestCase):
    def test_seam_score_matches_each_crossing_only_once(self):
        self.assertAlmostEqual(
            _match_score([0.0, 1.7], [0.85]),
            2 / 3,
        )

    def test_rank_prefers_stronger_total_seam_quality_over_more_weak_matches(self):
        strong = {
            'matched_seams': 10,
            'perfect_seams': 10,
            'zero_seams': 0,
            'confidence_score': 1.0,
            'seam_evidence': 10,
            'page_slots': list(range(6)),
            'rows': 2,
            'columns': 3,
        }
        weak = {
            'matched_seams': 11,
            'perfect_seams': 0,
            'zero_seams': 9,
            'confidence_score': 0.4,
            'seam_evidence': 20,
            'page_slots': list(range(6)),
            'rows': 1,
            'columns': 6,
        }

        self.assertGreater(_rank_result(strong), _rank_result(weak))

    def test_candidate_grids_include_legal_extra_blank_columns(self):
        candidates = set(_candidate_grids(5, maximum_blank_cells=4))

        self.assertIn((2, 4), candidates)

    def test_finds_extra_blank_column_layout_after_optimizing_blank_positions(self):
        slots = [0, 1, 2, 3, None, 4, None, None]
        signatures = connected_signatures(2, 4, slots, 'row')

        result = choose_best_layout(signatures, 5, maximum_blank_cells=4)

        self.assertEqual((result['rows'], result['columns']), (2, 4))
        self.assertEqual(result['order'], 'row')
        self.assertEqual(result['page_slots'], slots)
        self.assertEqual(result['matched_seams'], 4)

    def test_equal_seams_prefer_compact_grid_without_empty_border(self):
        slots = list(range(6))
        signatures = connected_signatures(3, 2, slots, 'row')

        result = choose_best_layout(signatures, 6, maximum_blank_cells=4)

        self.assertEqual((result['rows'], result['columns']), (3, 2))
        self.assertEqual(result['page_slots'], slots)
        self.assertFalse(result['layout_ambiguous'])

    def test_preserves_red_pattern_segments_away_from_crop_boundary(self):
        class Page:
            rect = pymupdf.Rect(0, 0, 200, 200)
            rotation_matrix = pymupdf.Matrix(1, 1)

            def get_drawings(self):
                return [{
                    'color': (1.0, 0.0, 0.0),
                    'items': [
                        (
                            'l',
                            pymupdf.Point(25, 22.68),
                            pymupdf.Point(175, 22.68),
                        ),
                        (
                            'l',
                            pymupdf.Point(100, 40),
                            pymupdf.Point(100, 160),
                        ),
                    ],
                }]

        class Document:
            page_count = 1

            def load_page(self, _index):
                return Page()

        _sizes, pages = _extract_vector_segments(Document(), pymupdf)

        self.assertEqual(
            pages[0],
            [((100.0, 40.0), (100.0, 160.0))],
        )

    def test_boundary_aligned_guide_does_not_become_seam_evidence(self):
        signatures = build_edge_signatures(
            [[((0.0, 10.0), (0.0, 90.0))]],
            100.0,
            100.0,
            0.0,
        )

        self.assertEqual(
            signatures,
            [{'left': [], 'right': [], 'top': [], 'bottom': []}],
        )

    def test_finds_eight_by_three_column_major_layout(self):
        slots = list(range(24))
        signatures = connected_signatures(8, 3, slots, 'column')

        result = choose_best_layout(signatures, 24, maximum_blank_cells=4)

        self.assertEqual((result['rows'], result['columns']), (8, 3))
        self.assertEqual(result['order'], 'column')
        self.assertEqual(result['page_slots'], slots)
        self.assertEqual(result['confidence'], 'high')

    def test_finds_blank_before_page_eight_without_reordering_pages(self):
        slots = list(range(7)) + [None] + list(range(7, 46)) + [None]
        signatures = connected_signatures(8, 6, slots, 'column')

        result = choose_best_layout(signatures, 46, maximum_blank_cells=4)

        self.assertEqual((result['rows'], result['columns']), (8, 6))
        self.assertEqual(result['order'], 'column')
        self.assertEqual(result['page_slots'], slots)
        self.assertEqual(result['zero_seams'], 0)
        self.assertEqual(result['confidence'], 'high')

    def test_refuses_layout_without_enough_vector_seam_evidence(self):
        signatures = [
            {'left': [], 'right': [], 'top': [], 'bottom': []}
            for _ in range(6)
        ]

        result = choose_best_layout(signatures, 6, maximum_blank_cells=4)

        self.assertEqual(result['confidence'], 'low')
        self.assertEqual(result['matched_seams'], 0)

    def test_refuses_repeated_seams_that_cannot_identify_the_grid(self):
        signatures = [
            {'left': [10.0], 'right': [10.0], 'top': [], 'bottom': []}
            for _ in range(24)
        ]

        result = choose_best_layout(signatures, 24, maximum_blank_cells=4)

        self.assertEqual(result['confidence'], 'low')
        self.assertTrue(result['layout_ambiguous'])

    def test_equal_layout_scores_prefer_the_smallest_crop(self):
        class FakeDocument:
            page_count = 1

            def close(self):
                return None

        tied_result = {
            'rows': 1,
            'columns': 1,
            'order': 'row',
            'page_slots': [0],
            'seam_evidence': 0,
            'matched_seams': 0,
            'perfect_seams': 0,
            'zero_seams': 0,
            'confidence_score': 0.0,
            'confidence': 'low',
            'layout_ambiguous': False,
        }
        with patch.dict(os.environ, {
            'PDF_LAYOUT_OPTIMIZER_MAX_CROP_MM': '0.2',
            'PDF_LAYOUT_OPTIMIZER_CROP_STEP_MM': '0.1',
        }), patch(
            'app.services.pdf_layout_optimizer.load_fitz', return_value=object()
        ), patch(
            'app.services.pdf_layout_optimizer.open_pdf_document', return_value=FakeDocument()
        ), patch(
            'app.services.pdf_layout_optimizer.validate_pdf_complexity'
        ), patch(
            'app.services.pdf_layout_optimizer.read_pdf_layout_metadata_from_document',
            return_value=None,
        ), patch(
            'app.services.pdf_layout_optimizer._extract_vector_segments',
            return_value=([(600.0, 800.0)], [[((0.0, 0.0), (1.0, 1.0))]]),
        ), patch(
            'app.services.pdf_layout_optimizer.build_edge_signatures',
            return_value=[{'left': [], 'right': [], 'top': [], 'bottom': []}],
        ), patch(
            'app.services.pdf_layout_optimizer.choose_best_layout',
            side_effect=lambda *args, **kwargs: dict(tied_result),
        ):
            result = optimize_pdf_layout(b'%PDF fake')

        self.assertEqual(result['crop_margins_mm']['top'], 0.0)

    def test_incomplete_roundtrip_pdf_excludes_its_internal_guides_from_analysis(self):
        class FakeDocument:
            page_count = 1

            def close(self):
                return None

        low_result = {
            'rows': 1,
            'columns': 1,
            'order': 'row',
            'page_slots': [0],
            'seam_evidence': 0,
            'matched_seams': 0,
            'perfect_seams': 0,
            'zero_seams': 0,
            'confidence_score': 0.0,
            'confidence': 'low',
            'layout_ambiguous': False,
        }
        with patch(
            'app.services.pdf_layout_optimizer.load_fitz', return_value=object()
        ) as fitz, patch(
            'app.services.pdf_layout_optimizer.open_pdf_document', return_value=FakeDocument()
        ), patch(
            'app.services.pdf_layout_optimizer.validate_pdf_complexity'
        ), patch(
            'app.services.pdf_layout_optimizer.read_pdf_layout_metadata_from_document',
            return_value={'complete_layout': False},
        ), patch(
            'app.services.pdf_layout_optimizer._extract_vector_segments',
            return_value=([(600.0, 800.0)], [[((0.0, 0.0), (1.0, 1.0))]]),
        ) as extract, patch(
            'app.services.pdf_layout_optimizer.choose_best_layout',
            side_effect=lambda *args, **kwargs: dict(low_result),
        ):
            optimize_pdf_layout(b'%PDF fake')

        extract.assert_called_once_with(
            unittest.mock.ANY,
            fitz.return_value,
            ignore_internal_guides=True,
        )


if __name__ == '__main__':
    unittest.main()
