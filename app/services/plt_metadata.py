import os
import math
import re

from .number_tokens import iter_number_tokens


COMMAND_RE = re.compile(r'([A-Za-z]{2})([^;]*);')
COORDINATE_COMMANDS = {'PU', 'PD', 'PA', 'PR'}


def inspect_plt(source, units_per_inch=1016):
    if not isinstance(source, (bytes, bytearray)) or not source:
        raise ValueError('PLT 文件为空')
    if units_per_inch <= 0:
        raise ValueError('units_per_inch 必须大于 0')

    text = bytes(source).decode('latin1', errors='replace')
    current_x = 0.0
    current_y = 0.0
    mode = 'absolute'
    min_x = float('inf')
    min_y = float('inf')
    max_x = float('-inf')
    max_y = float('-inf')
    command_count = 0
    coordinate_command_count = 0
    point_count = 0
    path_count = 0

    maximum_points = max(1000, int(os.getenv('PLT_MAX_POINTS', '500000')))
    maximum_paths = max(100, int(os.getenv('PLT_MAX_PATHS', '100000')))

    for match in COMMAND_RE.finditer(text):
        raw_command, argument_text = match.groups()
        command_count += 1
        if command_count > max(1000, int(os.getenv('PLT_MAX_COMMANDS', '250000'))):
            raise ValueError('PLT 命令数量超过服务器限制')
        command = raw_command.upper()
        if command not in COORDINATE_COMMANDS:
            if command == 'PA':
                mode = 'absolute'
            elif command == 'PR':
                mode = 'relative'
            continue

        numbers = _parse_numbers(
            argument_text,
            maximum_values=max((maximum_points - point_count) * 2, 0),
        )
        if len(numbers) < 2:
            continue
        coordinate_command_count += 1
        if command == 'PD':
            path_count += 1
            if path_count > maximum_paths:
                raise ValueError(f'PLT 路径过多，最多支持 {maximum_paths} 条')
        if command == 'PA':
            mode = 'absolute'
        elif command == 'PR':
            mode = 'relative'

        for index in range(0, len(numbers) - 1, 2):
            x, y = numbers[index], numbers[index + 1]
            if mode == 'relative':
                current_x += x
                current_y += y
            else:
                current_x = x
                current_y = y
            min_x = min(min_x, current_x)
            min_y = min(min_y, current_y)
            max_x = max(max_x, current_x)
            max_y = max(max_y, current_y)
            point_count += 1
            if point_count > maximum_points:
                raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')

    if point_count == 0:
        raise ValueError('没有解析到有效的 PLT 坐标')

    unit_to_mm = 25.4 / float(units_per_inch)
    return {
        'units_per_inch': units_per_inch,
        'min_x': min_x,
        'min_y': min_y,
        'max_x': max_x,
        'max_y': max_y,
        'width_mm': round((max_x - min_x) * unit_to_mm, 3),
        'height_mm': round((max_y - min_y) * unit_to_mm, 3),
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
