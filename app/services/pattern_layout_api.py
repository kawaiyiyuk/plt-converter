"""Source-referenced experimental candidates and strict PLT fragment export.

Geometry is evidence, never a claim that a closed contour is a sewing piece.
The independent contour alternatives deliberately retain interior ambiguity.
"""
import copy
import hashlib
import math
from shapely.geometry import LineString, Polygon, Point
from shapely import make_valid
from shapely.ops import unary_union
from .pattern_layout.parse_source import parse
from .pattern_layout.closed_loop import build_geometry, interval_parts, bounds_of_points, PARAMS
from .pattern_layout.layout_marker_v5 import classify_layout_markers


def runtime_version():
    from .pattern_layout_fingerprint import recognition_runtime_version
    return recognition_runtime_version()


def source_paths(raw):
    parsed = parse(raw)
    metrics = parsed['metrics']
    units = metrics['units_per_inch'] / 25.4
    paths = []
    for shape in parsed['shapes']:
        if 'points' not in shape:
            continue
        paths.append({'id': shape['id'], 'raw_points': [[p['x'], p['y']] for p in shape['points']],
                      'points': [[(p['x']-metrics['min_x'])/units, (metrics['max_y']-p['y'])/units] for p in shape['points']],
                      'pen': shape['pen'], 'segment_styles': shape['source_segment_styles'],
                      'line_width_units': shape.get('line_width_units')})
    return parsed, paths


def recognize_source(raw, name='source.plt'):
    parsed, paths = source_paths(raw)
    m = parsed['metrics']
    units = m['units_per_inch']/25.4
    result = build_geometry(paths, m['width_mm'], m['height_mm'], units)
    result['v5_layout_marker_audit'] = classify_layout_markers(result, paths, numeric_epsilon_mm=PARAMS['numeric_epsilon_plotter_units']/units,
                                                            min_span_to_symbol_ratio=PARAMS['layout_marker_min_span_to_symbol_ratio'], apply=True)
    # Keep the original experimental grouping intact for inspect/restore. A nested
    # loop or large rectangle may be a real piece; its full geometric alternative
    # includes exact clipped interior intervals, not an incomplete support outline.
    result['experimental_grouping'] = copy.deepcopy({'pieces': result['pieces'], 'auxiliary': result['auxiliary']})
    eps = PARAMS['numeric_epsilon_plotter_units']/units
    alternatives = []
    for path in paths:
        line = LineString(path['points'])
        if len(path['points'])<4 or path['points'][0]!=path['points'][-1]:
            continue
        original_polygon=Polygon(path['points'])
        repaired=make_valid(original_polygon) if not original_polygon.is_valid else original_polygon
        def polygon_components(geometry):
            if geometry.geom_type=='Polygon':return [geometry]
            return [p for part in getattr(geometry,'geoms',()) for p in polygon_components(part)]
        poly=unary_union(polygon_components(repaired))
        if poly.is_empty or poly.area<=eps*eps:continue
        contour_issues=[] if original_polygon.is_valid else [{'code':'source_self_intersection_region_ambiguous'}]
        fragments = []
        for source in paths:
            for index, (a, b) in enumerate(zip(source['points'], source['points'][1:])):
                selected=[(0.,1.)] if source['id']==path['id'] and math.dist(a,b)>eps else interval_parts(a,b,poly,eps)
                for t0, t1 in selected:
                    fragments.append({'path_id': source['id'], 'segment_index': index, 't0': t0, 't1': t1,
                                      'a': [a[k]+(b[k]-a[k])*t0 for k in (0,1)], 'b': [a[k]+(b[k]-a[k])*t1 for k in (0,1)],
                                      'style': source['segment_styles'][index], 'assignment': 'geometric_region_ambiguous'})
        alternatives.append({'id': 'S-'+path['id'], 'status': 'independent_source_contour_needs_review',
                             'bounds': bounds_of_points(path['points']), 'outer': path['points'], 'fragments': fragments,
                             'source_closed_path_ids': [path['id']], 'semantic_piece_verified': False,
                             'content_complete_for_region': True,
                             'issues': contour_issues+[{'code': 'interior_geometry_may_include_other_real_pieces_or_sizes'},
                                        {'code': 'not_semantic_piece_truth_no_exclusive_owner_assumed'}]})
    result['pieces'].extend(alternatives)
    result.update(schema_version=1, paths=paths,
                  source={'sha256': hashlib.sha256(raw).hexdigest(), 'name': name, 'width_mm': m['width_mm'], 'height_mm': m['height_mm'],
                          'units_per_mm': units, 'raw_bounds': [m['min_x'],m['min_y'],m['max_x'],m['max_y']], 'warnings': parsed['warnings'],
                          'engine_version': 'v6b-program-only-experimental-with-source-contour-alternatives',
                          'algorithm_parameters': PARAMS, 'semantic_verified': False, 'runtime_version': runtime_version()},
                  non_path_shapes=[s for s in parsed['shapes'] if 'points' not in s])
    for candidate in result['pieces']:
        poly = make_valid(Polygon(candidate['outer']))
        candidate['text_ids'] = [s['id'] for s in result['non_path_shapes'] if s['type']=='text' and poly.covers(Point((s['point']['x']-m['min_x'])/units, (m['max_y']-s['point']['y'])/units))]
    return result


