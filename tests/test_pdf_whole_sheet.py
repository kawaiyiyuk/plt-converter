import os
import unittest
from unittest.mock import patch

import pymupdf as fitz

from app.services.pattern_pdf_to_plt import (
    PatternPdfRejected,
    convert_pattern_pdf_to_plt,
)
from app.services.pdf_metadata import encode_pdf_layout_metadata
from app.services.pdf_to_plt import convert_pdf_to_plt, validate_pdf_complexity
from app.services.pdf_whole_sheet import (
    UNITS_PER_MM,
    has_big_piece,
    read_whole_sheet,
)


MM_TO_PT = 72 / 25.4
CONTENT_WIDTH_PT = 300 * MM_TO_PT
CONTENT_HEIGHT_PT = 400 * MM_TO_PT


def pentagon_points():
    return [
        (0, 0),
        (CONTENT_WIDTH_PT, 0),
        (CONTENT_WIDTH_PT, CONTENT_HEIGHT_PT * 0.62),
        (CONTENT_WIDTH_PT * 0.5, CONTENT_HEIGHT_PT),
        (0, CONTENT_HEIGHT_PT * 0.62),
    ]


def rectangle_points():
    return [
        (0, 0),
        (CONTENT_WIDTH_PT, 0),
        (CONTENT_WIDTH_PT, CONTENT_HEIGHT_PT),
        (0, CONTENT_HEIGHT_PT),
    ]


