import math
import random
import unittest
from unittest.mock import patch

from app.services import pdf_layout_optimizer as opt
from tests.test_pdf_layout_asymmetric import tiled_pdf
from tests.test_pdf_layout_optimizer import connected_signatures


NO_PRUNE = patch.object(opt, '_minimum_relevant_total', return_value=float('-inf'))


def random_signatures(rng, page_count):
    pool = [round(rng.uniform(0, 100), 2) for _ in range(6)]
    signatures = []
    for _ in range(page_count):
        edges = {}
        for name in opt.EDGE_NAMES:
            count = rng.choice((0, 0, 1, 1, 2, 3))
            edges[name] = sorted(rng.choice(pool) for _ in range(count))
        signatures.append(edges)
    return signatures


def random_slots(rng, page_count, blank_count):
    slots = list(range(page_count))
    for _ in range(blank_count):
        slots.insert(rng.randrange(len(slots) + 1), None)
    return slots


def choose_without_pruning(signatures, page_count, maximum_blank_cells=8):
    with NO_PRUNE:
        return opt.choose_best_layout(
            signatures,
            page_count,
            maximum_blank_cells=maximum_blank_cells,
        )


class PdfLayoutPruneUpperBoundTest(unittest.TestCase):
    def test_upper_bound_covers_optimized_and_random_placements(self):
        rng = random.Random(20261104)
        for page_count in range(4, 31):
            signatures = random_signatures(rng, page_count)
            grids = [
                grid for grid in opt._candidate_grids(page_count, maximum_blank_cells=3)
                if grid[0] * grid[1] > page_count
            ]
            rng.shuffle(grids)
            for rows, columns in grids[:3]:
                for order in ('row', 'column'):
                    with self.subTest(
                        page_count=page_count, rows=rows, columns=columns, order=order,
                    ):
                        blank_count = rows * columns - page_count
                        score_cache = {}
                        candidate = opt._evaluate_layout(
                            rows,
                            columns,
                            order,
                            list(range(page_count)) + [None] * blank_count,
                            signatures,
                            score_cache,
                        )
                        bound = opt._blank_search_upper_bound(
                            candidate, signatures, page_count, score_cache,
                        )
                        optimized = opt._optimize_blank_positions(
                            candidate, signatures, page_count, score_cache,
                        )
                        self.assertLessEqual(
                            opt._total_score(optimized),
                            bound + 0.00005 * (optimized['seam_evidence'] + 1),
                        )
                        for _ in range(30):
                            placed = opt._evaluate_layout(
                                rows,
                                columns,
                                order,
                                random_slots(rng, page_count, blank_count),
                                signatures,
                                score_cache,
                            )
                            self.assertLessEqual(
                                opt._total_score(placed),
                                bound + 0.00005 * (placed['seam_evidence'] + 1),
                            )


class PdfLayoutPruneEquivalenceTest(unittest.TestCase):
    def assert_equivalent(self, signatures, page_count, maximum_blank_cells=8):
        normal = opt.choose_best_layout(
            signatures,
            page_count,
            maximum_blank_cells=maximum_blank_cells,
        )
        baseline = choose_without_pruning(
            signatures,
            page_count,
            maximum_blank_cells=maximum_blank_cells,
        )
        self.assertEqual(normal, baseline)
        return normal

    def test_random_partial_match_signatures(self):
        rng = random.Random(424242)
        for page_count in range(4, 13):
            with self.subTest(page_count=page_count):
                self.assert_equivalent(
                    random_signatures(rng, page_count),
                    page_count,
                    maximum_blank_cells=4,
                )

    def test_complete_connected_signatures(self):
        for rows, columns, order in ((3, 3, 'row'), (8, 3, 'column'), (2, 4, 'row')):
            with self.subTest(rows=rows, columns=columns, order=order):
                signatures = connected_signatures(
                    rows, columns, list(range(rows * columns)), order,
                )
                self.assert_equivalent(signatures, rows * columns, maximum_blank_cells=4)

    def test_repeated_edge_signatures_are_ambiguous(self):
        signatures = [
            dict.fromkeys(opt.EDGE_NAMES, [10.0])
            for _ in range(12)
        ]
        self.assert_equivalent(signatures, 12, maximum_blank_cells=4)

    def test_empty_signatures(self):
        signatures = [
            {name: [] for name in opt.EDGE_NAMES}
            for _ in range(6)
        ]
        self.assert_equivalent(signatures, 6, maximum_blank_cells=4)

    def test_complete_connected_signatures_with_blank_cells(self):
        cases = (
            (2, 4, 'row', [0, 1, 2, 3, None, 4, None, None]),
            (3, 4, 'column', [None, 0, 1, 2, 3, None, 4, 5, 6, 7, None, None]),
        )
        for rows, columns, order, slots in cases:
            with self.subTest(rows=rows, columns=columns, order=order, slots=slots):
                signatures = connected_signatures(rows, columns, slots, order)
                page_count = sum(page is not None for page in slots)
                self.assert_equivalent(signatures, page_count, maximum_blank_cells=4)

    def test_near_competitor_stays_ambiguous(self):
        signatures = [
            dict.fromkeys(opt.EDGE_NAMES, [10.0])
            for _ in range(9)
        ]
        normal = opt.choose_best_layout(signatures, 9, maximum_blank_cells=4)
        baseline = choose_without_pruning(signatures, 9, maximum_blank_cells=4)
        self.assertTrue(normal['layout_ambiguous'])
        self.assertEqual(normal['layout_ambiguous'], baseline['layout_ambiguous'])
        self.assertEqual(normal['confidence'], baseline['confidence'])

    def test_threshold_keeps_weakest_possible_competitor(self):
        """Candidates at the exact _has_competing_layout limits must stay above the threshold."""
        for page_count in (4, 12, 46, 200):
            maximum_evidence = 2 * page_count
            for best_evidence in range(4, maximum_evidence + 1, max(1, page_count // 4)):
                for best_score in (0.02, 0.5, 0.92, 0.9999, 1.0):
                    best_total = opt._total_score(
                        {'confidence_score': best_score, 'seam_evidence': best_evidence}
                    )
                    evidence = math.ceil(best_evidence * 0.9)
                    # The candidate's exact seam sum sits below its 4-decimal rounded score.
                    seam_total = (best_score - 0.02) * evidence - 0.00005 * evidence
                    self.assertGreaterEqual(
                        seam_total,
                        opt._minimum_relevant_total(best_total, page_count),
                    )

    def test_pruning_skips_blank_search_on_strong_match(self):
        signatures = connected_signatures(8, 3, list(range(24)), 'column')
        with patch.object(
            opt, '_optimize_blank_positions', wraps=opt._optimize_blank_positions,
        ) as mocked:
            opt.choose_best_layout(signatures, 24, maximum_blank_cells=4)
            pruned_calls = mocked.call_count
        with NO_PRUNE:
            with patch.object(
                opt, '_optimize_blank_positions', wraps=opt._optimize_blank_positions,
            ) as mocked:
                opt.choose_best_layout(signatures, 24, maximum_blank_cells=4)
                baseline_calls = mocked.call_count
        self.assertLess(pruned_calls, baseline_calls)


class PdfLayoutPruneEndToEndTest(unittest.TestCase):
    def test_optimize_pdf_layout_equivalent_with_and_without_pruning(self):
        data = tiled_pdf()
        normal = opt.optimize_pdf_layout(data)
        with NO_PRUNE:
            baseline = opt.optimize_pdf_layout(data)
        self.assertEqual(normal, baseline)


if __name__ == '__main__':
    unittest.main()
