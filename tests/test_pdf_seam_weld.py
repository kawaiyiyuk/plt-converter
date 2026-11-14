import unittest

from app.services.pdf_seam_weld import remove_grid_frames, weld_seam_paths


UNITS_PER_MM = 1016 / 25.4


def point(x, y):
    return {'x': x, 'y': y}


def grid(x_seams=(), y_seams=(), outer_x=(), outer_y=()):
    return {
        'x': list(outer_x),
        'y': list(outer_y),
        'seam_x': list(x_seams),
        'seam_y': list(y_seams),
    }


class RemoveGridFramesTest(unittest.TestCase):
    def test_removes_only_paths_whose_every_segment_lies_on_a_grid_line(self):
        paths = [
            [point(100, 0), point(100, 100)],
            [point(0, 100), point(100, 100)],
            [point(100, 0), point(110, 50), point(100, 100)],
            [point(100, 0), point(103, 100)],
        ]

        kept, removed = remove_grid_frames(
            paths, grid(outer_x=(0, 100, 200), outer_y=(0, 100)), UNITS_PER_MM
        )

        self.assertEqual(removed, 2)
        self.assertEqual(kept, paths[2:])

    def test_off_grid_middle_segment_keeps_the_whole_path(self):
        paths = [[point(100, 0), point(100, 50), point(120, 50), point(120, 100)]]

        kept, removed = remove_grid_frames(
            paths, grid(outer_x=(100, 120), outer_y=(0, 100)), UNITS_PER_MM
        )

        self.assertEqual(removed, 0)
        self.assertEqual(kept, paths)

    def test_tolerance_is_millimetres_on_both_axes(self):
        near = UNITS_PER_MM * 0.02
        paths = [[point(100 + near, 0), point(100 + near, 100)]]

        kept, removed = remove_grid_frames(
            paths, grid(outer_x=(100,), outer_y=(0, 100)), UNITS_PER_MM
        )

        self.assertEqual(removed, 1)
        self.assertEqual(kept, [])


class WeldSeamPathsTest(unittest.TestCase):
    def test_welds_two_paths_that_meet_across_a_seam_without_moving_points(self):
        first = [point(0, 50), point(100, 50)]
        second = [point(100, 50), point(200, 50)]

        welded, merged = weld_seam_paths(
            [first, second], grid(x_seams=(100,)), UNITS_PER_MM
        )

        self.assertEqual(merged, 1)
        self.assertEqual(welded, [[point(0, 50), point(100, 50), point(200, 50)]])
        self.assertEqual(first, [point(0, 50), point(100, 50)])

    def test_reversed_orientation_is_joined_in_pen_order(self):
        first = [point(0, 50), point(100, 50)]
        second = [point(200, 50), point(100, 50)]

        welded, merged = weld_seam_paths(
            [first, second], grid(x_seams=(100,)), UNITS_PER_MM
        )

        self.assertEqual(merged, 1)
        self.assertEqual(welded, [[point(0, 50), point(100, 50), point(200, 50)]])

    def test_one_to_many_endpoint_is_not_welded(self):
        paths = [
            [point(0, 50), point(100, 50)],
            [point(100, 50), point(200, 50)],
            [point(100, 51), point(200, 60)],
        ]

        welded, merged = weld_seam_paths(paths, grid(x_seams=(100,)), UNITS_PER_MM)

        self.assertEqual(merged, 0)
        self.assertEqual(welded, paths)

    def test_endpoint_outside_match_distance_is_not_welded(self):
        paths = [
            [point(0, 50), point(100, 50)],
            [point(105, 50), point(200, 50)],
        ]

        welded, merged = weld_seam_paths(paths, grid(x_seams=(100,)), UNITS_PER_MM)

        self.assertEqual(merged, 0)
        self.assertEqual(welded, paths)

    def test_two_ends_of_the_same_path_are_never_welded_together(self):
        paths = [[point(100, 50), point(100, 51)]]

        welded, merged = weld_seam_paths(paths, grid(x_seams=(100,)), UNITS_PER_MM)

        self.assertEqual(merged, 0)
        self.assertEqual(welded, paths)

    def test_closed_path_does_not_participate(self):
        closed = [point(100, 50), point(150, 60), point(100, 50)]
        other = [point(100, 50), point(200, 60)]

        welded, merged = weld_seam_paths(
            [closed, other], grid(x_seams=(100,)), UNITS_PER_MM
        )

        self.assertEqual(merged, 0)
        self.assertIn(closed, welded)
        self.assertIn(other, welded)

    def test_x_seam_is_checked_before_y_seam(self):
        paths = [
            [point(0, 100), point(100, 100)],
            [point(100, 100), point(200, 100)],
        ]

        welded, merged = weld_seam_paths(
            paths, grid(x_seams=(100,), y_seams=(100,)), UNITS_PER_MM
        )

        self.assertEqual(merged, 1)
        self.assertEqual(welded, [[point(0, 100), point(100, 100), point(200, 100)]])


if __name__ == '__main__':
    unittest.main()
