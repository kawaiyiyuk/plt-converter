import re
import unittest
import zlib
from unittest.mock import patch

import pymupdf

from app.services.pdf_renderer import render_pdf, utf16be_hex
from app.services.plt_parser import parse_plt


class PdfRendererTest(unittest.TestCase):
    @staticmethod
    def decoded_streams(pdf):
        streams = re.findall(
            rb'<< /Length \d+ /Filter /FlateDecode >>\nstream\n(.*?)\nendstream',
            pdf,
            flags=re.DOTALL,
        )
        return b'\n'.join(zlib.decompress(stream) for stream in streams)

    def test_renders_tiled_pdf_and_selected_pages(self):
        document = parse_plt(
            b'IN;PU0,0;PD1016,0,1016,2032,0,2032,0,0;'
        )
        pdf, layout = render_pdf(document, {
            'paper_size': 'A4',
            'orientation': 'portrait',
            'margin_mm': 10,
            'enabled_pages': [0],
        })

        self.assertEqual(layout['page_count'], 1)
        self.assertEqual(pdf[:8], b'%PDF-1.4')
        self.assertIn(b'/Type /Catalog', pdf)
        self.assertIn(b'/MediaBox', pdf)
        self.assertIn(b'/Filter /FlateDecode', pdf)
        content = self.decoded_streams(pdf)
        self.assertIn(f'<{utf16be_hex("1-1")}> Tj'.encode(), content)
        self.assertIn(f'<{utf16be_hex("A4 100% | 1-1 | 1/1 | plt-guide-v1")}> Tj'.encode(), content)
        self.assertIn(b'/F1 48.000 Tf', content)
        self.assertIn(b'1 0 0 RG', content)
        self.assertIn(f'<{utf16be_hex("5 cm / 50 mm")}> Tj'.encode(), content)
        self.assertNotIn(b'(1-1) Tj', content)
        with pymupdf.open(stream=pdf, filetype='pdf') as rendered:
            self.assertEqual(rendered.page_count, 1)
            self.assertAlmostEqual(rendered[0].rect.width, layout['page_width_pt'], places=2)
            self.assertAlmostEqual(rendered[0].rect.height, layout['page_height_pt'], places=2)

    def test_rejects_page_count_before_rendering(self):
        document = parse_plt(b'IN;PU0,0;PD50000,50000;')
        with patch.dict('os.environ', {'PLT_MAX_OUTPUT_PAGES': '1'}):
            with self.assertRaisesRegex(ValueError, '输出页数'):
                render_pdf(document, {
                    'paper_size': 'A4',
                    'orientation': 'portrait',
                    'margin_mm': 10,
                })

    def test_rejects_when_all_pages_are_disabled(self):
        document = parse_plt(b'IN;PU0,0;PD1016,1016;')
        with self.assertRaisesRegex(ValueError, '至少保留一个输出页面'):
            render_pdf(document, {
                'paper_size': 'A4',
                'orientation': 'portrait',
                'margin_mm': 10,
                'enabled_pages': [],
            })

    def test_single_page_output_rejects_disabled_page(self):
        document = parse_plt(b'IN;PU0,0;PD1016,1016;')
        with self.assertRaisesRegex(ValueError, '至少保留一个输出页面'):
            render_pdf(document, {
                'single_page_output': True,
                'enabled_pages': [],
            })


if __name__ == '__main__':
    unittest.main()
