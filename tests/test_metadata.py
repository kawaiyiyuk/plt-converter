import unittest
from unittest.mock import patch

from app.services.plt_metadata import inspect_plt


class PltMetadataTest(unittest.TestCase):
    def test_reads_absolute_coordinates(self):
        metadata = inspect_plt(
            b'IN;PU0,0;PD1016,0,1016,2032,0,2032,0,0;'
        )

        self.assertEqual(metadata['point_count'], 5)
        self.assertEqual(metadata['path_count'], 1)
        self.assertAlmostEqual(metadata['width_mm'], 25.4, places=3)
        self.assertAlmostEqual(metadata['height_mm'], 50.8, places=3)

    def test_rejects_command_complexity(self):
        source = b''.join(b'PU0,0;' for _ in range(1000)) + b'PD1,1;'
        with patch.dict('os.environ', {'PLT_MAX_COMMANDS': '1000'}):
            with self.assertRaisesRegex(ValueError, '命令数量'):
                inspect_plt(source)

    def test_rejects_point_complexity_before_building_unbounded_list(self):
        coordinates = ','.join(f'{index},{index}' for index in range(1001))
        with patch.dict('os.environ', {'PLT_MAX_POINTS': '1000'}):
            with self.assertRaisesRegex(ValueError, '参数数量|坐标点过多'):
                inspect_plt(f'IN;PD{coordinates};'.encode())

    def test_ignores_non_finite_coordinates(self):
        metadata = inspect_plt(b'IN;PU0,0;PDNaN,1,Infinity,2,1016,1016;')

        self.assertTrue(all(
            value == value and value not in (float('inf'), float('-inf'))
            for value in (metadata['min_x'], metadata['min_y'], metadata['max_x'], metadata['max_y'])
        ))


if __name__ == '__main__':
    unittest.main()
