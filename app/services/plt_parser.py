import math
import os
import re

from .number_tokens import iter_number_tokens


DEFAULT_UNITS_PER_INCH = 1016
# The PDF compatibility font uses a 1000-unit default width (one em).
PLT_LABEL_FONT_SIZE_PT = 8


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


def curve_approximation_warning(max_chord_error_mm):
    return (f'圆弧/圆导出使用折线近似，圆弧分段理论弦高不超过{max_chord_error_mm:g}mm；'
            '不代表投影总误差，需实测。')



_HPGL_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
# 大写指令连写只在已知指令键和完整参数之间拆分。PG 不属于这组连写边界。
_COMPACT_NEXT_COMMANDS = frozenset(
    'AA AR CA CI CO CP CS DC DF DI DP DR DT EA ER FF FL FT IM IN IP IW LB LT '
    'OA OC OD OE OF OH OI OO OP OS PA PD PR PS PT PU RA RO RR '
    'SA SC SI SL SM SP SR SS TL UC VS WG XT YT'.split()
)
_COMPACT_NEXT_COMMANDS_V2 = _COMPACT_NEXT_COMMANDS | {'PW'}
_LINE_START_COMMANDS = _COMPACT_NEXT_COMMANDS | {'PG', 'PW'}
_COORDINATE_PAIR_COMMANDS = {'PA', 'PR', 'PU', 'PD'}
_COMPACT_ARGUMENT_COUNTS = {
    'IN': {0}, 'DF': {0}, 'SP': {0, 1}, 'CI': {1, 2}, 'AA': {3, 4}, 'AR': {3, 4},
    'PW': {0, 1, 2}, 'VS': {0, 1, 2}, 'LT': {0, 1, 2, 3},
    'PG': {0, 1}, 'FL': {1}, 'FF': {0}, 'DI': {0, 2}, 'SI': {0, 2},
}
_ARGUMENT_BOUNDARIES = r';|[\r\n][ \t\r\n]*|(?=(?P<opcode>[A-Z]{2}))'
_LAYOUT_ARGUMENT_TOKENS = re.compile(r'"[^"]*"|' + _ARGUMENT_BOUNDARIES)
_LAYOUT_DT_TOKENS = re.compile(_ARGUMENT_BOUNDARIES)


def _explicit_label_terminator(argument):
    token = argument.strip(' \t\r\n')
    if len(token) == 1 and (token == '\x03' or '!' <= token <= '~'):
        return token
    return None


def _complete_compact_arguments(command, argument):
    if command == 'DT':
        return _explicit_label_terminator(argument) is not None
    count = 0
    for token in iter_number_tokens(argument):
        if _HPGL_NUMBER.fullmatch(token) is None:
            return False
        count += 1
    if command in _COORDINATE_PAIR_COMMANDS:
        return count % 2 == 0
    return count in _COMPACT_ARGUMENT_COUNTS[command]


def _line_starts_command(text, start, compact_commands):
    word = text[start:start + 2]
    if len(word) != 2 or not word.isascii():
        return False
    command = word.upper()
    if command not in _LINE_START_COMMANDS:
        return False
    if command in {'LB', 'DT'}:
        return True
    following = text[start + 2:start + 4]
    # 换行后的坐标值属于原参数。字母长串也不能只凭前两个字符当成指令。
    return not following or not following[0].isalpha() or following in compact_commands


def _layout_argument_boundary(text, start, command, compact_commands):
    """返回参数结束位置、下一条命令起点，以及是否使用了扩展边界。"""
    tokens = _LAYOUT_DT_TOKENS if command == 'DT' else _LAYOUT_ARGUMENT_TOKENS
    compact = command in _COORDINATE_PAIR_COMMANDS or command in _COMPACT_ARGUMENT_COUNTS or command == 'DT'
    quoted_semicolon = False
    for match in tokens.finditer(text, start):
        at = match.start()
        opcode = match.group('opcode')
        if opcode is not None:
            if compact and opcode in compact_commands:
                argument = text[start:at]
                # DT 的第一个字符可以恰好也是下一对字母的一部分。
                if command == 'DT' and not argument.strip():
                    continue
                if _complete_compact_arguments(command, argument):
                    return at, at, True
                compact = False
            continue
        if text[at] == ';':
            return at, match.end(), quoted_semicolon
        if text[at] == '"':
            quoted_semicolon = quoted_semicolon or text.find(';', at + 1, match.end() - 1) >= 0
        elif _line_starts_command(text, match.end(), compact_commands):
            return at, match.end(), True
    return len(text), len(text), quoted_semicolon


