import io
import unittest
from unittest.mock import patch

import pymupdf

from app import create_app
from app.services.pdf_metadata import encode_pdf_layout_metadata
from app.services.pdf_to_plt import convert_pdf_to_plt, read_pdf_layout_metadata
from app.services.pdf_to_pdf import convert_pdf_to_pdf
from app.services.pdf_layout_optimizer import optimize_pdf_layout
from app.services.plt_parser import parse_plt


def sample_pdf(with_metadata=True, complete=True):
    document = pymupdf.open()
    page = document.new_page(width=300, height=300)
    page.draw_line((40, 60), (240, 60), color=(1, 0, 0))
    page.draw_line((40, 150), (240, 150), color=(0, 0, 0))
    if with_metadata:
        metadata = {
            'version': 1, 'rows': 1, 'columns': 1, 'order': 'row',
            'page_slots': [0],
            'crop_margins_mm': {'top': 0, 'right': 0, 'bottom': 0, 'left': 0},
            'drawing_width_mm': 105.8333, 'drawing_height_mm': 105.8333,
            'complete_layout': complete,
        }
        document.set_metadata({'subject': encode_pdf_layout_metadata(metadata)})
    source = document.tobytes()
    document.close()
    return source


class PdfMetadataChoiceTest(unittest.TestCase):
    def test_current_keeps_red_line_and_original_filters_it(self):
        source = sample_pdf()
        with self.assertRaisesRegex(ValueError, '先选择'):
            convert_pdf_to_plt(source, {'rows': 1, 'columns': 1})
        original, _ = convert_pdf_to_plt(source, {'metadata_mode': 'original'})
        current, _ = convert_pdf_to_plt(source, {'metadata_mode': 'current'})
        self.assertEqual(len(parse_plt(original)['shapes']), 1)
        self.assertEqual(len(parse_plt(current)['shapes']), 2)

    def test_incomplete_valid_metadata_still_requires_choice(self):
        source = sample_pdf(complete=False)
        self.assertIsNotNone(read_pdf_layout_metadata(source))
        with self.assertRaisesRegex(ValueError, '先选择'):
            convert_pdf_to_plt(source)
        self.assertEqual(len(parse_plt(convert_pdf_to_plt(source, {'metadata_mode': 'current'})[0])['shapes']), 2)

    def test_plain_pdf_uses_existing_path_and_invalid_mode_is_rejected(self):
        source = sample_pdf(with_metadata=False)
        self.assertEqual(len(parse_plt(convert_pdf_to_plt(source)[0])['shapes']), 2)
        with self.assertRaisesRegex(ValueError, 'original 或 current'):
            convert_pdf_to_plt(source, {'metadata_mode': 'guess'})

    def test_optimizer_and_paper_conversion_respect_mode(self):
        source = sample_pdf()
        with self.assertRaisesRegex(ValueError, '先选择'):
            optimize_pdf_layout(source)
        self.assertEqual(optimize_pdf_layout(source, metadata_mode='original')['source'], 'embedded_metadata')
        self.assertNotEqual(optimize_pdf_layout(source, metadata_mode='current').get('source'), 'embedded_metadata')
        with self.assertRaisesRegex(ValueError, '先选择'):
            convert_pdf_to_pdf(source, {'paper_size': 'A4'})
        original, original_result = convert_pdf_to_pdf(source, {'paper_size': 'A4', 'metadata_mode': 'original'})
        current, current_result = convert_pdf_to_pdf(source, {'paper_size': 'A4', 'metadata_mode': 'current'})
        self.assertTrue(original.startswith(b'%PDF'))
        self.assertTrue(current.startswith(b'%PDF'))
        self.assertEqual(original_result['layout_source'], 'embedded_metadata')
        self.assertEqual(current_result['layout_source'], 'single_page')
        with pymupdf.open(stream=original, filetype='pdf') as original_document, \
                pymupdf.open(stream=current, filetype='pdf') as current_document:
            original_lines = original_document[0].get_drawings()[0]['items']
            current_lines = current_document[0].get_drawings()[0]['items']
            self.assertGreater(len(current_lines), len(original_lines))

    def test_routes_reject_missing_choice_before_billing(self):
        source = sample_pdf()
        app = create_app()
        with patch('app.routes.enforce_upload_limits'), patch('app.routes.authorize_job') as authorize:
            pdf_to_plt = app.test_client().post(
                '/api/v1/pdf/jobs',
                data={'file': (io.BytesIO(source), 'sample.pdf')},
            )
            pdf_to_pdf = app.test_client().post(
                '/api/v1/pdf/repage/jobs',
                data={'file': (io.BytesIO(source), 'sample.pdf'), 'paper_size': 'A4'},
            )
        self.assertEqual(pdf_to_plt.status_code, 422)
        self.assertEqual(pdf_to_pdf.status_code, 422)
        authorize.assert_not_called()

    def test_paper_inspection_reports_valid_metadata_choice(self):
        app = create_app()
        with patch('app.routes.enforce_upload_limits'), \
                patch('app.routes.store_source_upload', return_value={'job_id': 'source-id'}):
            client = app.test_client()
            with_metadata = client.post(
                '/api/v1/pdf/repage/inspect',
                data={'file': (io.BytesIO(sample_pdf(complete=False)), 'sample.pdf')},
            )
            plain = client.post(
                '/api/v1/pdf/repage/inspect',
                data={'file': (io.BytesIO(sample_pdf(with_metadata=False)), 'plain.pdf')},
            )
        self.assertEqual(with_metadata.status_code, 200)
        self.assertTrue(with_metadata.get_json()['requires_metadata_choice'])
        self.assertEqual(plain.status_code, 200)
        self.assertFalse(plain.get_json()['requires_metadata_choice'])


if __name__ == '__main__':
    unittest.main()
