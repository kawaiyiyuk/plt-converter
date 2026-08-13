import math
import os

from .number_tokens import iter_number_tokens


DEFAULT_UNITS_PER_INCH = 1016


def decode_plt(source):
    if not isinstance(source, (bytes, bytearray)):
        raise ValueError('PLT 数据必须是字节')
    raw = bytes(source)
    for encoding in ('utf-8', 'gb18030', 'latin1'):
        try:
            return raw.decode(encoding, errors='strict')
        except UnicodeDecodeError:
            continue
    return raw.decode('latin1', errors='replace')


def parse_plt(source, units_per_inch=DEFAULT_UNITS_PER_INCH):
    text = decode_plt(source) if isinstance(source, (bytes, bytearray)) else str(source or '')
    text = text.replace('\x00', '')
    index = 0
    mode = 'absolute'
    pen_down = False
    current_pen = 1
    current_position = {'x': 0.0, 'y': 0.0}
    current_line_width = None
    stroke = []
    shapes = []
    pen_widths = {}
    next_shape_id = 1
    command_count = 0
    point_count = 0
    maximum_commands = max(1000, int(os.getenv('PLT_MAX_COMMANDS', '250000')))
    maximum_points = max(1000, int(os.getenv('PLT_MAX_POINTS', '500000')))
    maximum_shapes = max(100, int(os.getenv('PLT_MAX_PATHS', '100000')))
    maximum_text_chars = max(1000, int(os.getenv('PLT_MAX_TEXT_CHARS', '100000')))
    text_char_count = 0

    def create_shape(shape):
        nonlocal next_shape_id
        if next_shape_id > maximum_shapes:
            raise ValueError(f'PLT 路径过多，最多支持 {maximum_shapes} 条')
        result = {'id': f'shape-{next_shape_id}'}
        result.update(shape)
        next_shape_id += 1
        return result

    def flush_stroke():
        nonlocal stroke
        if len(stroke) > 1:
            shapes.append(create_shape({
                'type': 'path',
                'pen': current_pen,
                'points': stroke,
                'line_width_units': current_line_width,
            }))
        stroke = []

    def add_point(x, y):
        nonlocal current_position, point_count
        point_count += 1
        if point_count > maximum_points:
            raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
        start = dict(current_position)
        if mode == 'relative':
            point = {
                'x': current_position['x'] + x,
                'y': current_position['y'] + y,
            }
        else:
            point = {'x': x, 'y': y}
        current_position = point
        if pen_down:
            if not stroke:
                stroke.append(start)
            stroke.append(point)
        return point

    def add_absolute_point(point):
        nonlocal current_position, point_count
        point_count += 1
        if point_count > maximum_points:
            raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
        start = dict(current_position)
        current_position = point
        if pen_down:
            if not stroke:
                stroke.append(start)
            stroke.append(point)
        return point

    def add_arc(center, angle_degrees, chord_angle_degrees=5):
        radius = math.hypot(
            current_position['x'] - center['x'],
            current_position['y'] - center['y'],
        )
        if radius <= 0 or not math.isfinite(radius) or not math.isfinite(angle_degrees):
            return
        start_angle = math.atan2(
            current_position['y'] - center['y'],
            current_position['x'] - center['x'],
        )
        chord_angle = max(abs(float(chord_angle_degrees or 5)), 0.1)
        steps = max(1, math.ceil(abs(angle_degrees) / chord_angle))
        if point_count + steps > maximum_points:
            raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
        total_angle = math.radians(angle_degrees)
        for step in range(1, steps + 1):
            theta = start_angle + total_angle * step / steps
            add_absolute_point({
                'x': center['x'] + math.cos(theta) * radius,
                'y': center['y'] + math.sin(theta) * radius,
            })

    while index < len(text):
        if text[index] in '\n\r\t ,':
            index += 1
            continue

        command = text[index:index + 2].upper()
        index += 2
        if len(command) != 2 or not command.isalpha():
            index += 1
            continue
        command_count += 1
        if command_count > maximum_commands:
            raise ValueError(f'PLT 命令数量过多，最多支持 {maximum_commands} 条')

        if command == 'LB':
            text_start = index
            while index < len(text) and text[index] not in '\x03;':
                index += 1
            label = text[text_start:index]
            if label:
                text_char_count += len(label)
                if text_char_count > maximum_text_chars:
                    raise ValueError(f'PLT 文本内容过多，最多支持 {maximum_text_chars} 个字符')
                shapes.append(create_shape({
                    'type': 'text',
                    'pen': current_pen,
                    'point': dict(current_position),
                    'text': label,
                }))
            if index < len(text):
                index += 1
            continue

        argument_start = index
        while index < len(text) and text[index] != ';':
            index += 1
        argument_text = text[argument_start:index]
        if index < len(text):
            index += 1
        remaining_values = max((maximum_points - point_count) * 2, 0)
        value_limit = remaining_values if command in {'PA', 'PR', 'PU', 'PD'} else 10000
        numbers = parse_numbers(argument_text, maximum_values=value_limit)

        if command == 'IN':
            flush_stroke()
            mode = 'absolute'
            pen_down = False
            current_pen = 1
            current_position = {'x': 0.0, 'y': 0.0}
            current_line_width = None
            pen_widths = {}
        elif command == 'SP':
            flush_stroke()
            current_pen = int(numbers[0]) if numbers else 1
            current_line_width = pen_widths.get(current_pen)
        elif command == 'PA':
            mode = 'absolute'
            add_coordinate_pairs(numbers, add_point)
        elif command == 'PR':
            mode = 'relative'
            add_coordinate_pairs(numbers, add_point)
        elif command == 'PU':
            flush_stroke()
            pen_down = False
            add_coordinate_pairs(numbers, add_point)
        elif command == 'PD':
            pen_down = True
            add_coordinate_pairs(numbers, add_point)
        elif command == 'CI':
            flush_stroke()
            radius = numbers[0] if numbers else 0
            if radius > 0:
                shapes.append(create_shape({
                    'type': 'circle',
                    'pen': current_pen,
                    'center': dict(current_position),
                    'radius': radius,
                    'line_width_units': current_line_width,
                }))
        elif command == 'AA' and len(numbers) >= 3:
            add_arc({'x': numbers[0], 'y': numbers[1]}, numbers[2], numbers[3] if len(numbers) > 3 else 5)
        elif command == 'AR' and len(numbers) >= 3:
            add_arc({
                'x': current_position['x'] + numbers[0],
                'y': current_position['y'] + numbers[1],
            }, numbers[2], numbers[3] if len(numbers) > 3 else 5)
        elif command == 'PW':
            if numbers and numbers[0] >= 0:
                target_pen = int(numbers[1]) if len(numbers) > 1 else current_pen
                pen_widths[target_pen] = numbers[0]
                if target_pen == current_pen:
                    current_line_width = numbers[0]

    flush_stroke()
    if not shapes:
        raise ValueError('没有解析到有效的 PLT 矢量内容')
    return {
        'shapes': shapes,
        'metrics': measure_document(shapes, units_per_inch),
    }


