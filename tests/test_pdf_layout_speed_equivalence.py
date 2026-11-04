import unittest
from unittest.mock import patch

from app.services import pdf_layout_optimizer as opt
from tests.test_pdf_layout_asymmetric import tiled_pdf
from tests.test_pdf_layout_optimizer import connected_signatures


def reference_evaluate_layout(rows, columns, order, page_slots, signatures, score_cache=None):
    scores = []
    slots = page_slots
    if len(slots) < rows * columns:
        slots = list(slots) + [None] * (rows * columns - len(slots))
    for index, adjacent_index, first_side, second_side in opt._layout_adjacencies(rows, columns, order):
        page, adjacent = slots[index], slots[adjacent_index]
        if page is None or adjacent is None:
            continue
        score = opt._seam_score(page, adjacent, first_side, second_side, signatures, score_cache)
        if score is not None:
            scores.append(score)
    evidence = len(scores)
    return {
        'rows': rows,
        'columns': columns,
        'order': order,
        'page_slots': list(page_slots),
        'seam_evidence': evidence,
        'matched_seams': sum(score > 0 for score in scores),
        'perfect_seams': sum(score >= 0.999 for score in scores),
        'zero_seams': sum(score == 0 for score in scores),
        'confidence_score': round(sum(scores) / evidence, 4) if evidence else 0.0,
    }


def mm(points):
    return points / opt.MM_TO_PT


def boundary_segments_fixture():
    return [
        [
            # Edge-hugging segments on all four sides.
            ((0.0, 10.0), (0.0, 90.0)),
            ((100.0, 10.0), (100.0, 90.0)),
            ((10.0, 0.0), (90.0, 0.0)),
            ((10.0, 100.0), (90.0, 100.0)),
            # Crossing the left boundary.
            ((0.0, 20.0), (60.0, 20.0)),
            # Middle segments that cannot cross any candidate boundary.
            ((40.0, 40.0), (60.0, 40.0)),
            ((40.0, 60.0), (40.0, 60.0)),
        ],
        [
            # Rectangle frame exactly on the c2 crop boundary (10/90/20/80 pt).
            ((10.0, 20.0), (90.0, 20.0)),
            ((90.0, 20.0), (90.0, 80.0)),
            ((90.0, 80.0), (10.0, 80.0)),
            ((10.0, 80.0), (10.0, 20.0)),
            # Crossing the right boundary.
            ((40.0, 30.0), (100.0, 30.0)),
            # Endpoint inside the expanded band edge, just past the real boundary.
            ((10.5, 40.0), (10.5, 60.0)),
            # Middle diagonal.
            ((45.0, 45.0), (55.0, 55.0)),
        ],
    ]


def crop_candidates_fixture():
    return [
        dict.fromkeys(opt.EDGE_NAMES, 0.0),
        dict.fromkeys(opt.EDGE_NAMES, 5.0),
        {'left': mm(10.0), 'right': mm(10.0), 'top': mm(20.0), 'bottom': mm(20.0)},
        {'left': mm(8.0), 'right': mm(2.0), 'top': mm(6.0), 'bottom': mm(12.0)},
    ]


class PdfLayoutBoundaryFilterTest(unittest.TestCase):
    def test_filtered_segments_produce_identical_signatures_for_every_crop(self):
        segments = boundary_segments_fixture()
        candidates = crop_candidates_fixture()
        filtered = opt._segments_near_crop_boundaries(segments, 100.0, 100.0, candidates)
        self.assertLess(
            sum(len(page) for page in filtered),
            sum(len(page) for page in segments),
        )
        for margins in candidates:
            with self.subTest(margins=margins):
                self.assertEqual(
                    opt.build_edge_signatures(filtered, 100.0, 100.0, margins),
                    opt.build_edge_signatures(segments, 100.0, 100.0, margins),
                )

    def test_middle_segments_are_removed_and_edge_segments_kept(self):
        segments = boundary_segments_fixture()
        filtered = opt._segments_near_crop_boundaries(
            segments, 100.0, 100.0, crop_candidates_fixture(),
        )
        self.assertNotIn(((40.0, 40.0), (60.0, 40.0)), filtered[0])
        self.assertNotIn(((40.0, 60.0), (40.0, 60.0)), filtered[0])
        self.assertNotIn(((45.0, 45.0), (55.0, 55.0)), filtered[1])
        for segment in segments[0][:5] + segments[1][:6]:
            self.assertIn(segment, filtered[0] + filtered[1])

    def test_endpoint_within_safety_margin_of_band_edge_is_kept(self):
        segments = [[((10.5, 40.0), (10.5, 60.0))]]
        candidates = [
            {'left': mm(10.0), 'right': mm(2.0), 'top': mm(6.0), 'bottom': mm(12.0)},
        ]
        filtered = opt._segments_near_crop_boundaries(segments, 100.0, 100.0, candidates)
        self.assertEqual(filtered, segments)
        # The real left boundary is 10pt; with the 1pt margin the band ends at 11pt.
        self.assertLess(abs(10.5 - 11.0), opt.BOUNDARY_FILTER_MARGIN_PT)

    def test_filter_preserves_page_count_and_segment_order(self):
        segments = boundary_segments_fixture()
        filtered = opt._segments_near_crop_boundaries(
            segments, 100.0, 100.0, crop_candidates_fixture(),
        )
        self.assertEqual(len(filtered), len(segments))
        for original, kept in zip(segments, filtered):
            position = 0
            for segment in kept:
                while position < len(original) and original[position] != segment:
                    position += 1
                self.assertLess(position, len(original))
                position += 1