def default_extra_paths(rows, columns, page_width, page_height, bottom_up, count=20):
    """Return short moving paths that sit outside page 1.

    The whole-sheet outline itself is one long path spanning many windows, so
    these extra paths provide the >5% moving-path budget and the >30% of paths
    completely outside page 1.
    """
    if columns > 1:
        span = CONTENT_WIDTH_PT - page_width
        return [
            (
                (page_width + 5 + (index % 10) * (span - 10) / 10,
                 10 + (index // 10) * 10),
                (page_width + 6 + (index % 10) * (span - 10) / 10,
                 10 + (index // 10) * 10),
            )
            for index in range(count)
        ]
    if bottom_up:
        band_start = 0
    else:
        band_start = page_height
    return [
        (
            (10 + (index % 10) * 10, band_start + 5 + (index // 10) * 10),
            (11 + (index % 10) * 10, band_start + 5 + (index // 10) * 10),
        )
        for index in range(count)
    ]


def build_whole_sheet(
    rows,
    columns,
    order='row',
    bottom_up=False,
    points=None,
    extra=None,
    fixed_rect=None,
    static=False,
    page_extra=None,
    image=False,
    metadata_subject=None,
    perturb_page=None,
    perturb_indexes=(),
):
    page_width = CONTENT_WIDTH_PT / columns
    page_height = CONTENT_HEIGHT_PT / rows
    outline = points or pentagon_points()
    extra_paths = default_extra_paths(
        rows, columns, page_width, page_height, bottom_up
    )
    extra_paths.extend(extra or [])
    document = fitz.open()
    for index in range(rows * columns):
        if order == 'row':
            row, column = divmod(index, columns)
        else:
            row, column = index % rows, index // rows
        if static:
            origin_x = origin_y = 0
        else:
            origin_x = -(column * page_width)
            if bottom_up:
                origin_y = -(rows - 1) * page_height + row * page_height
            else:
                origin_y = -(row * page_height)
        page = document.new_page(width=page_width, height=page_height)
        page.draw_polyline(
            [(x + origin_x, y + origin_y) for x, y in outline],
            closePath=True,
        )
        for path_index, (a, b) in enumerate(extra_paths):
            shift = 10 if (index == perturb_page
                           and path_index in perturb_indexes) else 0
            page.draw_line(
                (a[0] + origin_x + shift, a[1] + origin_y),
                (b[0] + origin_x + shift, b[1] + origin_y),
            )
        for a, b in (page_extra or {}).get(index, []):
            page.draw_line(a, b)
        if fixed_rect is not None:
            page.draw_rect(fitz.Rect(fixed_rect))
        if image and index == 0:
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), 0)
            pixmap.set_rect(pixmap.irect, (128, 128, 128))
            page.insert_image(page.rect, pixmap=pixmap)
    if metadata_subject is not None:
        document.set_metadata({'subject': metadata_subject})
    data = document.tobytes()
    document.close()
    return data


def outline_shapes(points_mm):
    points = [(x * UNITS_PER_MM, y * UNITS_PER_MM) for x, y in points_mm]
    return [
        [
            {'x': start[0], 'y': start[1]},
            {'x': end[0], 'y': end[1]},
        ]
        for start, end in zip(points, points[1:] + points[:1])
    ]


class WholeSheetReadTest(unittest.TestCase):
    def test_row_layout_reads_first_page_and_reports_grid(self):
        sheet = read_whole_sheet(build_whole_sheet(2, 3))

        self.assertIsNotNone(sheet)
        self.assertEqual(sheet['rows'], 2)
        self.assertEqual(sheet['columns'], 3)
        self.assertEqual(sheet['order'], 'row')
        self.assertEqual(sheet['page_count'], 6)
        self.assertEqual(sheet['fixed_paths_removed'], 0)
        self.assertAlmostEqual(
            sheet['width_units'] * 25.4 / 1016, 300, delta=0.1
        )
        self.assertAlmostEqual(
            sheet['height_units'] * 25.4 / 1016, 400, delta=0.1
        )
        self.assertTrue(has_big_piece(sheet['shapes'], UNITS_PER_MM))

    def test_column_layout_from_bottom_up_reads_sheet(self):
        sheet = read_whole_sheet(
            build_whole_sheet(2, 2, order='column', bottom_up=True)
        )

        self.assertIsNotNone(sheet)
        self.assertEqual(sheet['rows'], 2)
        self.assertEqual(sheet['columns'], 2)
        self.assertEqual(sheet['order'], 'column')
        self.assertEqual(sheet['page_count'], 4)
        self.assertAlmostEqual(
            sheet['width_units'] * 25.4 / 1016, 300, delta=0.1
        )
        self.assertAlmostEqual(
            sheet['height_units'] * 25.4 / 1016, 400, delta=0.1
        )

    def test_fixed_path_is_dropped(self):
        sheet = read_whole_sheet(
            build_whole_sheet(2, 3, fixed_rect=(10, 10, 60, 40))
        )

        self.assertIsNotNone(sheet)
        self.assertEqual(sheet['fixed_paths_removed'], 1)
        self.assertAlmostEqual(
            sheet['width_units'] * 25.4 / 1016, 300, delta=0.1
        )
        self.assertAlmostEqual(
            sheet['height_units'] * 25.4 / 1016, 400, delta=0.1
        )

    def test_different_path_counts_are_rejected(self):
        source = build_whole_sheet(
            2, 3, page_extra={1: [((10, 10), (20, 10))]}
        )
        self.assertIsNone(read_whole_sheet(source))

    def test_static_pages_are_rejected(self):
        self.assertIsNone(read_whole_sheet(build_whole_sheet(2, 3, static=True)))

    def test_page_image_is_rejected(self):
        self.assertIsNone(read_whole_sheet(build_whole_sheet(2, 3, image=True)))

    def test_irregular_offsets_are_rejected(self):
        source = build_whole_sheet(
            2, 3, perturb_page=1, perturb_indexes=(0, 1)
        )
        self.assertIsNone(read_whole_sheet(source))

    def test_path_outside_all_windows_is_rejected(self):
        outside = (
            (CONTENT_WIDTH_PT + 50, 10),
            (CONTENT_WIDTH_PT + 50, 60),
        )
        self.assertIsNone(
            read_whole_sheet(build_whole_sheet(2, 3, extra=[outside]))
        )

    def test_path_overlapping_window_but_outside_bounds_is_rejected(self):
        oversized = ((10, 10), (CONTENT_WIDTH_PT + 50, 10))
        self.assertIsNone(
            read_whole_sheet(build_whole_sheet(2, 3, extra=[oversized]))
        )

    def test_embedded_layout_metadata_is_rejected(self):
        metadata = {
            'version': 1,
            'rows': 2,
            'columns': 3,
            'order': 'row',
            'page_slots': [0, 1, 2, 3, 4, 5],
            'crop_margins_mm': {
                'top': 0, 'right': 0, 'bottom': 0, 'left': 0,
            },
            'drawing_width_mm': 300,
            'drawing_height_mm': 400,
            'complete_layout': True,
        }
        source = build_whole_sheet(
            2, 3, metadata_subject=encode_pdf_layout_metadata(metadata)
        )
        self.assertIsNone(read_whole_sheet(source))


class WholeSheetConversionTest(unittest.TestCase):
    def test_convert_returns_whole_sheet_result_and_layout_keys(self):
        plt, result = convert_pattern_pdf_to_plt(build_whole_sheet(2, 3))

        self.assertTrue(plt.startswith(b'IN;'))
        self.assertEqual(result['layout_source'], 'whole_sheet')
        self.assertEqual(result['layout_confidence'], 'high')
        self.assertEqual(result['skipped_page_indexes'], [])
        self.assertEqual(result['seam_repair'], {
            'crop_delta_mm': 0,
            'frames_removed': 0,
            'welded_joints': 0,
        })
        self.assertIs(result['review_required'], True)

        document = fitz.open()
        page = document.new_page(width=300, height=300)
        page.draw_rect(fitz.Rect(20, 20, 200, 200))
        plain_source = document.tobytes()
        document.close()
        _plt, reference = convert_pdf_to_plt(plain_source, {})

        self.assertEqual(set(result['layout']), set(reference))
        self.assertEqual(result['layout']['rows'], 2)
        self.assertEqual(result['layout']['columns'], 3)
        self.assertEqual(result['layout']['order'], 'row')
        self.assertEqual(result['layout']['selected_count'], 6)
        self.assertEqual(result['layout']['empty_cells'], 0)
        self.assertAlmostEqual(result['layout']['width_mm'], 300, delta=0.1)
        self.assertAlmostEqual(result['layout']['height_mm'], 400, delta=0.1)
        self.assertEqual(result['layout']['source_types'], ['vector'])
        self.assertIs(result['layout']['raster_fallback'], False)

    def test_whole_sheet_without_big_piece_is_rejected(self):
        source = build_whole_sheet(2, 3, points=rectangle_points())
        with self.assertRaisesRegex(
            PatternPdfRejected, '没有识别到完整的大块裁片'
        ):
            convert_pattern_pdf_to_plt(source)

    def test_whole_sheet_over_drawing_limit_is_still_converted(self):
        page_width = CONTENT_WIDTH_PT / 2
        extra = []
        for index in range(600):
            x = page_width + 5 + (index % 200) * 1.5
            y = 20 + (index // 200) * 15
            extra.append(((x, y), (x + 1, y)))
        source = build_whole_sheet(1, 2, extra=extra)

        with patch.dict(os.environ, {'PDF_MAX_DRAWINGS': '1000'}):
            document = fitz.open(stream=source, filetype='pdf')
            try:
                with self.assertRaises(ValueError):
                    validate_pdf_complexity(document)
            finally:
                document.close()
            plt, result = convert_pattern_pdf_to_plt(source)

        self.assertTrue(plt.startswith(b'IN;'))
        self.assertEqual(result['layout_source'], 'whole_sheet')
        self.assertEqual(result['layout']['rows'], 1)
        self.assertEqual(result['layout']['columns'], 2)

    def test_whole_sheet_segment_limit_is_rejected(self):
        page_width = CONTENT_WIDTH_PT / 2
        extra = []
        for index in range(1100):
            x = page_width + 5 + (index % 200) * 1.5
            y = 20 + (index // 200) * 15
            extra.append(((x, y), (x + 1, y)))
        source = build_whole_sheet(1, 2, extra=extra)

        with patch.dict(os.environ, {'PDF_MAX_OUTPUT_SEGMENTS': '1000'}):
            with self.assertRaisesRegex(PatternPdfRejected, '线条数量过多'):
                convert_pattern_pdf_to_plt(source)


class BigPieceTest(unittest.TestCase):
    def test_pure_rectangle_is_not_big_piece(self):
        self.assertFalse(
            has_big_piece(outline_shapes(rectangle_points()), UNITS_PER_MM)
        )

    def test_large_non_rectangular_outline_is_big_piece(self):
        self.assertTrue(
            has_big_piece(outline_shapes(pentagon_points()), UNITS_PER_MM)
        )

    def test_small_outline_is_not_big_piece(self):
        small = [(0, 0), (50, 0), (50, 37), (25, 60), (0, 37)]
        self.assertFalse(has_big_piece(outline_shapes(small), UNITS_PER_MM))


if __name__ == '__main__':
    unittest.main()
