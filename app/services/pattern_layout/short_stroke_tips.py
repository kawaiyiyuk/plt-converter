"""短起笔与短收笔的轮廓判断，原始笔画仍用于导出。"""
import math

from shapely.geometry import LineString, Polygon


def _is_short_turn(first, corner, third, max_length, epsilon, params):
    incoming = (corner[0] - first[0], corner[1] - first[1])
    outgoing = (third[0] - corner[0], third[1] - corner[1])
    if not epsilon < math.hypot(*incoming) <= max_length or math.hypot(*outgoing) <= epsilon:
        return False
    cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
    dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
    angle = math.degrees(math.atan2(abs(cross), dot))
    return params['short_tip_min_angle_deg'] < angle < params['short_tip_max_angle_deg']


def short_tip_outline(points, units, epsilon, params):
    """只返回唯一可成立的轮廓，端线的坐标与源片段保持原样。"""
    if len(points) < 5 or math.dist(points[0], points[-1]) <= params['near_closed_tolerance_mm']:
        return None
    max_length = params['short_tip_max_plotter_units'] / units + epsilon
    start_is_tip = _is_short_turn(*points[:3], max_length, epsilon, params)
    end_is_tip = _is_short_turn(*points[-3:][::-1], max_length, epsilon, params)

    def outline_after_separating(start, end):
        outline = points[start:len(points) - end]
        if len(outline) < 4:
            return None
        gap = math.dist(outline[0], outline[-1])
        if not epsilon < gap <= params['multi_stroke_join_tolerance_mm'] + epsilon:
            return None
        polygon = Polygon(outline)
        if not polygon.is_valid or polygon.area < params['min_candidate_area_mm2']:
            return None

        # 只把轮廓外的端段分开判断，片内线保留现有归属方式。
        tips = []
        if start:
            tips.append(('start', points[:2]))
        if end:
            tips.append(('end', points[-2:]))
        if any(LineString(segment).intersection(polygon).length > epsilon for _, segment in tips):
            return None
        return polygon, gap, [side for side, _ in tips]

    single_end = []
    if start_is_tip:
        single_end.append(outline_after_separating(1, 0))
    if end_is_tip:
        single_end.append(outline_after_separating(0, 1))
    valid = [candidate for candidate in single_end if candidate is not None]
    if valid:
        return valid[0] if len(valid) == 1 else None
    if start_is_tip and end_is_tip:
        return outline_after_separating(1, 1)
    return None
