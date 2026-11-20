import hashlib
import unittest

from app.services.pattern_layout.parse_source import parse
from app.services.plt_parser import parse_plt

class TailPaddingProof(unittest.TestCase):
    clean = b'IN;SP1;PA;PU0,0;PD2000,0,2000,1600,0,1600,0,0;PU;PG;\r\n'
    
    def test_single_padding_after_complete_stream(self):
        raw = self.clean + b'\x00'
        result = parse(raw)
        reference = parse(self.clean)
        self.assertEqual(result['shapes'], reference['shapes'])
        self.assertEqual(result['metrics'], reference['metrics'])
        self.assertEqual(result['warnings'], reference['warnings'])
        self.assertEqual(result['source_sha256'], hashlib.sha256(raw).hexdigest())

    def test_single_padding_after_complete_pu_is_accepted(self):
        clean = b'IN;SP1;PA;PU0,0;PD2000,0,2000,1600,0,1600,0,0;PU;'
        raw = clean + b'\x00'
        result = parse(raw)
        reference = parse(clean)
        self.assertEqual(result['shapes'], reference['shapes'])
        self.assertEqual(result['metrics'], reference['metrics'])
        self.assertEqual(result['warnings'], reference['warnings'])
        self.assertEqual(result['source_sha256'], hashlib.sha256(raw).hexdigest())

    def test_nul_that_could_change_drawing_is_still_rejected(self):
        cases = [
            self.clean.replace(b'2000,0', b'20\x000,0', 1),
            self.clean + b'\x00\x00',
            self.clean + b'\x00\r\n',
            self.clean + b'PD2000,0\x00',
            self.clean + b'\x1a\x00',
        ]
        for raw in cases:
            for parser in (parse, lambda source: parse_plt(source, projection=True)):
                with self.subTest(raw=raw, parser=parser), self.assertRaisesRegex(ValueError, '无效空字符'):
                    parser(raw)

    def test_formal_conversion_and_projection_ignore_single_tail_padding(self):
        raw = self.clean + b'\x00'
        for options in ({}, {'projection': True}):
            with self.subTest(options=options):
                self.assertEqual(parse_plt(raw, **options), parse_plt(self.clean, **options))

    def test_strict_parser_still_rejects_null(self):
        with self.assertRaisesRegex(ValueError, '无效空字符'):
            parse_plt(self.clean + b'\x00', strict=True)

    def test_formal_conversion_keeps_legacy_null_handling(self):
        raw = self.clean.replace(b'2000,0', b'20\x0000,0', 1) + b'\x00\x00'
        self.assertEqual(parse_plt(raw), parse_plt(self.clean))
