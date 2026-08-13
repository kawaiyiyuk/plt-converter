import os
import unittest
from unittest.mock import patch

from app.services.plt_parser import parse_plt


class PltParserLimitsTest(unittest.TestCase):
    def test_rejects_too_many_commands_during_parse(self):
        source = b''.join(b'PU0,0;' for _ in range(1001)) + b'PD1,1;'
        with patch.dict(os.environ, {'PLT_MAX_COMMANDS': '1000'}):
            with self.assertRaisesRegex(ValueError, '命令数量'):
                parse_plt(source)

    def test_rejects_too_many_points_during_parse(self):
        coordinates = ','.join(f'{index},{index}' for index in range(1001))
        with patch.dict(os.environ, {'PLT_MAX_POINTS': '1000'}):
            with self.assertRaisesRegex(ValueError, '参数数量|坐标点过多'):
                parse_plt(f'IN;PD{coordinates};')

    def test_rejects_too_many_values_in_one_command(self):
        coordinates = ','.join('1' for _ in range(2001))
        with patch.dict(os.environ, {'PLT_MAX_POINTS': '1000'}):
            with self.assertRaisesRegex(ValueError, '参数数量'):
                parse_plt(f'IN;PD{coordinates};')

    def test_rejects_too_much_label_text(self):
        with patch.dict(os.environ, {'PLT_MAX_TEXT_CHARS': '1000'}):
            with self.assertRaisesRegex(ValueError, '文本内容'):
                parse_plt('IN;PU0,0;LB' + ('a' * 1001) + '\x03')

    def test_rejects_arc_before_expanding_too_many_points(self):
        with patch.dict(os.environ, {'PLT_MAX_POINTS': '1000'}):
            with self.assertRaisesRegex(ValueError, '坐标点过多'):
                parse_plt('IN;PU100,0;PD;AA0,0,360,0.1;')


if __name__ == '__main__':
    unittest.main()
