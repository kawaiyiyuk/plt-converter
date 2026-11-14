import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from app.services import pattern_pdf_to_plt as converter
from app.services.pattern_pdf_to_plt import (
    PatternPdfRejected,
    convert_pattern_pdf_to_plt,
)
from app.services.pdf_renderer import render_pdf
from app.services.pdf_to_pdf import source_conversion_options
from app.services.pdf_to_plt import convert_pdf_to_plt
from app.services.plt_parser import parse_plt
from app.tasks import (
    _requires_conversion_billing,
    commit_successful_conversion_billing,
    release_failed_conversion_billing,
)


MM_TO_PT = 72 / 25.4
UNITS_PER_PT = 1016 / 72
CROP_MM = 10.0
CROP_PT = CROP_MM * MM_TO_PT


def vector_page(source, line=((20, 40), (280, 40))):
    document = fitz.open()
    page = document.new_page(width=300, height=300)
    page.draw_line(*line)
    data = document.tobytes()
    document.close()
    return data


def tiled_2x2_pdf(rows=2, columns=2):
    document = fitz.open()
    rect = fitz.Rect(CROP_PT, CROP_PT, 300 - CROP_PT, 300 - CROP_PT)
    for index in range(rows * columns):
        page = document.new_page(width=300, height=300)
        page.draw_rect(rect)
        row, column = divmod(index, columns)
        if column == 0:
            for offset in range(3):
                y = rect.y0 + 40 + offset * 17 + row * 5
                page.draw_line((rect.x1 - 25, y), (rect.x1, y))
        else:
            for offset in range(3):
                y = rect.y0 + 40 + offset * 17 + row * 5
                page.draw_line((rect.x0, y), (rect.x0 + 25, y))
        if row == 0:
            for offset in range(3):
                x = rect.x0 + 40 + offset * 17 + column * 5
                page.draw_line((x, rect.y1 - 25), (x, rect.y1))
        else:
            for offset in range(3):
                x = rect.x0 + 40 + offset * 17 + column * 5
                page.draw_line((x, rect.y0), (x, rect.y0 + 25))
    data = document.tobytes()
    document.close()
    return data


SUGGESTION = {
    'confidence': 'high',
    'rows': 2,
    'columns': 2,
    'order': 'row',
    'page_slots': [0, 1, 2, 3],
    'crop_margins_mm': {'top': CROP_MM, 'right': CROP_MM,
                        'bottom': CROP_MM, 'left': CROP_MM},
    'output_rotation': 0,
    'preserve_outer_edges': True,
    'source': 'vector_seams',
    'skipped_page_indexes': [],
}


