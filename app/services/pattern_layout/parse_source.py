"""Public parser adapter. LT metadata travels with segments, never frame tracing."""
from pathlib import Path
import hashlib
from ..plt_parser import parse_plt


def parse(source):
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else bytes(source)
    result = parse_plt(raw, projection=True, max_chord_error_mm=.05, report_curve_approximation=True)
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
