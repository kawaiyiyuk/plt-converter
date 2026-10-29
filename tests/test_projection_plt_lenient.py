"""Projection tolerance is independent from projection cursor/safety semantics."""
import unittest
from unittest.mock import patch

import test_projection_plt as fixtures
from app.services.plt_parser import parse_plt


class LenientProjectionTests(unittest.TestCase):
    setUp = fixtures.ProjectionPltTests.setUp
    identity = fixtures.ProjectionPltTests.identity
    upload = fixtures.ProjectionPltTests.upload
    def test_setup_and_unknown_geometry_upload_with_warning(self):
        for command in ('WU0', 'SC0,100,0,100'):
            with self.subTest(command=command):
                response = self.upload(('IN;SP1;' + command + ';PU0,0;PD4000,2000;').encode(), 'lenient.plt')
                self.assertEqual(response.status_code, 201, response.get_json())
                body = response.get_json()
                self.assertAlmostEqual(body['width_mm'], 100, places=3)
                self.assertTrue(any(command[:2] in w for w in body['rendering_warnings']))

    def test_malformed_parameters_warn_without_losing_projection_cursor(self):
        result = parse_plt('IN;SP1;PU0,0;PD100,bad,0,200;LBAB\x03;PR400,200;PD400,200;', projection=True)
        self.assertTrue(any('参数' in w for w in result['warnings']))
        self.assertTrue(any('末尾' in w for w in result['warnings']))
        self.assertTrue(any('8pt' in w for w in result['warnings']))
        self.assertAlmostEqual(result['shapes'][-1]['points'][-1]['x'], 900 + 16 * 1016 / 72)

    def test_pure_strict_api_still_rejects_unknown_or_malformed(self):
        for source in ('IN;WU0;PD100,100;', 'IN;PD1,bad,2;'):
            with self.assertRaises(ValueError):
                parse_plt(source, strict=True)

    def test_warning_summary_is_bounded_and_sp0_is_not_legacy(self):
        names = [chr(a) + chr(b) for a in range(65, 91) for b in range(65, 91)]
        source = ';'.join(name + '0' for name in names if name not in {'IN','SP','PA','PR','PU','PD','CI','AA','AR','PW','VS','LB'})
        result = parse_plt(source + ';SP1;PU0,0;PD100,100;SP0;PD9000,9000;', projection=True)
        self.assertLess(sum(map(len, result['warnings'])), 2000)
        self.assertEqual(result['metrics']['max_x'], 100)

    def test_projection_keeps_rendering_semantics_and_null_protection(self):
        source = 'IN;SP1;PU1000,0;PD;AA0,0,90;LBAB\x03;PR400,200;PD400,200;SP0;PD9000,9000;SP2;PU0,0;CI500;'
        strict = parse_plt(source, strict=True, max_chord_error_mm=0.049)
        compatible = parse_plt(source, projection=True, max_chord_error_mm=0.049)
        self.assertEqual(strict, compatible)
        with self.assertRaisesRegex(ValueError, '空字符'):
            parse_plt('IN;PD100,100;\x00', projection=True)
        defaults = parse_plt('IN;PW;VS;PD100,100;', projection=True)
        self.assertFalse(any('数量' in w for w in defaults['warnings']))

    def test_projection_hard_caps_and_cumulative_finite_remain(self):
        with patch.dict('os.environ', {'PLT_MAX_POINTS': '1000'}):
            with self.assertRaisesRegex(ValueError, '超过服务器限制|坐标点过多'):
                parse_plt('IN;PD' + ','.join('1,1' for _ in range(1001)) + ';', projection=True)
        with self.assertRaisesRegex(ValueError, '有限'):
            parse_plt('IN;PR1e308,0;PD1e308,1;', projection=True)


if __name__ == '__main__':
    unittest.main()