class PatternLayoutExportError(ValueError):
    MESSAGES={
        'invalid_source_identity':'原件与工程版本不一致，请保留草稿并重新读取原附件。',
        'invalid_source_fragment':'裁片片段引用无效，请保留草稿并重新读取原附件。',
        'invalid_numeric_value':'排版坐标或参数无效，请保留草稿并检查当前排版。',
        'source_warning_confirmation_required':'原文件存在待确认警告，请查看并确认后再导出。',
        'source_semantics_not_faithful':'原文件含暂时无法保真的内容，请保留草稿并反馈原文件。',
        'no_selected_fragments':'请先选择并放置至少一片裁片。',
        'label_pose_not_faithful':'设备文字旋转或镜像暂无法保真，请保留草稿并调整该片姿态后再试。',
        'line_type_phase_not_faithful':'该设备线型的截取相位暂无法保真，请保留草稿并反馈原文件。',
        'label_control_not_supported':'原文件文字控制指令暂无法保真导出，请保留草稿并反馈原文件。',
    }
    def __init__(self,code):
        self.code=code
        super().__init__(self.MESSAGES[code])


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (float,int)) or not math.isfinite(value):
        raise PatternLayoutExportError('invalid_numeric_value')
    return float(value)


def source_phase_prefixes(paths):
    """Linear source arclength since each LT command, shared by preview/export."""
    phase_prefix = {}
    for path in paths:
        offsets=[];distance=0.;previous_generation=object()
        for index,style in enumerate(path['segment_styles']):
            generation=style.get('lt_generation')
            if generation!=previous_generation:distance=0.
            offsets.append(distance)
            distance+=math.dist(*path['points'][index:index+2])
            previous_generation=generation
        phase_prefix[path['id']]=offsets
    return phase_prefix


