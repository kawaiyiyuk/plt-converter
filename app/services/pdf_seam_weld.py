"""Remove page frames and weld matching path ends along PDF tiling seams."""

import math


def _near_grid_line(value, lines, tolerance):
    return any(abs(value - line) <= tolerance for line in lines)


def remove_grid_frames(paths, grid, units_per_mm, tolerance_mm=0.03):
    """Drop paths whose every segment lies on a grid line."""
    tolerance = tolerance_mm * units_per_mm
    x_lines = grid.get('x') or []
    y_lines = grid.get('y') or []
    kept = []
    removed_count = 0
    for path in paths:
        is_frame = True
        for start, end in zip(path, path[1:]):
            horizontal = (
                abs(start['x'] - end['x']) <= tolerance
                and _near_grid_line(start['x'], x_lines, tolerance)
                and _near_grid_line(end['x'], x_lines, tolerance)
            )
            vertical = (
                abs(start['y'] - end['y']) <= tolerance
                and _near_grid_line(start['y'], y_lines, tolerance)
                and _near_grid_line(end['y'], y_lines, tolerance)
            )
            if not (horizontal or vertical):
                is_frame = False
                break
        if is_frame:
            removed_count += 1
        else:
            kept.append(path)
    return kept, removed_count


def weld_seam_paths(paths, grid, units_per_mm, seam_tolerance_mm=0.1, match_mm=0.1):
    """Join path ends that meet across a seam into single continuous paths."""
    paths = [list(path) for path in paths]
    alive = [True] * len(paths)
    seam_x = grid.get('seam_x') or []
    seam_y = grid.get('seam_y') or []
    seam_tolerance = seam_tolerance_mm * units_per_mm
    match_distance = match_mm * units_per_mm

    def seam_of(point):
        for seam in seam_x:
            if abs(point['x'] - seam) <= seam_tolerance:
                return ('x', seam)
        for seam in seam_y:
            if abs(point['y'] - seam) <= seam_tolerance:
                return ('y', seam)
        return None

    merged_count = 0
    changed = True
    while changed:
        changed = False
        ends = {}
        for index, path in enumerate(paths):
            if not alive[index]:
                continue
            if path[0] == path[-1] and len(path) > 2:
                continue
            for side in (0, 1):
                point = path[0] if side == 0 else path[-1]
                seam = seam_of(point)
                if seam:
                    ends.setdefault(seam, []).append((point, index, side))
        used = set()
        for _seam, lane in ends.items():
            for a in range(len(lane)):
                point_a, index_a, side_a = lane[a]
                if index_a in used:
                    continue
                distances = [
                    math.hypot(point['x'] - point_a['x'], point['y'] - point_a['y'])
                    for point, _index, _side in lane
                ]
                candidates = [
                    b for b in range(len(lane))
                    if b != a
                    and distances[b] <= match_distance
                    and lane[b][1] != index_a
                ]
                if len(candidates) != 1:
                    continue
                b = candidates[0]
                point_b, index_b, side_b = lane[b]
                reverse_candidates = [
                    c for c in range(len(lane))
                    if c != b
                    and math.hypot(
                        lane[c][0]['x'] - point_b['x'], lane[c][0]['y'] - point_b['y'],
                    ) <= match_distance
                    and lane[c][1] != index_b
                ]
                if len(reverse_candidates) != 1 or reverse_candidates[0] != a:
                    continue
                if index_b in used:
                    continue
                path_a = paths[index_a] if side_a == 1 else paths[index_a][::-1]
                path_b = paths[index_b] if side_b == 0 else paths[index_b][::-1]
                paths[index_a] = path_a + (
                    path_b[1:] if path_a[-1] == path_b[0] else path_b
                )
                alive[index_b] = False
                used.add(index_a)
                used.add(index_b)
                merged_count += 1
                changed = True

    return [path for index, path in enumerate(paths) if alive[index]], merged_count
