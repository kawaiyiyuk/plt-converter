import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app

from app.job_queue import JOB_OUTPUT_VERSIONS
from app.services.pdf_to_plt import inspect_pdf, load_fitz, transparent_preview


class PdfPreviewAlphaTest(unittest.TestCase):
    def test_explicit_white_background_and_antialias_keep_white_page_appearance(self):
        fitz = load_fitz()
        document = fitz.open()
        page = document.new_page(width=240, height=240)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
        for y, color in ((40, (0, 0, 0)), (90, (.5, .5, .5)), (140, (.8, .2, .1))):
            page.draw_line((30.2, y + .3), (190.7, y + 25.4), color=color, width=1.3)
        original = page.get_pixmap(alpha=False)
        source = document.tobytes()
        with tempfile.TemporaryDirectory() as folder:
            pages = inspect_pdf(source, folder, 'alpha')
            thumbnail = fitz.Pixmap(str(Path(folder) / pages[0]['preview_name']))
            self.assertEqual(thumbnail.alpha, 1)
            self.assertEqual(thumbnail.pixel(5, 5), (0, 0, 0, 0))
            composite = fitz.open()
            target = composite.new_page(width=240, height=240)
            target.insert_image(target.rect, filename=str(Path(folder) / pages[0]['preview_name']))
            restored = target.get_pixmap(alpha=False)
            # PNG encode/decode and compositing may round up to two RGB units.
            self.assertLessEqual(max(abs(a - b) for a, b in zip(original.samples, restored.samples)), 2)
            rgba = transparent_preview(original, fitz).samples
            rgb = original.samples
            for offset in range(0, len(rgba), 4):
                red, green, blue, alpha = rgba[offset:offset + 4]
                start = offset // 4 * 3
                self.assertEqual((red + 255 - alpha, green + 255 - alpha, blue + 255 - alpha),
                                 tuple(rgb[start:start + 3]))
            alphas = thumbnail.samples[3::4]
            self.assertTrue(any(0 < alpha < 255 for alpha in alphas))
            editor = fitz.Pixmap(str(Path(folder) / pages[0]['editor_preview_name']))
            self.assertEqual(editor.alpha, 0)
            composite.close()
        document.close()

    def test_diagonal_page_overlap_preserves_both_page_lines(self):
        fitz = load_fitz()
        pt = 72 / 25.4
        document = fitz.open()
        first = document.new_page(width=100 * pt, height=100 * pt)
        first.draw_rect(first.rect, color=None, fill=(1, 1, 1))
        first.draw_line((85 * pt, 90 * pt), (95 * pt, 90 * pt), width=pt)
        second = document.new_page(width=100 * pt, height=100 * pt)
        second.draw_rect(second.rect, color=None, fill=(1, 1, 1))
        second.draw_line((10 * pt, 5 * pt), (10 * pt, 15 * pt), width=pt)
        with tempfile.TemporaryDirectory() as folder:
            pages = inspect_pdf(document.tobytes(), folder, 'overlap')
            composite = fitz.open()
            target = composite.new_page(width=180 * pt, height=180 * pt)
            for item, offset in zip(pages, (0, 80)):
                target.insert_image(fitz.Rect(offset * pt, offset * pt, (offset + 100) * pt, (offset + 100) * pt),
                                    filename=str(Path(folder) / item['preview_name']))
            rendered = target.get_pixmap(matrix=fitz.Matrix(3.6 / pt, 3.6 / pt), alpha=False)
            for x, y in ((86, 90), (90, 86)):
                with self.subTest(x=x, y=y):
                    # The source thumbnail is capped at its native point resolution;
                    # use a visible-ink threshold instead of an exact black pixel.
                    self.assertLess(max(rendered.pixel(round(x * 3.6), round(y * 3.6))), 160)
            composite.close()
        document.close()

    def test_old_job_url_bypasses_cache_and_lazily_converts_only_requested_png(self):
        fitz = load_fitz()
        with tempfile.TemporaryDirectory() as folder:
            preview_root = Path(folder) / 'previews'
            preview_root.mkdir()
            image = preview_root / 'old-job-0.png'
            original = fitz.Pixmap(fitz.csRGB, 2, 1, bytes((255, 255, 255, 50, 100, 150)), False)
            original.save(str(image))
            saved = image.read_bytes()
            record = {
                'job_id': 'old-job', 'job_type': 'pdf_preview', 'status': 'done',
                'user_key': 'owner', 'output_version': '3-metadata-choice',
                'input_path': str(Path(folder) / 'input.pdf'),
                'result': {'pages': [{'index': 0}, {'index': 1}], 'requires_metadata_choice': False},
            }
            # No input PDF or second PNG exists: GET must only process requested PNG.
            with patch('app.routes.load_job', return_value=record), \
                    patch('app.routes.request_user_key', return_value='owner'):
                client = create_app().test_client()
                response = client.get('/api/v1/pdf/preview/jobs/old-job')
                self.assertEqual(response.status_code, 200)
                url = response.get_json()['pages'][0]['preview_path']
                self.assertIn('v=4-transparent-thumbnails', url)
                allowed = client.get(url)
                self.assertEqual(allowed.status_code, 200)
                png = fitz.Pixmap(allowed.data)
                self.assertEqual(png.alpha, 1)
                self.assertEqual(png.pixel(0, 0), (0, 0, 0, 0))
                self.assertEqual(image.read_bytes(), saved)
                with patch('app.routes.request_user_key', return_value='other'):
                    self.assertEqual(client.get(url).status_code, 403)
                self.assertEqual(client.get('/api/v1/pdf/previews/old-job/2.png').status_code, 404)
                # New jobs already have RGBA PNGs and require no request-time decode.
                png.save(str(image))
                record['output_version'] = JOB_OUTPUT_VERSIONS['pdf_preview']
                with patch('app.routes.load_fitz', side_effect=AssertionError('unneeded decode')):
                    current = client.get(url)
                    self.assertEqual(current.status_code, 200)
                    self.assertEqual(current.data, image.read_bytes())
                    current.close()

    def test_opaque_preview_cache_version_is_retired(self):
        self.assertEqual(JOB_OUTPUT_VERSIONS['pdf_preview'], '4-transparent-thumbnails')