def hpgl_decimal(value):
    """Round-trip finite binary floats as plain HPGL decimals, never exponents."""
    from decimal import Decimal
    value=finite(value,'HPGL output number')
    if value==0:return '0'
    text=format(Decimal(repr(value)),'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def export_project(source_bytes, document):
    """Export only exact referenced intervals; unknown/lossy semantics fail closed."""
    if document.get('schema_version') != 1 or document.get('source_sha256') != hashlib.sha256(source_bytes).hexdigest():
        raise PatternLayoutExportError('invalid_source_identity')
    parsed, paths = source_paths(source_bytes)
    from .plt_parser import curve_approximation_warning
    acknowledged = document.get('acknowledged_source_warnings', [])
    unconfirmed=[];unsupported=[]
    for warning in parsed['warnings']:
        if '绘图速度 VS' in warning or 'LB 文字按固定 8pt 兼容显示' in warning:continue
        if ('已跳过未支持' in warning or '已跳过无法识别' in warning
                or warning == curve_approximation_warning(.05)):
            if warning not in acknowledged:unconfirmed.append(warning)
        else:unsupported.append(warning)
    if unsupported:raise PatternLayoutExportError('source_semantics_not_faithful')
    if unconfirmed:raise PatternLayoutExportError('source_warning_confirmation_required')
    m = parsed['metrics']; units = m['units_per_inch']/25.4
    lookup = {p['id']: p for p in paths}
    phase_prefix = source_phase_prefixes(paths)
    candidates = {p['id']: p for p in document.get('candidates', [])}
    if len(candidates) != len(document.get('candidates', [])):
        raise PatternLayoutExportError('invalid_source_fragment')
    output = ['IN;', 'PA;']
    expected = []
    expected_text = []
    text_by_id = {s['id']:s for s in parsed['shapes'] if s['type']=='text'}
    for placement in document.get('placements', []):
        candidate = candidates.get(placement['piece_id'])
        if candidate is None: raise PatternLayoutExportError('invalid_source_fragment')
        angle = finite(placement.get('rotation_deg',0), 'rotation_deg')
        rad = math.radians(angle); c,s = math.cos(rad), math.sin(rad)
        mirror = placement.get('mirror_x',False)
        if not isinstance(mirror,bool): raise PatternLayoutExportError('invalid_numeric_value')
        anchor = placement.get('anchor', {'x':0,'y':0})
        ax,ay = finite(anchor['x'],'anchor.x'),finite(anchor['y'],'anchor.y')
        dx,dy = finite(placement['x'],'x'), finite(placement['y'],'y')
        def transform(p):
            x,y = p[0]-ax,p[1]-ay
            if mirror: x=-x
            return [(c*x-s*y+dx)*units, -(s*x+c*y+dy)*units]
        previous = None
        for fragment in candidate.get('fragments', []):
            path = lookup.get(fragment['path_id'])
            index = fragment['segment_index']
            if path is None or isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(path['points'])-1:
                raise PatternLayoutExportError('invalid_source_fragment')
            t0,t1 = finite(fragment['t0'],'t0'),finite(fragment['t1'],'t1')
            if not 0<=t0<t1<=1: raise PatternLayoutExportError('invalid_source_fragment')
            a,b = path['points'][index:index+2]
            pa,pb = transform([a[k]+(b[k]-a[k])*t0 for k in (0,1)]),transform([a[k]+(b[k]-a[k])*t1 for k in (0,1)])
            style = path['segment_styles'][index]
            lt = style['lt_argument'] or ''
            key = (path['id'],index,t1,lt,style.get('lt_generation'),style.get('line_width_units',path.get('line_width_units')))
            continuous = previous is not None and previous[0]==path['id'] and previous[3]==lt and previous[4]==style.get('lt_generation') and ((previous[1]==index and previous[2]==t0) or (previous[1]+1==index and previous[2]==1 and t0==0))
            if lt:
                try: lt_values=[float(v) for v in lt.replace(',', ' ').split()]
                except ValueError: lt_values=[]
                if len(lt_values)==3 and lt_values[0]==2 and lt_values[1]>0 and lt_values[2]==1:
                    # Explicit absolute LT2: each dash begins at a native pattern
                    # boundary. Keep LT2 commands and pen, split PU gaps in source
                    # arclength before applying rotation/mirror. Never infer other
                    # device-dependent LT tables/relative reference diagonals.
                    period=lt_values[1]; half=period/2
                    length=math.dist(a,b); prefix=phase_prefix[path['id']][index]
                    begin,end=prefix+t0*length,prefix+t1*length
                    cycle=math.floor(begin/period)
                    while cycle*period<end:
                        lo,hi=max(begin,cycle*period),min(end,cycle*period+half)
                        if hi>lo and length>0:
                            u0,u1=(lo-prefix)/length,(hi-prefix)/length
                            da=transform([a[k]+(b[k]-a[k])*u0 for k in (0,1)])
                            db=transform([a[k]+(b[k]-a[k])*u1 for k in (0,1)])
                            output.extend(['PU;','IN;','PA;',f'SP{path["pen"]};','LT'+lt+';'])
                            width=style.get('line_width_units',path.get('line_width_units'))
                            if width is not None: output.append('PW'+hpgl_decimal(width)+';')
                            output.extend(['PU'+','.join(hpgl_decimal(v) for v in da)+';', 'PD'+','.join(hpgl_decimal(v) for v in db)+';'])
                            expected.append({'a':da,'b':db,'pen':path['pen'],'lt':lt,'width':width})
                        cycle+=1
                    previous=None
                    continue
                if not continuous and (index!=0 or t0!=0):
                    raise PatternLayoutExportError('line_type_phase_not_faithful')
            if not continuous:
                output.extend(['PU;','IN;','PA;',f'SP{path["pen"]};','LT'+lt+';'])
                width = style.get('line_width_units',path.get('line_width_units'))
                if width is not None: output.append('PW'+hpgl_decimal(width)+';')
                output.append('PU'+','.join(hpgl_decimal(v) for v in pa)+';')
            if continuous and previous[5]!=style.get('line_width_units',path.get('line_width_units')):
                width=style.get('line_width_units',path.get('line_width_units'))
                if width is None:raise PatternLayoutExportError('source_semantics_not_faithful')
                output.append('PW'+hpgl_decimal(width)+';')
            output.append('PD'+','.join(hpgl_decimal(v) for v in pb)+';')
            expected.append({'a':pa,'b':pb,'pen':path['pen'],'lt':lt,'width':style.get('line_width_units',path.get('line_width_units'))})
            previous=key
        output.append('PU;')
        for text_id in candidate.get('text_ids', []):
            if text_id not in text_by_id: raise PatternLayoutExportError('invalid_source_fragment')
            if mirror or angle%360: raise PatternLayoutExportError('label_pose_not_faithful')
            label=text_by_id[text_id]; txt=label['text']
            if any(ord(ch)<32 or ch in ';\x03' for ch in txt): raise PatternLayoutExportError('label_control_not_supported')
            p=transform([(label['point']['x']-m['min_x'])/units,(m['max_y']-label['point']['y'])/units])
            output.extend([f'SP{label["pen"]};','PU'+','.join(hpgl_decimal(v) for v in p)+';', 'LB'+txt+'\x03'])
            expected_text.append({'point':p,'pen':label['pen'],'text':txt})
    if not expected: raise PatternLayoutExportError('no_selected_fragments')
    result=''.join(output).encode('utf-8')
    from .pattern_layout_verify import verify_export
    verify_export(result,expected,expected_text)
    return result
