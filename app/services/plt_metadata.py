import os
import math
import re

from .number_tokens import iter_number_tokens
from .plt_parser import decode_plt


COMMAND_RE = re.compile(
    r'(LB)([^\x03;]*)(?:\x03|;|\Z)|([A-Za-z]{2})([^;]*)(?:;|\Z)',
    re.IGNORECASE,
)
COORDINATE_COMMANDS = {'PU', 'PD', 'PA', 'PR'}


def inspect_plt(source, units_per_inch=1016):
    if not isinstance(source, (bytes, bytearray)) or not source:
        raise ValueError('PLT 文件为空')
    try:
        unit_scale = float(units_per_inch)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('units_per_inch 参数无效') from error
    if not math.isfinite(unit_scale) or unit_scale <= 0:
        raise ValueError('units_per_inch 必须是大于 0 的有限数值')

    text = decode_plt(source).replace('\x00', '')
    current_x = 0.0
    current_y = 0.0
    mode = 'absolute'
    pen_down = False
    min_x = float('inf')
    min_y = float('inf')
    max_x = float('-inf')
    max_y = float('-inf')
    command_count = 0
    coordinate_command_count = 0
    point_count = 0
    path_count = 0
    shape_count = 0
    stroke_point_count = 0
    text_char_count = 0

    maximum_points = max(1000, int(os.getenv('PLT_MAX_POINTS', '500000')))
    maximum_paths = max(100, int(os.getenv('PLT_MAX_PATHS', '100000')))
    maximum_text_chars = max(1000, int(os.getenv('PLT_MAX_TEXT_CHARS', '100000')))

    def register_shape(is_path=False):
        nonlocal path_count, shape_count
        shape_count += 1
        if shape_count > maximum_paths:
            raise ValueError(f'PLT 路径过多，最多支持 {maximum_paths} 条')
        if is_path:
            path_count += 1

    def flush_stroke():
        nonlocal stroke_point_count
        if stroke_point_count > 1:
            register_shape(is_path=True)
        stroke_point_count = 0

    def add_absolute_point(x, y):
        nonlocal current_x, current_y, min_x, min_y, max_x, max_y
        nonlocal point_count, stroke_point_count
        start_x = current_x
        start_y = current_y
        current_x = x
        current_y = y
        if pen_down:
            if stroke_point_count == 0:
                min_x = min(min_x, start_x)
                min_y = min(min_y, start_y)
                max_x = max(max_x, start_x)
                max_y = max(max_y, start_y)
                stroke_point_count = 1
            min_x = min(min_x, current_x)
            min_y = min(min_y, current_y)
            max_x = max(max_x, current_x)
            max_y = max(max_y, current_y)
            stroke_point_count += 1
        point_count += 1
        if point_count > maximum_points:
            raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')

    def add_arc(center_x, center_y, angle_degrees, chord_angle_degrees=5):
        radius = math.hypot(current_x - center_x, current_y - center_y)
        if radius <= 0 or not math.isfinite(radius) or not math.isfinite(angle_degrees):
            return
        start_angle = math.atan2(current_y - center_y, current_x - center_x)
        chord_angle = max(abs(float(chord_angle_degrees or 5)), 0.1)
        steps = max(1, math.ceil(abs(angle_degrees) / chord_angle))
        if point_count + steps > maximum_points:
            raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
        total_angle = math.radians(angle_degrees)
        for step in range(1, steps + 1):
            theta = start_angle + total_angle * step / steps
            add_absolute_point(
                center_x + math.cos(theta) * radius,
                center_y + math.sin(theta) * radius,
            )

    for match in COMMAND_RE.finditer(text):
        label_command, label_text, regular_command, regular_arguments = match.groups()
        raw_command = label_command or regular_command
        argument_text = label_text if label_command else regular_arguments
        command_count += 1
        if command_count > max(1000, int(os.getenv('PLT_MAX_COMMANDS', '250000'))):
            raise ValueError('PLT 命令数量超过服务器限制')
        command = raw_command.upper()
        if command == 'LB':
            if argument_text:
                text_char_count += len(argument_text)
                if text_char_count > maximum_text_chars:
                    raise ValueError(
                        f'PLT 文本内容过多，最多支持 {maximum_text_chars} 个字符'
                    )
                min_x = min(min_x, current_x)
                min_y = min(min_y, current_y)
                max_x = max(max_x, current_x)
                max_y = max(max_y, current_y)
                register_shape()
            continue
        if command == 'IN':
            flush_stroke()
            current_x = 0.0
            current_y = 0.0
            mode = 'absolute'
            pen_down = False
            continue
        if command == 'CI':
            flush_stroke()
            numbers = _parse_numbers(argument_text, maximum_values=10000)
            radius = numbers[0] if numbers else 0
            if radius > 0:
                min_x = min(min_x, current_x - radius)
                min_y = min(min_y, current_y - radius)
                max_x = max(max_x, current_x + radius)
                max_y = max(max_y, current_y + radius)
                register_shape()
            continue
        if command in {'AA', 'AR'}:
            numbers = _parse_numbers(argument_text, maximum_values=10000)
            if len(numbers) >= 3:
                center_x = numbers[0]
                center_y = numbers[1]
                if command == 'AR':
                    center_x += current_x
                    center_y += current_y
                add_arc(
                    center_x,
                    center_y,
                    numbers[2],
                    numbers[3] if len(numbers) > 3 else 5,
                )
            continue
        if command not in COORDINATE_COMMANDS:
            if command == 'SP':
                flush_stroke()
            continue

        numbers = _parse_numbers(
            argument_text,
            maximum_values=max((maximum_points - point_count) * 2, 0),
        )
        if command == 'PU':
            flush_stroke()
            pen_down = False
        elif command == 'PD':
            pen_down = True
        elif command == 'PA':
            mode = 'absolute'
        elif command == 'PR':
            mode = 'relative'
        if len(numbers) < 2:
            continue
        coordinate_command_count += 1

        for index in range(0, len(numbers) - 1, 2):
            x, y = numbers[index], numbers[index + 1]
            start_x = current_x
            start_y = current_y
            if mode == 'relative':
                current_x += x
                current_y += y
            else:
                current_x = x
                current_y = y
            if pen_down:
                if stroke_point_count == 0:
                    min_x = min(min_x, start_x)
                    min_y = min(min_y, start_y)
                    max_x = max(max_x, start_x)
                    max_y = max(max_y, start_y)
                    stroke_point_count = 1
                min_x = min(min_x, current_x)
                min_y = min(min_y, current_y)
                max_x = max(max_x, current_x)
                max_y = max(max_y, current_y)
                stroke_point_count += 1
            point_count += 1
            if point_count > maximum_points:
                raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')

    flush_stroke()
    if not math.isfinite(min_x):
        raise ValueError('没有解析到有效的 PLT 坐标')

    unit_to_mm = 25.4 / unit_scale
    width_mm = (max_x - min_x) * unit_to_mm
    height_mm = (max_y - min_y) * unit_to_mm
    if not math.isfinite(width_mm) or not math.isfinite(height_mm):
        raise ValueError('PLT 尺寸无效')
    return {
        'units_per_inch': units_per_inch,
        'min_x': min_x,
        'min_y': min_y,
        'max_x': max_x,
        'max_y': max_y,
        'width_mm': round(width_mm, 3),
        'height_mm': round(height_mm, 3),
        'point_count': point_count,
        'path_count': path_count,
        'coordinate_command_count': coordinate_command_count,
        'command_count': command_count,
    }


def _parse_numbers(text, maximum_values=None):
    if not text.strip():
        return []
    values = []
    for value in iter_number_tokens(text):
        if not value:
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if not math.isfinite(number):
            continue
        values.append(number)
        if maximum_values is not None and len(values) > maximum_values:
            raise ValueError('PLT 命令参数数量超过服务器限制')
    return values