class PatternPdfConversionTest(unittest.TestCase):
    def test_single_page_rejects_vectorless_and_image_heavy_pdfs(self):
        document = fitz.open()
        document.new_page(width=300, height=300)
        blank = document.tobytes()
        document.close()
        with self.assertRaisesRegex(
            PatternPdfRejected, '扫描件或图片'
        ):
            convert_pattern_pdf_to_plt(blank)

        document = fitz.open()
        page = document.new_page(width=300, height=300)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), 0)
        pixmap.set_rect(pixmap.irect, (128, 128, 128))
        page.insert_image(page.rect, pixmap=pixmap)
        page.draw_line((10, 10), (100, 10))
        scanned = document.tobytes()
        document.close()
        with self.assertRaisesRegex(
            PatternPdfRejected, '扫描件或图片'
        ):
            convert_pattern_pdf_to_plt(scanned)

    def test_single_vector_page_is_converted_without_seam_repair(self):
        plt, result = convert_pattern_pdf_to_plt(vector_page(None))

        self.assertTrue(plt.startswith(b'IN;'))
        self.assertEqual(result['layout_source'], 'single_page')
        self.assertEqual(result['layout_confidence'], 'high')
        self.assertEqual(result['skipped_page_indexes'], [])
        self.assertEqual(result['seam_repair'], {
            'crop_delta_mm': 0,
            'frames_removed': 0,
            'welded_joints': 0,
        })
        self.assertIs(result['review_required'], True)

    def test_medium_and_low_confidence_are_rejected_without_workbench_hint(self):
        source = tiled_2x2_pdf()
        for confidence in ('medium', 'low'):
            with self.subTest(confidence=confidence), patch.object(
                converter, 'automatic_layout_suggestion',
                return_value={**SUGGESTION, 'confidence': confidence},
            ), self.assertRaises(PatternPdfRejected) as raised:
                convert_pattern_pdf_to_plt(source)
            self.assertEqual(str(raised.exception), '这份 PDF 的拼版把握不够，无法自动转换')
            self.assertNotIn('工作台', str(raised.exception))

        with patch.object(
            converter, 'automatic_layout_suggestion',
            side_effect=ValueError(
                '无法可靠识别原 PDF 的页面拼版：接缝不足。'
                '请改用“PDF 转 PLT”工作台手动调整后再转换。'
            ),
        ), self.assertRaises(PatternPdfRejected) as raised:
            convert_pattern_pdf_to_plt(source)
        self.assertEqual(
            str(raised.exception), '无法可靠识别这份 PDF 的拼版：接缝不足'
        )
        self.assertNotIn('工作台', str(raised.exception))

    def test_raster_fallback_is_rejected(self):
        source = vector_page(None)
        with patch.object(
            converter, 'convert_pdf_to_plt',
            return_value=(b'', {'raster_fallback': True}),
        ), self.assertRaisesRegex(PatternPdfRejected, '扫描页或图片页'):
            convert_pattern_pdf_to_plt(source)

    def test_embedded_layout_uses_original_metadata(self):
        document = parse_plt(
            b'IN;PU0,0;PD12000,0;PD12000,4000;PD0,4000;PD0,0;', 1016
        )
        pdf, render_layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'show_page_number': True,
        })
        self.assertGreater(render_layout['page_count'], 1)
        self.assertTrue(render_layout['embedded_layout']['complete_layout'])

        real_suggestion = converter.automatic_layout_suggestion
        seen = {}

        def capture(source, page_count=None, metadata_mode=None):
            seen['metadata_mode'] = metadata_mode
            return real_suggestion(source, page_count, metadata_mode)

        with patch.object(
            converter, 'automatic_layout_suggestion', side_effect=capture
        ):
            plt, result = convert_pattern_pdf_to_plt(pdf)

        self.assertEqual(seen['metadata_mode'], 'original')
        self.assertEqual(result['layout_source'], 'embedded')
        self.assertTrue(result['layout']['embedded_layout_applied'])
        self.assertTrue(result['layout']['embedded_layout_detected'])
        self.assertEqual(result['seam_repair']['crop_delta_mm'], 0)
        self.assertEqual(result['seam_repair']['frames_removed'], 0)
        self.assertTrue(plt.startswith(b'IN;'))

    def test_two_by_two_frames_are_removed_and_cross_seam_paths_are_welded(self):
        source = tiled_2x2_pdf()
        with patch.object(
            converter, 'automatic_layout_suggestion', return_value=SUGGESTION
        ), patch.object(converter, '_select_crop_delta', return_value=0):
            plt, result = convert_pattern_pdf_to_plt(source)

        self.assertEqual(result['layout_source'], 'vector_seams')
        self.assertEqual(result['seam_repair']['crop_delta_mm'], 0)
        self.assertEqual(result['seam_repair']['frames_removed'], 4)
        self.assertEqual(result['seam_repair']['welded_joints'], 12)

        shapes = parse_plt(plt, 1016)['shapes']
        self.assertEqual(len(shapes), 12)
        seam = round((300 - CROP_PT) * UNITS_PER_PT)
        vertical = []
        horizontal = []
        for shape in shapes:
            points = []
            for point in shape['points']:
                if not points or points[-1] != point:
                    points.append(point)
            self.assertIn(len(points), {3, 4})
            if points[0]['x'] == points[-1]['x']:
                horizontal.append(points)
            else:
                vertical.append(points)
        self.assertEqual(len(vertical), 6)
        self.assertEqual(len(horizontal), 6)
        for points in vertical:
            self.assertAlmostEqual(points[1]['x'], seam)
            self.assertEqual(len({point['y'] for point in points}), 1)
        for points in horizontal:
            self.assertAlmostEqual(points[1]['y'], seam)
            self.assertEqual(len({point['x'] for point in points}), 1)

    def test_pipeline_without_postprocessor_is_byte_identical_to_baseline(self):
        baseline = Path(__file__).resolve().parents[1] / '.oc-baseline'
        baseline_module = baseline / 'app' / 'services' / 'pdf_to_plt.py'
        if not baseline_module.exists():
            self.skipTest('baseline checkout is unavailable')

        source = tiled_2x2_pdf()
        options = {
            'rows': 2,
            'columns': 2,
            'margin_mm': 0,
            'line_width_mm': 0.265,
        }
        branch_plt, _layout = convert_pdf_to_plt(source, options)

        handle, path = tempfile.mkstemp(suffix='.pdf')
        try:
            with os.fdopen(handle, 'wb') as stream:
                stream.write(source)
            script = (
                'import hashlib, sys\n'
                'from app.services.pdf_to_plt import convert_pdf_to_plt\n'
                'source = open(sys.argv[1], "rb").read()\n'
                'plt, _layout = convert_pdf_to_plt(source, {\n'
                '    "rows": 2, "columns": 2, "margin_mm": 0,\n'
                '    "line_width_mm": 0.265,\n'
                '})\n'
                'print(hashlib.sha256(plt).hexdigest())\n'
            )
            environment = os.environ.copy()
            environment['PYTHONPATH'] = str(baseline)
            completed = subprocess.run(
                [sys.executable, '-c', script, path],
                cwd=str(baseline),
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            os.unlink(path)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            hashlib.sha256(branch_plt).hexdigest(),
            completed.stdout.strip(),
        )

    def test_crop_delta_with_most_matching_seam_endpoints_is_selected(self):
        source = tiled_2x2_pdf()
        options = source_conversion_options(SUGGESTION)
        options['metadata_mode'] = None
        counts = {}
        for delta in converter.CROP_DELTA_CANDIDATES:
            candidate = converter._crop_options(options, delta)
            if candidate is None:
                continue
            stats = {'frames_removed': 0, 'welded_joints': 0}
            postprocessor = converter._repair_postprocessor(
                converter.UNITS_PER_MM, 0.02, stats
            )
            converter._convert_with(source, candidate, postprocessor)
            counts[delta] = stats['welded_joints']

        best = None
        for delta in converter.CROP_DELTA_CANDIDATES:
            if delta in counts and (best is None or counts[delta] > counts[best]):
                best = delta

        with patch.object(
            converter, 'automatic_layout_suggestion', return_value=SUGGESTION
        ), patch.object(
            converter, '_select_crop_delta', wraps=converter._select_crop_delta
        ) as select:
            _, result = convert_pattern_pdf_to_plt(source)

        select.assert_called_once()
        self.assertEqual(result['seam_repair']['crop_delta_mm'], best)
        self.assertGreater(counts[best], count_for(counts, 0))


