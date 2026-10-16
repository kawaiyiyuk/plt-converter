import os
import unittest
from unittest.mock import patch
from pathlib import Path

from app.services import pdf_layout_optimizer as opt
from tests.test_pdf_layout_optimizer import connected_signatures


SLOTS_29 = [None, 0, 1, 2, 3, None, 4, 5, 6, 7, 8, 9, 10, 11, 12,
            None, 13, 14, 15, 16, None, 17, 18, 19, 20, None, 21, 22, 23, 24,
            None, 25, 26, 27, 28]


def multi_crossing_signatures(rows, columns, slots, order):
    signatures = connected_signatures(rows, columns, slots, order)
    return [{side: sorted(value for crossing in crossings for value in (crossing*10, crossing*10+5))
             for side, crossings in page.items()} for page in signatures]


class PdfLayoutBlankCoverageTest(unittest.TestCase):
    def test_default_search_finds_six_blank_29_page_layout(self):
        signatures = multi_crossing_signatures(5, 7, SLOTS_29, 'column')
        result = opt.choose_best_layout(signatures, 29)
        self.assertEqual((result['rows'], result['columns'], result['order']), (5, 7, 'column'))
        self.assertEqual(result['page_slots'], SLOTS_29)
        self.assertEqual(result['confidence'], 'high')
        self.assertEqual(result['matched_seams'], 46)

    def test_incomplete_four_blank_search_downgrades_false_high(self):
        signatures = multi_crossing_signatures(5, 7, SLOTS_29, 'column')
        result = opt.choose_best_layout(signatures, 29, maximum_blank_cells=4)
        self.assertEqual((result['rows'], result['columns']), (4, 8))
        self.assertEqual(result['confidence'], 'low')
        self.assertTrue(result['blank_seam_conflict'])
        self.assertEqual(result['blank_seam_conflict_count'], 3)

    def test_effective_blank_budget_is_shared_and_hard_capped(self):
        for value, expected in (('4', 4), ('8', 8), ('999', 8), ('-2', 0), ('bad', 8)):
            with self.subTest(value=value), patch.dict(os.environ, {'PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS': value}):
                self.assertEqual(opt.effective_pdf_layout_max_blank_cells(), expected)

    def test_single_crossing_repeated_guides_and_outer_border_do_not_trigger(self):
        layout = dict(rows=1, columns=4, order='row', page_slots=[0, None, None, 1])
        empty = lambda: dict(left=[], right=[], top=[], bottom=[])
        signatures = [empty(), empty()]
        signatures[0]['right'] = [10.]
        signatures[1]['left'] = [10.]
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures), 0)
        signatures[0]['right'] = [10., 30.]
        signatures[1]['left'] = [10., 30.]
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures), 1)
        signatures.append(empty())
        signatures[2]['left'] = [10., 30.]
        layout.update(columns=5, page_slots=[0, None, None, 1, 2])
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures), 0)
        layout.update(columns=4, page_slots=[None, 0, 1, None])
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures[:2]), 0)

    def test_vertical_blank_run_uses_bottom_to_top_evidence(self):
        signatures = [dict(left=[], right=[], top=[], bottom=[10., 30.]),
                      dict(left=[], right=[], top=[10., 30.], bottom=[])]
        layout = dict(rows=4, columns=1, order='column', page_slots=[0, None, None, 1])
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures), 1)
        signatures[1]['top'] = [10., 50.]
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures), 0)

    def test_minor_annotation_mismatch_does_not_erase_strong_unique_conflict(self):
        empty = lambda: dict(left=[], right=[], top=[], bottom=[])
        signatures = [empty(), empty()]
        signatures[0]['right'] = list(range(10, 110, 10))
        signatures[1]['left'] = list(range(10, 100, 10))
        layout = dict(rows=1, columns=3, order='row', page_slots=[0, None, 1])
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures), 1)

    def test_repeated_page_pair_scores_are_cached_without_changing_results(self):
        signatures = multi_crossing_signatures(3, 3, list(range(9)), 'row')
        with patch.object(opt, '_match_score', wraps=opt._match_score) as match:
            cached = opt.choose_best_layout(signatures, 9)
            cached_calls = match.call_count
        def uncached(first, second, first_side, second_side, signatures, score_cache):
            return opt._match_score(signatures[first][first_side], signatures[second][second_side])
        with patch.object(opt, '_seam_score', side_effect=uncached), patch.object(opt, '_match_score', wraps=opt._match_score) as match:
            uncached_result = opt.choose_best_layout(signatures, 9)
            uncached_calls = match.call_count
        self.assertEqual(cached, uncached_result)
        self.assertLess(cached_calls, uncached_calls / 4)

    def test_cached_grid_topology_preserves_reference_evaluation_order(self):
        signatures = multi_crossing_signatures(3, 3, list(range(9)), 'row')
        for rows, columns, order, slots in (
            (3, 3, 'row', list(range(9))),
            (3, 4, 'column', [None, 0, 1, 2, None, 3, 4, 5, 6, None, 7, 8]),
            (4, 3, 'row', [0, None, 1, 2, 3, None, 4, 5, 6, 7, None, 8]),
        ):
            scores = []
            for row in range(rows):
                for column in range(columns):
                    first = opt._page_at(slots, rows, columns, order, row, column)
                    if first is None:
                        continue
                    for dr, dc, first_side, second_side in ((0, 1, 'right', 'left'), (1, 0, 'bottom', 'top')):
                        if row+dr >= rows or column+dc >= columns:
                            continue
                        second = opt._page_at(slots, rows, columns, order, row+dr, column+dc)
                        if second is not None:
                            score = opt._match_score(signatures[first][first_side], signatures[second][second_side])
                            if score is not None:
                                scores.append(score)
            result = opt._evaluate_layout(rows, columns, order, slots, signatures, {})
            self.assertEqual(result['seam_evidence'], len(scores))
            self.assertEqual(result['confidence_score'], round(sum(scores)/len(scores), 4))
            self.assertEqual(result['perfect_seams'], sum(score >= .999 for score in scores))
            self.assertEqual(result['matched_seams'], sum(score > 0 for score in scores))
            self.assertEqual(result['zero_seams'], sum(score == 0 for score in scores))

    def test_production_template_uses_eight_blank_default(self):
        self.assertEqual(opt.DEFAULT_MAX_BLANK_CELLS, 8)
        template = Path(__file__).parents[1] / 'compose.production.yaml'
        self.assertIn('PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS: 8', template.read_text())

    def test_unique_strong_seam_across_blank_is_reported(self):
        signatures = multi_crossing_signatures(2, 3, list(range(6)), 'row')
        # Put an internal empty column between the first two real columns.
        layout = dict(rows=2, columns=4, order='row', page_slots=[0, None, 1, 2, 3, None, 4, 5])
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures), 2)

    def test_legitimate_blanks_with_no_crossing_are_not_conflicts(self):
        signatures = multi_crossing_signatures(5, 7, SLOTS_29, 'column')
        layout = dict(rows=5, columns=7, order='column', page_slots=SLOTS_29)
        self.assertEqual(opt._blank_seam_conflicts(layout, signatures), 0)


if __name__ == '__main__':
    unittest.main()