class PdfLayoutEvaluateEquivalenceTest(unittest.TestCase):
    def signatures_with_empty_edges(self):
        return [
            {'left': [], 'right': [10.0, 20.0], 'top': [], 'bottom': []},
            {'left': [10.0, 20.0], 'right': [], 'top': [], 'bottom': []},
            {'left': [], 'right': [], 'top': [], 'bottom': []},
            {'left': [5.0], 'right': [5.0], 'top': [], 'bottom': []},
            {'left': [], 'right': [], 'top': [], 'bottom': []},
        ]

    def layouts(self):
        return (
            (2, 2, 'row', list(range(4))),
            (2, 3, 'row', [0, 1, 2, 3, 4, None]),
            (2, 3, 'column', [0, 1, 2, 3, None, 4]),
            (3, 2, 'row', [0, 1, 2, 3, 4, None]),
            (1, 5, 'row', [0, None, 1, 2, 3]),
            (3, 3, 'row', list(range(5)) + [None] * 4),
            (2, 3, 'row', [0, 1, 2]),
            (3, 3, 'column', [None, 0, 1, 2, 3, None, 4, None, None]),
        )

    def prefilled_cache(self, rows, columns, order, slots, signatures):
        cache = {}
        padded = list(slots)
        if len(padded) < rows * columns:
            padded += [None] * (rows * columns - len(padded))
        for index, adjacent_index, first_side, second_side in opt._layout_adjacencies(
            rows, columns, order,
        ):
            page, adjacent = padded[index], padded[adjacent_index]
            if page is None or adjacent is None:
                continue
            opt._seam_score(page, adjacent, first_side, second_side, signatures, cache)
        return cache

    def test_new_evaluation_matches_reference_without_cache(self):
        signatures = self.signatures_with_empty_edges()
        for rows, columns, order, slots in self.layouts():
            with self.subTest(rows=rows, columns=columns, order=order, slots=slots):
                self.assertEqual(
                    opt._evaluate_layout(rows, columns, order, slots, signatures),
                    reference_evaluate_layout(rows, columns, order, slots, signatures),
                )

    def test_new_evaluation_matches_reference_with_empty_cache(self):
        signatures = self.signatures_with_empty_edges()
        for rows, columns, order, slots in self.layouts():
            with self.subTest(rows=rows, columns=columns, order=order, slots=slots):
                self.assertEqual(
                    opt._evaluate_layout(rows, columns, order, slots, signatures, {}),
                    reference_evaluate_layout(rows, columns, order, slots, signatures, {}),
                )

    def test_new_evaluation_matches_reference_with_prefilled_cache(self):
        signatures = self.signatures_with_empty_edges()
        for rows, columns, order, slots in self.layouts():
            cache = self.prefilled_cache(rows, columns, order, slots, signatures)
            with self.subTest(rows=rows, columns=columns, order=order, slots=slots):
                self.assertEqual(
                    opt._evaluate_layout(rows, columns, order, slots, signatures, dict(cache)),
                    reference_evaluate_layout(rows, columns, order, slots, signatures, dict(cache)),
                )

    def test_new_evaluation_matches_reference_on_connected_signatures(self):
        signatures = connected_signatures(5, 7, [None, 0, 1, 2, 3, None, 4, 5, 6, 7, 8, 9,
                                                 10, 11, 12, None, 13, 14, 15, 16, None,
                                                 17, 18, 19, 20, None, 21, 22, 23, 24, None,
                                                 25, 26, 27, 28], 'column')
        for rows, columns, order, slots in (
            (5, 7, 'column', [None, 0, 1, 2, 3, None, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                              None, 13, 14, 15, 16, None, 17, 18, 19, 20, None, 21, 22,
                              23, 24, None, 25, 26, 27, 28]),
            (4, 8, 'row', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, None, 15, 16,
                           17, 18, 19, 20, 21, 22, 23, 24, None, 25, 26, 27, 28, None]),
        ):
            prefilled = self.prefilled_cache(rows, columns, order, slots, signatures)
            for label, cache in (('none', None), ('empty', {}),
                                 ('prefilled', prefilled)):
                with self.subTest(rows=rows, columns=columns, order=order, cache=label):
                    self.assertEqual(
                        opt._evaluate_layout(rows, columns, order, slots, signatures, cache),
                        reference_evaluate_layout(
                            rows, columns, order, slots, signatures,
                            dict(cache) if cache is not None else None,
                        ),
                    )


class PdfLayoutSpeedEndToEndTest(unittest.TestCase):
    def test_optimize_pdf_layout_matches_unfiltered_segments(self):
        data = tiled_pdf()
        filtered = opt.optimize_pdf_layout(data)
        with patch.object(
            opt, '_segments_near_crop_boundaries',
            side_effect=lambda segments, *args: segments,
        ):
            unfiltered = opt.optimize_pdf_layout(data)
        self.assertEqual(filtered, unfiltered)


if __name__ == '__main__':
    unittest.main()