def count_for(counts, delta):
    return counts.get(delta, -1)


class PatternPdfBillingTest(unittest.TestCase):
    def test_billing_is_required_for_the_new_job_type(self):
        self.assertTrue(_requires_conversion_billing({
            'job_type': 'pattern_pdf_to_plt',
            'billing_access_method': 'points',
        }))
        self.assertFalse(_requires_conversion_billing({
            'job_type': 'plt_to_pdf',
            'billing_access_method': 'points',
        }))

    @patch('app.tasks.release_conversion', return_value=True)
    def test_failed_job_releases_billing(self, release):
        record = {
            'job_type': 'pattern_pdf_to_plt',
            'billing_access_method': 'points',
            'user_key': 'user:42',
            'billing_request_id': 'request-1',
            'job_id': 'job-1',
        }

        self.assertTrue(release_failed_conversion_billing(record))
        release.assert_called_once_with(42, 'request-1', 'job-1')

    @patch('app.tasks.commit_conversion', return_value={})
    def test_successful_job_commits_completed_billing(self, commit):
        record = {
            'job_type': 'pattern_pdf_to_plt',
            'billing_access_method': 'points',
            'user_key': 'user:42',
            'billing_request_id': 'request-1',
            'job_id': 'job-1',
        }

        self.assertTrue(commit_successful_conversion_billing(record))
        commit.assert_called_once_with(42, 'request-1', 'job-1', completed=True)


if __name__ == '__main__':
    unittest.main()
