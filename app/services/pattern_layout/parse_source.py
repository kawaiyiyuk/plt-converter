"""Public parser adapter. LT metadata travels with segments, never frame tracing."""
from pathlib import Path
import hashlib
from ..plt_parser import parse_plt


_LAYOUT_PARSE_FLAGS = ('page_x_offsets_applied', 'layout_command_boundaries_applied')


def layout_parse_flags(source):
    """从原件解析结果或保存的源元信息中取已确认的解释标记。"""
    if not isinstance(source, dict):
        return {}
    flags = {key: True for key in _LAYOUT_PARSE_FLAGS if source.get(key) is True}
    if flags.get('layout_command_boundaries_applied') and source.get('layout_command_boundaries_version') == 2:
        flags['layout_command_boundaries_version'] = 2
    return flags


def parse(source, *, apply_page_offsets=True, apply_command_boundaries=True, command_boundaries_version=2):
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else bytes(source)
    drawing_bytes = raw
    # 仅容忍完整命令流末尾的单个 NUL 填充：末字节为 0x00、全文件仅此一个，
    # 且去掉后忽略空白仍以 HPGL 分号结束；其他情况原样交给严格解析器拒绝。
    if (raw.endswith(b'\x00')
            and raw.find(b'\x00') == len(raw) - 1
            and raw[:-1].rstrip(b' \t\r\n').endswith(b';')):
        drawing_bytes = raw[:-1]
    result = parse_plt(drawing_bytes, projection=True, max_chord_error_mm=.05,
                       report_curve_approximation=True, apply_page_offsets=apply_page_offsets,
                       apply_command_boundaries=apply_command_boundaries,
                       command_boundaries_version=command_boundaries_version)
    result['source_sha256'] = hashlib.sha256(raw).hexdigest()
    result['parser_sha256'] = hashlib.sha256((Path(__file__).parent.parent / 'plt_parser.py').read_bytes()).hexdigest()
    units = result['metrics']['units_per_inch'] / 25.4
    for shape in result['shapes']:
        for style in shape.get('source_segment_styles', []):
            argument = style['lt_argument']
            pattern = None
            mode = 'solid'
            if argument:
                try:
                    nums = [float(v) for v in argument.replace(',', ' ').split()]
                except ValueError:
                    nums = []
                if len(nums) == 3 and nums[0] == 2 and nums[1] > 0 and nums[2] == 1:
                    pattern, mode = [nums[1] * units / 2] * 2, 'LT2_absolute_mm_50_50'
                else:
                    mode = 'unsupported_solid_preview'
            style['preview_interpretation'] = {'mode': mode, 'pattern_units': pattern, 'raw': argument}
    return result
