import unittest
from unittest.mock import patch

from app.services.pdf_renderer import render_pdf, utf16be_hex
from app.services.plt_parser import parse_plt


class PdfRendererTest(unittest.TestCase):
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
        self.assertIn(f'<{utf16be_hex("1-1")}> Tj'.encode(), pdf)
        self.assertIn(f'<{utf16be_hex("A4 100% | 1-1 | 1/1 | plt-guide-v1")}> Tj'.encode(), pdf)
        self.assertIn(b'/F1 48.000 Tf', pdf)
        self.assertIn(b'1 0 0 RG', pdf)
        self.assertIn(f'<{utf16be_hex("5 cm / 50 mm")}> Tj'.encode(), pdf)
        self.assertNotIn(b'(1-1) Tj', pdf)

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
