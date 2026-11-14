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
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, '无效空字符'):
                parse(raw)

    def test_other_entrypoints_unchanged(self):
        raw = self.clean + b'\x00'
        self.assertEqual(parse_plt(raw), parse_plt(self.clean))
        with self.assertRaisesRegex(ValueError, '无效空字符'):
            parse_plt(raw, projection=True)