def measure_document(shapes, units_per_inch=DEFAULT_UNITS_PER_INCH):
    points = []
    for shape in shapes:
        if shape['type'] == 'path':
            points.extend(shape['points'])
        elif shape['type'] == 'circle':
            center = shape['center']
            radius = shape['radius']
            points.extend([
                {'x': center['x'] - radius, 'y': center['y'] - radius},
                {'x': center['x'] + radius, 'y': center['y'] + radius},
            ])
        elif shape['type'] == 'text':
            points.append(shape['point'])

    if not points:
        raise ValueError('没有解析到有效的 PLT 坐标')
    min_x = min(point['x'] for point in points)
    min_y = min(point['y'] for point in points)
    max_x = max(point['x'] for point in points)
    max_y = max(point['y'] for point in points)
    unit_to_mm = 25.4 / float(units_per_inch)
    return {
        'units_per_inch': units_per_inch,
        'min_x': min_x,
        'min_y': min_y,
        'max_x': max_x,
        'max_y': max_y,
        'width_mm': (max_x - min_x) * unit_to_mm,
        'height_mm': (max_y - min_y) * unit_to_mm,
        'point_count': sum(len(shape.get('points', [])) for shape in shapes),
        'path_count': sum(1 for shape in shapes if shape['type'] == 'path'),
    }


def parse_numbers(text, maximum_values=None):
    if not text.strip():
        return []
    values = []
    for value in iter_number_tokens(text):
        try:
            number = float(value)
        except ValueError:
            continue
        if math.isfinite(number):
            values.append(number)
            if maximum_values is not None and len(values) > maximum_values:
                raise ValueError('PLT 命令参数数量超过服务器限制')
    return values


def add_coordinate_pairs(numbers, callback):
    for index in range(0, len(numbers) - 1, 2):
        callback(numbers[index], numbers[index + 1])