def parse_plt(source, units_per_inch=DEFAULT_UNITS_PER_INCH, *, strict=False,
              max_chord_error_mm=None, projection=False, report_curve_approximation=False,
              apply_page_offsets=False, apply_command_boundaries=False, command_boundaries_version=2):
    projection_semantics = strict or projection
    page_offsets_enabled = apply_page_offsets and projection and not strict
    layout_boundaries_enabled = apply_command_boundaries and projection and not strict
    # 旧工程继续按首次识别时的指令分隔和文字状态解释原线编号。
    boundary_v2 = layout_boundaries_enabled and command_boundaries_version == 2
    compact_commands = _COMPACT_NEXT_COMMANDS_V2 if boundary_v2 else _COMPACT_NEXT_COMMANDS
    # Opt-in projection rules must not change the formal converter's parser.
    if not math.isfinite(float(units_per_inch)) or float(units_per_inch) <= 0:
        raise ValueError('PLT 单位必须为正有限数值')
    if max_chord_error_mm is not None and (not math.isfinite(max_chord_error_mm) or max_chord_error_mm <= 0):
        raise ValueError('PLT 圆弧精度必须为正有限数值')
    text = decode_plt(source) if isinstance(source, (bytes, bytearray)) else str(source or '')
    if projection_semantics and '\x00' in text:
        raise ValueError('PLT 含无效空字符，无法保证投影图形完整')
    text = text.replace('\x00', '')
    index = 0
    mode = 'absolute'
    pen_down = False
    current_pen = 1
    current_position = {'x': 0.0, 'y': 0.0}
    page_offset_x = 0.0
    feed_step_x = 0.0
    page_x_offsets_applied = False
    layout_command_boundaries_applied = False
    label_terminator = '\x03'
    explicit_label_terminator = False
    label_carriage_return = dict(current_position)
    current_line_width = None
    stroke = []
    stroke_styles = []
    current_lt_argument = None
    lt_generation = 0
    shapes = []
    pen_widths = {}
    next_shape_id = 1
    command_count = 0
    point_count = 0
    maximum_commands = max(1000, int(os.getenv('PLT_MAX_COMMANDS', '250000')))
    maximum_points = max(1000, int(os.getenv('PLT_MAX_POINTS', '500000')))
    maximum_shapes = max(100, int(os.getenv('PLT_MAX_PATHS', '100000')))
    maximum_text_chars = max(1000, int(os.getenv('PLT_MAX_TEXT_CHARS', '100000')))
    if projection_semantics:
        maximum_commands = min(maximum_commands, 250000)
        maximum_points = min(maximum_points, 500000)
        maximum_shapes = min(maximum_shapes, 100000)
        maximum_text_chars = min(maximum_text_chars, 100000)
    text_char_count = 0
    warnings = []
    ignored_commands = set()
    ignored_command_count = 0

    def warn(message):
        if message not in warnings:
            warnings.append(message)


    def create_shape(shape):
        nonlocal next_shape_id
        if next_shape_id > maximum_shapes:
            raise ValueError(f'PLT 路径过多，最多支持 {maximum_shapes} 条')
        result = {'id': f'shape-{next_shape_id}'}
        result.update(shape)
        next_shape_id += 1
        return result

    def flush_stroke():
        nonlocal stroke, stroke_styles
        if len(stroke) > 1:
            shapes.append(create_shape({
                'type': 'path',
                'pen': current_pen,
                'points': stroke,
                'source_segment_styles': stroke_styles,
                'line_width_units': current_line_width,
            }))
        stroke = []
        stroke_styles = []

    def add_point(x, y):
        nonlocal current_position, point_count, label_carriage_return
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
            point = {'x': x + page_offset_x if page_offset_x else x, 'y': y}
        if projection_semantics and not all(math.isfinite(value) for value in point.values()):
            raise ValueError('PLT 累计坐标超出有限数值范围')
        current_position = point
        if projection_semantics:
            label_carriage_return = dict(point)
        if pen_down and (not projection_semantics or current_pen != 0):
            if not stroke:
                stroke.append(start)
            stroke.append(point)
            stroke_styles.append({'lt_argument': current_lt_argument, 'line_width_units': current_line_width, 'lt_generation': lt_generation})
        return point

    def add_absolute_point(point):
        nonlocal current_position, point_count, label_carriage_return
        point_count += 1
        if point_count > maximum_points:
            raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
        start = dict(current_position)
        if projection_semantics and not all(math.isfinite(value) for value in point.values()):
            raise ValueError('PLT 累计坐标超出有限数值范围')
        current_position = point
        if projection_semantics:
            label_carriage_return = dict(point)
        if pen_down and (not projection_semantics or current_pen != 0):
            if not stroke:
                stroke.append(start)
            stroke.append(point)
            stroke_styles.append({'lt_argument': current_lt_argument, 'line_width_units': current_line_width, 'lt_generation': lt_generation})
        return point

    def add_arc(center, angle_degrees, chord_angle_degrees=5):
        radius = math.hypot(
            current_position['x'] - center['x'],
            current_position['y'] - center['y'],
        )
        if projection and not strict and (not math.isfinite(radius) or not math.isfinite(angle_degrees)):
            raise ValueError('HPGL 圆弧半径或角度超出有限数值范围')
        if radius <= 0 or not math.isfinite(radius) or not math.isfinite(angle_degrees):
            if strict:
                raise ValueError('HPGL 圆弧半径或角度无效')
            if projection:
                warn('已忽略无法绘制的 PLT 圆弧参数，内容可能不完整')
            return
        start_angle = math.atan2(
            current_position['y'] - center['y'],
            current_position['x'] - center['x'],
        )
        chord_angle = max(abs(float(chord_angle_degrees or 5)), 0.1)
        if max_chord_error_mm is not None:
            chord_angle = min(chord_angle, math.degrees(chord_step_angle(radius, units_per_inch, max_chord_error_mm)))
        if projection_semantics and (abs(angle_degrees) / 360 > maximum_points or
                       chord_angle < abs(angle_degrees) / maximum_points):
            raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
        steps = max(1, math.ceil(abs(angle_degrees) / chord_angle))
        if point_count + steps > maximum_points:
            raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
        if (report_curve_approximation and max_chord_error_mm is not None
                and pen_down and (not projection_semantics or current_pen != 0) and angle_degrees != 0):
            warn(curve_approximation_warning(max_chord_error_mm))
        total_angle = math.radians(angle_degrees)
        for step in range(1, steps + 1):
            theta = start_angle + total_angle * step / steps
            add_absolute_point({
                'x': center['x'] + math.cos(theta) * radius,
                'y': center['y'] + math.sin(theta) * radius,
            })

    def add_projection_label(label):
        nonlocal current_position, label_carriage_return
        # Numeric paths retain their endpoint dictionaries after flush_stroke.
        # The label cursor must not mutate an already rendered endpoint.
        current_position = dict(current_position)
        cell_units = PLT_LABEL_FONT_SIZE_PT * float(units_per_inch) / 72
        start = dict(current_position)
        run = []
        run_start = dict(current_position)
        emitted = False

        def flush_run():
            nonlocal run, emitted
            if run and current_pen != 0:
                shapes.append(create_shape({'type': 'text', 'pen': current_pen,
                                            'point': dict(run_start), 'text': ''.join(run)}))
                emitted = True
            run = []

        for offset, character in enumerate(label):
            if ord(character) < 32:
                if character not in '\r\n\t\b' or (character == '\b' and offset == 0):
                    continue
                # C0 controls move the label cursor, never appear as PDF glyphs.
                flush_run()
                if character == '\r':
                    current_position = dict(label_carriage_return)
                elif character == '\n':
                    current_position['y'] -= cell_units
                    label_carriage_return['y'] -= cell_units
                elif character == '\t':
                    column = (current_position['x'] - label_carriage_return['x']) / cell_units
                    # Stabilize exact cell boundaries after repeated 8pt advances.
                    if abs(column - round(column)) < 1e-9:
                        column = round(column)
                    current_position['x'] = label_carriage_return['x'] + (math.floor(column / 8) + 1) * 8 * cell_units
                elif character == '\b' and offset > 0:
                    current_position['x'] -= cell_units
                continue
            if ord(character) > 0xffff:
                character = '□'
                warning = '不支持的增补字符以□兼容显示'
                if warning not in warnings:
                    warnings.append(warning)
            if not run:
                run_start = dict(current_position)
            run.append(character)
            current_position['x'] += cell_units
        flush_run()
        if not all(math.isfinite(value) for value in current_position.values()):
            raise ValueError('PLT 累计坐标超出有限数值范围')
        if current_pen != 0:
            # Preserve accepted control-only labels without inventing visible ink.
            if not emitted:
                shapes.append(create_shape({'type': 'text', 'pen': current_pen,
                                            'point': start, 'text': ''}))
            warning = 'LB 文字按固定 8pt 兼容显示'
            if warning not in warnings:
                warnings.append(warning)

    while index < len(text):
        if text[index] in '\n\r\t ,' or (projection_semantics and text[index] == ';'):
            index += 1
            continue

        command = text[index:index + 2].upper()
        index += 2
        if len(command) != 2 or not command.isalpha():
            if strict:
                raise ValueError('PLT 命令格式无效，无法保证投影图形完整')
            if projection:
                warn('已跳过无法识别的 PLT 命令片段，内容可能不完整')
            index += 1
            continue
        command_count += 1
        if command_count > maximum_commands:
            raise ValueError(f'PLT 命令数量过多，最多支持 {maximum_commands} 条')

        if command == 'LB':
            if projection_semantics:
                flush_stroke()
            text_start = index
            if boundary_v2 and explicit_label_terminator:
                index = text.find(label_terminator, index)
                if index < 0:
                    raise ValueError('PLT 文字标注缺少 DT 指定的结束符')
            else:
                while index < len(text) and text[index] not in (label_terminator, ';'):
                    index += 1
            label = text[text_start:index]
            if label:
                text_char_count += len(label)
                if text_char_count > maximum_text_chars:
                    raise ValueError(f'PLT 文本内容过多，最多支持 {maximum_text_chars} 个字符')
                if projection_semantics:
                    add_projection_label(label)
                else:
                    shapes.append(create_shape({
                        'type': 'text',
                        'pen': current_pen,
                        'point': dict(current_position),
                        'text': label,
                    }))
            if index < len(text):
                index += 1
            continue

        if strict and command not in {'IN', 'SP', 'PA', 'PR', 'PU', 'PD', 'CI', 'AA', 'AR', 'PW', 'VS'}:
            raise ValueError(f'投影暂不支持 HPGL 命令 {command}，无法保证图形完整')

        argument_start = index
        if layout_boundaries_enabled:
            argument_end, index, compatible_boundary = _layout_argument_boundary(text, index, command, compact_commands)
            layout_command_boundaries_applied |= compatible_boundary
        else:
            while index < len(text) and text[index] != ';':
                index += 1
            argument_end = index
            if index < len(text):
                index += 1
        argument_text = text[argument_start:argument_end]
        if layout_boundaries_enabled and command == 'DT':
            terminator = _explicit_label_terminator(argument_text)
            if terminator is not None:
                label_terminator = terminator
                explicit_label_terminator = True
                layout_command_boundaries_applied = True
                continue
        if strict:
            # Use exactly the legacy tokenizer's comma/whitespace boundaries.
            # Validate whole tokens before parse_numbers can skip an invalid one;
            # e.g. 100-200 is one malformed token, never two coordinates.
            for token in iter_number_tokens(argument_text):
                if not _HPGL_NUMBER.fullmatch(token):
                    raise ValueError(f'HPGL 命令 {command} 参数格式无效')
                if not math.isfinite(float(token)):
                    raise ValueError(f'HPGL 命令 {command} 坐标必须有限')
        if projection and not strict:
            for token in iter_number_tokens(argument_text):
                try:
                    valid_number = math.isfinite(float(token))
                except ValueError:
                    valid_number = False
                if not valid_number:
                    warn('已忽略无法解析或非有限的 PLT 参数，内容可能不完整')
        remaining_values = max((maximum_points - point_count) * 2, 0)
        value_limit = remaining_values if command in {'PA', 'PR', 'PU', 'PD'} else 10000
        numbers = parse_numbers(argument_text, maximum_values=value_limit)

        if page_offsets_enabled and command in {'PG', 'FL', 'FF'}:
            argument = argument_text.strip()
            integer_argument = (command in {'PG', 'FL'} and len(numbers) == 1
                                and re.fullmatch(r'[+-]?[0-9]+', argument) is not None)
            if integer_argument or (command == 'FF' and not argument):
                if command == 'FL':
                    feed_step_x = numbers[0]
                else:
                    page_offset_x += feed_step_x if command == 'FF' else numbers[0]
                    if not math.isfinite(page_offset_x):
                        raise ValueError('PLT 累计坐标超出有限数值范围')
                page_x_offsets_applied = True
                continue

        if strict:
            lengths = {'IN': {0}, 'SP': {0, 1}, 'CI': {1, 2}, 'AA': {3, 4}, 'AR': {3, 4}, 'PW': {1, 2}, 'VS': {1, 2}}
            if ((command in lengths and len(numbers) not in lengths[command])
                    or (command in {'PA', 'PR', 'PU', 'PD'} and len(numbers) % 2)):
                raise ValueError(f'HPGL 命令 {command} 参数数量无效')
            if command == 'SP' and numbers and (not numbers[0].is_integer() or numbers[0] < 0):
                raise ValueError('HPGL 命令 SP 画笔编号必须为非负整数')

        # Capture source style as public metadata without claiming renderer
        # support: projection keeps its existing LT warning, strict still rejects.
        if command == 'LT':
            lt_generation += 1
            current_lt_argument = argument_text.strip()

        if projection and not strict:
            supported = {'IN', 'SP', 'PA', 'PR', 'PU', 'PD', 'CI', 'AA', 'AR', 'PW', 'VS'}
            if command not in supported:
                # At most 16 command names are retained; never retain source arguments.
                if command not in ignored_commands and len(ignored_commands) < 16:
                    ignored_commands.add(command)
                ignored_command_count += 1
                continue
            if command in {'PA', 'PR', 'PU', 'PD'} and len(numbers) % 2:
                warn('已忽略 PLT 坐标末尾未配对的参数，内容可能不完整')
            lengths = {'IN': {0}, 'SP': {0, 1}, 'CI': {1, 2}, 'AA': {3, 4}, 'AR': {3, 4}, 'PW': {0, 1, 2}, 'VS': {0, 1, 2}}
            if command in lengths and len(numbers) not in lengths[command]:
                warn('PLT 参数数量不完整或多余，已按兼容方式处理')
            if command == 'CI' and numbers and numbers[0] <= 0:
                warn('已忽略无法绘制的 PLT 圆参数，内容可能不完整')

        if command == 'IN':
            flush_stroke()
            # 复位累计横移，步长由 FL 单独设置。
            page_offset_x = 0.0
            if boundary_v2:
                label_terminator = '\x03'
                explicit_label_terminator = False
            mode = 'absolute'
            pen_down = False
            current_pen = 1
            current_position = {'x': 0.0, 'y': 0.0}
            label_carriage_return = dict(current_position)
            current_line_width = None
            pen_widths = {}
            current_lt_argument = None
            lt_generation += 1
        elif command == 'SP':
            flush_stroke()
            # HPGL SP without a parameter stores the pen, just like SP0.
            # Preserve the formal converter's historical default outside strict.
            current_pen = int(numbers[0]) if numbers else (0 if projection_semantics else 1)
            current_line_width = pen_widths.get(current_pen)
        elif command == 'PA':
            mode = 'absolute'
            add_coordinate_pairs(numbers, add_point)
            if projection_semantics:
                label_carriage_return = dict(current_position)
        elif command == 'PR':
            mode = 'relative'
            add_coordinate_pairs(numbers, add_point)
            if projection_semantics:
                label_carriage_return = dict(current_position)
        elif command == 'PU':
            flush_stroke()
            pen_down = False
            add_coordinate_pairs(numbers, add_point)
        elif command == 'PD':
            pen_down = True
            add_coordinate_pairs(numbers, add_point)
        elif command == 'CI':
            flush_stroke()
            if projection_semantics and current_pen == 0:
                continue
            radius = numbers[0] if numbers else 0
            if strict and radius <= 0:
                raise ValueError('HPGL 命令 CI 半径必须大于0')
            if radius > 0 and max_chord_error_mm is not None:
                step_angle = chord_step_angle(radius, units_per_inch, max_chord_error_mm)
                if step_angle < 2 * math.pi / maximum_points:
                    raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
                steps = max(4, 4 * math.ceil((2 * math.pi / step_angle) / 4))
                if point_count + steps + 1 > maximum_points:
                    raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
                if report_curve_approximation:
                    warn(curve_approximation_warning(max_chord_error_mm))
                point_count += steps + 1
                points = [{'x': current_position['x'] + radius * math.cos(2 * math.pi * i / steps),
                           'y': current_position['y'] + radius * math.sin(2 * math.pi * i / steps)}
                          for i in range(steps + 1)]
                shapes.append(create_shape({'type': 'path', 'pen': current_pen, 'points': points,
                                            'source_segment_styles': [{'lt_argument': current_lt_argument, 'line_width_units': current_line_width, 'lt_generation': lt_generation} for _ in points[1:]],
                                            'line_width_units': current_line_width}))
            elif radius > 0:
                shapes.append(create_shape({
                    'type': 'circle',
                    'pen': current_pen,
                    'center': dict(current_position),
                    'radius': radius,
                    'line_width_units': current_line_width,
                }))
        elif command == 'AA' and len(numbers) >= 3:
            center_x = numbers[0] + page_offset_x if page_offset_x else numbers[0]
            add_arc({'x': center_x, 'y': numbers[1]}, numbers[2], numbers[3] if len(numbers) > 3 else 5)
        elif command == 'AR' and len(numbers) >= 3:
            add_arc({
                'x': current_position['x'] + numbers[0],
                'y': current_position['y'] + numbers[1],
            }, numbers[2], numbers[3] if len(numbers) > 3 else 5)
        elif command == 'VS':
            if projection_semantics and '已忽略绘图速度 VS，投影使用统一黑线' not in warnings:
                warnings.append('已忽略绘图速度 VS，投影使用统一黑线')
        elif command == 'PW':
            if numbers and numbers[0] >= 0:
                target_pen = int(numbers[1]) if len(numbers) > 1 else current_pen
                pen_widths[target_pen] = numbers[0]
                if target_pen == current_pen:
                    current_line_width = numbers[0]

    flush_stroke()
    if ignored_command_count:
        names = '、'.join(sorted(ignored_commands))
        warn(f'已跳过未支持的 HPGL 命令（最多列16种）：{names}；共{ignored_command_count}条，图形可能缺失或尺寸不正确')
    if not shapes:
        raise ValueError('没有解析到有效的 PLT 矢量内容')
    metrics = measure_document(shapes, units_per_inch)
    # A path split (SP/PU/IN) repeats its start in the rendered shape. Include
    # those starts in the strict output cap as well as incoming coordinates.
    if projection_semantics and metrics['point_count'] > maximum_points:
        raise ValueError(f'PLT 坐标点过多，最多支持 {maximum_points} 个')
    return {
        'shapes': shapes,
        'metrics': metrics,
        **({'warnings': warnings} if projection_semantics else {}),
        **({'page_x_offsets_applied': True} if page_x_offsets_applied else {}),
        **({'layout_command_boundaries_applied': True} if layout_command_boundaries_applied else {}),
        **({'layout_command_boundaries_version': 2} if boundary_v2 and layout_command_boundaries_applied else {}),
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


def chord_step_angle(radius_units, units_per_inch, error_mm):
    """Maximum central angle with <= error_mm physical sagitta; stable for huge r."""
    error_units = error_mm * float(units_per_inch) / 25.4
    ratio = min(1.0, error_units / abs(radius_units) / 2)
    return min(math.pi, 4 * math.asin(math.sqrt(ratio)))
