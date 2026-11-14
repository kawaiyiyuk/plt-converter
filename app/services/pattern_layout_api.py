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
from .pattern_layout.parse_source import layout_parse_flags, parse
from .pattern_layout.closed_loop import build_geometry, interval_parts, bounds_of_points, PARAMS
from .pattern_layout.independent_shells import report_progress
from .pattern_layout.near_closed_strokes import discover_multi_stroke_alternatives, source_contour_fragments
from .pattern_layout.short_stroke_tips import short_tip_outline
from .pattern_layout.layout_marker_v5 import classify_layout_markers

ENGINE_VERSION='v6b-program-only-experimental-with-source-contour-alternatives'


def runtime_version():
    from .pattern_layout_fingerprint import recognition_runtime_version
    return recognition_runtime_version()


def source_paths(raw, *, apply_page_offsets=True, apply_command_boundaries=True, command_boundaries_version=2):
    parsed = parse(raw, apply_page_offsets=apply_page_offsets, apply_command_boundaries=apply_command_boundaries,
                   command_boundaries_version=command_boundaries_version)
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


def recognize_source(raw, name='source.plt', *, include_full_support_audit=True, on_progress=None, recognition_mode='strict'):
    if recognition_mode not in ('strict', 'relaxed'):
        raise ValueError('未知识别模式')
    report_progress(on_progress, 'parsing', 0, 1)
    parsed, paths = source_paths(raw)
    report_progress(on_progress, 'parsing', 1, 1)
    m = parsed['metrics']
    units = m['units_per_inch']/25.4
    result = build_geometry(paths, m['width_mm'], m['height_mm'], units,
                            include_full_support_audit=include_full_support_audit,on_progress=on_progress)
    result['v5_layout_marker_audit'] = classify_layout_markers(result, paths, numeric_epsilon_mm=PARAMS['numeric_epsilon_plotter_units']/units,
                                                            min_span_to_symbol_ratio=PARAMS['layout_marker_min_span_to_symbol_ratio'], apply=True)
    # Keep the original experimental grouping intact for inspect/restore. A nested
    # loop or large rectangle may be a real piece; its full geometric alternative
    # includes exact clipped interior intervals, not an incomplete support outline.
    result['experimental_grouping'] = copy.deepcopy({'pieces': result['pieces'], 'auxiliary': result['auxiliary']})
    eps = PARAMS['numeric_epsilon_plotter_units']/units
    alternatives = []
    alternative_polys = {}
    report_progress(on_progress, 'alternatives', 0, len(paths))
    for path_index, path in enumerate(paths):
        report_progress(on_progress, 'alternatives', path_index, len(paths))
        line = LineString(path['points'])
        if len(path['points'])<4 or math.dist(path['points'][0],path['points'][-1])>PARAMS['near_closed_tolerance_mm']:
            continue
        original_polygon=Polygon(path['points'])
        repaired=make_valid(original_polygon) if not original_polygon.is_valid else original_polygon
        def polygon_components(geometry):
            if geometry.geom_type=='Polygon':return [geometry]
            return [p for part in getattr(geometry,'geoms',()) for p in polygon_components(part)]
        poly=unary_union(polygon_components(repaired));poly_bounds=poly.bounds
        if poly.is_empty or poly.area<=eps*eps:continue
        if poly.area<PARAMS['min_candidate_area_mm2']:
            result['small_candidate_filter']['ignored_source_contour_count']+=1
            continue
        contour_issues=[] if original_polygon.is_valid else [{'code':'source_self_intersection_region_ambiguous'}]
        if path['points'][0]!=path['points'][-1]:
            contour_issues=[{'code':'source_path_not_exactly_closed','gap_mm':math.dist(path['points'][0],path['points'][-1])}]+contour_issues
        fragments = []
        for source in paths:
            for index, (a, b) in enumerate(zip(source['points'], source['points'][1:])):
                selected=[(0.,1.)] if source['id']==path['id'] and math.dist(a,b)>eps else interval_parts(a,b,poly,eps,poly_bounds)
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
        alternative_polys[alternatives[-1]['id']] = poly
    frame_candidates = []
    main_frame_ids = set(result['geometry_audit']['frame_path_ids'])
    for candidate in alternatives:
        if candidate['source_closed_path_ids'][0] in main_frame_ids:
            continue
        poly = alternative_polys[candidate['id']]
        x0, y0, x1, y1 = poly.bounds
        if not (m['width_mm'] > 0 and m['height_mm'] > 0):
            continue
        if max(x0, m['width_mm'] - x1) > m['width_mm'] * PARAMS['frame_boundary_fraction'] or max(y0, m['height_mm'] - y1) > m['height_mm'] * PARAMS['frame_boundary_fraction']:
            continue
        envelope = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        if envelope.area <= 0 or poly.area / envelope.area < PARAMS['frame_min_envelope_fill']:
            continue
        witnesses = [q for q in alternatives if q is not candidate and alternative_polys[q['id']].area >= PARAMS['frame_body_witness_area_mm2'] and alternative_polys[q['id']].area < poly.area and poly.covers(alternative_polys[q['id']].representative_point())]
        witness_polys = [alternative_polys[q['id']] for q in witnesses]
        independent = any(not a.covers(b) and not b.covers(a) and a.intersection(b).area <= eps * eps for i, a in enumerate(witness_polys) for b in witness_polys[:i])
        if independent:
            frame_candidates.append((candidate, len(witnesses)))
    for candidate, witness_count in frame_candidates:
        candidate['status'] = 'suspected_layout_frame'
        candidate['issues'] = [{'code': 'suspected_layout_frame_contains_other_pieces', 'witness_count': witness_count}] + candidate['issues']
        result['auxiliary'].append(candidate)
    moved = {id(candidate) for candidate, _ in frame_candidates}
    result['pieces'].extend(candidate for candidate in alternatives if id(candidate) not in moved)
    result['source_contour_frame_audit'] = {'frame_candidate_ids': [candidate['id'] for candidate, _ in frame_candidates],
                                            'min_envelope_fill': PARAMS['frame_min_envelope_fill'], 'boundary_fraction': PARAMS['frame_boundary_fraction'],
                                            'witness_area_mm2': PARAMS['frame_body_witness_area_mm2']}
    # 多笔备选独立追加；不重排、合并或替换既有候选和原路径。
    excluded = {path_id for candidate in alternatives for path_id in candidate.get('source_closed_path_ids', [])}
    strict_params = {**PARAMS, 'multi_stroke_join_tolerance_mm': .1}
    joined = discover_multi_stroke_alternatives(paths, excluded, eps, strict_params)
    if recognition_mode == 'relaxed':
        members = {frozenset(candidate['source_closed_path_ids']) for candidate, _ in joined}
        relaxed_params = {**PARAMS, 'multi_stroke_join_tolerance_mm': .5}
        for candidate, poly in discover_multi_stroke_alternatives(paths, excluded, eps, relaxed_params):
            identity = frozenset(candidate['source_closed_path_ids'])
            if identity in members:
                continue
            # 宽松新增轮廓使用独立身份，不覆盖严格模式中的同名裁片。
            candidate['id'] = candidate['id'].replace('SJ-', 'SJW-', 1)
            candidate['issues'].insert(0, {'code': 'relaxed_stroke_join_needs_review', 'tolerance_mm': .5})
            joined.append((candidate, poly))
            members.add(identity)
    result['pieces'].extend(candidate for candidate, _ in joined)
    # 兼容短端线作为新增备选追加，已有候选的分类、顺序与原段保持。
    def add_source_alternative(path, poly, outer, contour_issues):
        fragments = source_contour_fragments(paths, {path['id']}, poly, eps)
        candidate_id = 'S-' + path['id']
        alternatives.append({'id': candidate_id, 'status': 'independent_source_contour_needs_review',
                             'bounds': bounds_of_points(path['points']), 'outer': outer, 'fragments': fragments,
                             'source_closed_path_ids': [path['id']], 'semantic_piece_verified': False,
                             'content_complete_for_region': True,
                             'issues': contour_issues + [{'code': 'interior_geometry_may_include_other_real_pieces_or_sizes'},
                                        {'code': 'not_semantic_piece_truth_no_exclusive_owner_assumed'}]})
        alternative_polys[candidate_id] = poly

    previous_alternative_count = len(alternatives)
    covered_paths = {
        path_id
        for candidate in result['pieces'] + result['auxiliary'] + alternatives
        for path_id in candidate.get('source_closed_path_ids', ())
    }
    for path in paths:
        if path['id'] in covered_paths:
            continue
        recognized = short_tip_outline(path['points'], units, eps, PARAMS)
        if recognized is None:
            continue
        poly, gap, tip_sides = recognized
        add_source_alternative(path, poly, [list(point) for point in poly.exterior.coords], [{
            'code': 'source_short_tip_separated_for_recognition', 'sides': tip_sides, 'gap_mm': gap,
        }])
    result['pieces'].extend(alternatives[previous_alternative_count:])
    result.update(schema_version=1, paths=paths,
                  source={'sha256': hashlib.sha256(raw).hexdigest(), 'name': name, 'width_mm': m['width_mm'], 'height_mm': m['height_mm'],
                          'units_per_mm': units, 'raw_bounds': [m['min_x'],m['min_y'],m['max_x'],m['max_y']], 'warnings': parsed['warnings'],
                          'engine_version': ENGINE_VERSION,
                          'algorithm_parameters': PARAMS, 'semantic_verified': False, 'runtime_version': runtime_version(),
                          'recognition_mode': recognition_mode,
                          'multi_stroke_join_tolerance_mm': .5 if recognition_mode == 'relaxed' else .1},
                  non_path_shapes=[s for s in parsed['shapes'] if 'points' not in s])
    result['source'].update(layout_parse_flags(parsed))
    for candidate in result['pieces']:
        poly = make_valid(Polygon(candidate['outer']))
        candidate['text_ids'] = [s['id'] for s in result['non_path_shapes'] if s['type']=='text' and poly.covers(Point((s['point']['x']-m['min_x'])/units, (m['max_y']-s['point']['y'])/units))]
    report_progress(on_progress, 'alternatives', len(paths), len(paths))
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
        'label_control_not_supported':'原件标注含分号或不支持的控制字符，无法保真导出。请保留原件和排版草稿。',
        'invalid_fold_edge':'折边无效或与当前裁片、布面不一致，请保留草稿并重新确认折边。',
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


# Fold endpoints are compared as floating-point source coordinates. This is a
# numeric equality tolerance, not a claim about physical plotter or cutting
# accuracy; it is far below any meaningful millimetre measurement.
FOLD_NUMERIC_TOLERANCE_MM=1e-6
FOLD_PARAMETER_TOLERANCE=1e-9


def _fold_error():
    raise PatternLayoutExportError('invalid_fold_edge')


def _fold_number(value):
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
        _fold_error()
    return float(value)


def _fold_point(value):
    if not isinstance(value,(list,tuple)) or len(value)!=2:
        _fold_error()
    return [_fold_number(value[0]),_fold_number(value[1])]


def _fold_tolerance(a,b,values=()):
    scale=max([1.,abs(a[0]),abs(a[1]),abs(b[0]),abs(b[1])]+[abs(v) for v in values])
    return max(FOLD_NUMERIC_TOLERANCE_MM,scale*1e-12)


def _fold_merge(parts,tolerance=FOLD_PARAMETER_TOLERANCE):
    merged=[]
    for start,end in sorted(parts):
        if merged and start<=merged[-1][1]+tolerance:
            merged[-1][1]=max(end,merged[-1][1])
        else:
            merged.append([start,end])
    return merged


def _fold_covered(parts,start,end):
    return any(lo<=start+FOLD_PARAMETER_TOLERANCE and hi>=end-FOLD_PARAMETER_TOLERANCE for lo,hi in parts)


def _fold_subtract(start,end,parts):
    """Remove exact source-parameter intervals from one candidate interval."""
    if not parts:return [(start,end)]
    remaining=[];cursor=start
    for lo,hi in parts:
        if hi<=cursor+FOLD_PARAMETER_TOLERANCE:continue
        if lo>=end-FOLD_PARAMETER_TOLERANCE:break
        if lo>cursor:remaining.append((cursor,min(lo,end)))
        cursor=max(cursor,hi)
        if cursor>=end-FOLD_PARAMETER_TOLERANCE:break
    if cursor<end-FOLD_PARAMETER_TOLERANCE:remaining.append((max(cursor,start),end))
    return [(lo,hi) for lo,hi in remaining if hi-lo>FOLD_PARAMETER_TOLERANCE]


def _fold_interval_point(path,index,t):
    a,b=path['points'][index:index+2]
    return [a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t]


def _fold_reflect(point,axis):
    origin,direction=axis
    vx,vy=point[0]-origin[0],point[1]-origin[1]
    projection=vx*direction[0]+vy*direction[1]
    # Reflect the offset from the axis origin, then restore that origin:
    # origin + 2*projection*direction - (point-origin).
    return [origin[0]+2*projection*direction[0]-vx,
            origin[1]+2*projection*direction[1]-vy]


def _fold_export_point(path,index,t,reflected,axis):
    point=_fold_interval_point(path,index,t)
    return _fold_reflect(point,axis) if reflected else point


def _fold_candidate_entries(candidate,lookup):
    entries=[]
    for fragment in candidate.get('fragments', []):
        if not isinstance(fragment,dict):raise PatternLayoutExportError('invalid_source_fragment')
        path=lookup.get(fragment.get('path_id'))
        index=fragment.get('segment_index')
        if path is None or isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(path['points'])-1:
            raise PatternLayoutExportError('invalid_source_fragment')
        t0,t1=fragment.get('t0'),fragment.get('t1')
        if (isinstance(t0,bool) or isinstance(t1,bool) or not isinstance(t0,(int,float)) or not isinstance(t1,(int,float))
                or not math.isfinite(t0) or not math.isfinite(t1) or not 0<=t0<t1<=1):
            raise PatternLayoutExportError('invalid_source_fragment')
        p0,p1=_fold_interval_point(path,index,t0),_fold_interval_point(path,index,t1)
        entries.append({'path_id':path['id'],'segment_index':index,'t0':float(t0),'t1':float(t1),'a':p0,'b':p1})
    return entries


def _validate_fold_edge(fold_edge,candidate,lookup):
    """Validate a confirmed source fold edge and return exact skip intervals.

    The edge must be the complete connected straight outer-boundary interval
    of this candidate, not a middle share of a longer collinear edge. Every
    declared fragment must be covered by this candidate's own source refs,
    follow the same axis and cover it without gaps. Coincident support that is
    not declared is rejected so no residual fold line remains in the output.
    """
    if not isinstance(fold_edge,dict):_fold_error()
    a=_fold_point(fold_edge.get('a'));b=_fold_point(fold_edge.get('b'))
    length=math.dist(a,b)
    entries=_fold_candidate_entries(candidate,lookup)
    tol=_fold_tolerance(a,b,[value for entry in entries for value in entry['a']+entry['b']])
    if length<=tol:_fold_error()
    direction=((b[0]-a[0])/length,(b[1]-a[1])/length)
    def signed(point):return -direction[1]*(point[0]-a[0])+direction[0]*(point[1]-a[1])
    def along(point):return direction[0]*(point[0]-a[0])+direction[1]*(point[1]-a[1])
    raw=fold_edge.get('fragments')
    if not isinstance(raw,list) or not raw:_fold_error()
    candidate_intervals={}
    for entry in entries:
        candidate_intervals.setdefault((entry['path_id'],entry['segment_index']),[]).append((entry['t0'],entry['t1']))
    candidate_intervals={key:_fold_merge(parts) for key,parts in candidate_intervals.items()}
    fragments=[];projected=[]
    for item in raw:
        if not isinstance(item,dict):_fold_error()
        path_id=item.get('path_id');index=item.get('segment_index')
        if not isinstance(path_id,str) or not path_id:_fold_error()
        path=lookup.get(path_id)
        if path is None or isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(path['points'])-1:_fold_error()
        t0,t1=_fold_number(item.get('t0')),_fold_number(item.get('t1'))
        if not 0<=t0<t1<=1:_fold_error()
        p0,p1=_fold_interval_point(path,index,t0),_fold_interval_point(path,index,t1)
        if math.dist(p0,p1)<=tol:_fold_error()
        if abs(signed(p0))>tol or abs(signed(p1))>tol:_fold_error()
        u0,u1=along(p0)/length,along(p1)/length
        tol_u=tol/length
        if min(u0,u1)<-tol_u or max(u0,u1)>1+tol_u:_fold_error()
        if not _fold_covered(candidate_intervals.get((path_id,index),[]),t0,t1):_fold_error()
        fragments.append({'path_id':path_id,'segment_index':index,'t0':t0,'t1':t1})
        projected.append((max(0.,min(u0,u1)),min(1.,max(u0,u1))))
    cursor=0.;tol_u=tol/length
    for lo,hi in _fold_merge(projected):
        if lo>cursor+tol_u:_fold_error()
        cursor=max(cursor,hi)
    if cursor<1-tol_u:_fold_error()
    skip={}
    for fragment in fragments:
        skip.setdefault((fragment['path_id'],fragment['segment_index']),[]).append((fragment['t0'],fragment['t1']))
    skip={key:_fold_merge(parts) for key,parts in skip.items()}
    # Any coincident candidate support inside this axis interval must be part
    # of the confirmed skip set; otherwise the exported fold line would retain
    # a residual duplicate or a different source path's edge.
    for entry in entries:
        if abs(signed(entry['a']))>tol or abs(signed(entry['b']))>tol:continue
        dx,dy=entry['b'][0]-entry['a'][0],entry['b'][1]-entry['a'][1]
        segment_length=math.hypot(dx,dy)
        if segment_length<=tol:continue
        s0=((a[0]-entry['a'][0])*dx+(a[1]-entry['a'][1])*dy)/(segment_length*segment_length)
        s1=((b[0]-entry['a'][0])*dx+(b[1]-entry['a'][1])*dy)/(segment_length*segment_length)
        lo,hi=max(0.,min(s0,s1)),min(1.,max(s0,s1))
        if hi-lo<=tol/segment_length:continue
        start=entry['t0']+(entry['t1']-entry['t0'])*lo
        end=entry['t0']+(entry['t1']-entry['t0'])*hi
        if not _fold_covered(skip.get((entry['path_id'],entry['segment_index']),[]),start,end):_fold_error()
    # The selected edge must be the complete connected collinear boundary
    # interval of this candidate. Evidence comes from the candidate's own
    # source refs, so a compact project without private outer geometry works.
    axis_intervals=[]
    for entry in entries:
        if abs(signed(entry['a']))<=tol and abs(signed(entry['b']))<=tol:
            u0,u1=along(entry['a'])/length,along(entry['b'])/length
            axis_intervals.append((min(u0,u1),max(u0,u1)))
    positive=negative=False
    for entry in entries:
        for point in (entry['a'],entry['b']):
            distance=signed(point)
            if distance>tol:positive=True
            elif distance<-tol:negative=True
    if positive and negative:_fold_error()
    if not positive and not negative:_fold_error()
    host=None
    for lo,hi in _fold_merge(axis_intervals,tol_u):
        if lo<=tol_u and hi>=1-tol_u:
            host=(lo,hi);break
    if host is None:_fold_error()
    if host[0]<-tol_u or host[1]>1+tol_u:_fold_error()
    return {'a':a,'b':b,'axis':(a,direction),'tol':tol,'skip':skip,'fragments':fragments}


def _fabric_lay_mode(document):
    fabric=document.get('fabric')
    if not isinstance(fabric,dict):_fold_error()
    lay_mode=fabric.get('lay_mode')
    if lay_mode not in ('single','folded'):_fold_error()
    for name in ('width_mm','length_mm'):
        value=fabric.get(name)
        if value is None:_fold_error()
        value=_fold_number(value)
        if value<=0:_fold_error()
    return lay_mode


def _fabric_fold_side(document,schema):
    fabric=document.get('fabric')
    if isinstance(fabric,dict) and 'fold_side' in fabric:
        fold_side=fabric['fold_side']
    else:
        if schema==3:_fold_error()
        fold_side='top'
    if fold_side not in ('top','bottom'):_fold_error()
    return fold_side


def _placement_cut_mode(placement,lay_mode,schema):
    if schema==1:
        if 'cut_mode' in placement or 'fold_edge' in placement:_fold_error()
        return 'normal'
    cut_mode=placement.get('cut_mode','normal')
    if cut_mode not in ('normal','on_fold','unfolded'):_fold_error()
    if cut_mode=='on_fold':
        if lay_mode!='folded' or placement.get('fold_edge') is None:_fold_error()
    elif cut_mode=='unfolded':
        if lay_mode!='single' or placement.get('fold_edge') is None:_fold_error()
    return cut_mode


def _fold_survivors(fragments,skip):
    survivors=[]
    for fragment in fragments:
        key=(fragment['path_id'],fragment['segment_index'])
        t0,t1=fragment['t0'],fragment['t1']
        parts=_fold_subtract(float(t0),float(t1),skip.get(key,[])) if key in skip else [(t0,t1)]
        for start,end in parts:
            if start==t0 and end==t1:survivors.append(fragment)
            else:
                derived=dict(fragment);derived['t0']=start;derived['t1']=end;survivors.append(derived)
    return survivors


def _fold_export_fragments(candidate,cut_mode,fold):
    """Return exact fragments plus a chain id and source-reflection flag.

    on_fold keeps one open contour (the fold edge is not cut); unfolded adds
    the reflected non-fold geometry as the same instance transform. normal
    never changes the cut line even when it carries a confirmed fold edge.
    """
    fragments=candidate.get('fragments', [])
    if cut_mode=='normal' or fold is None:
        return [(fragment,0,False) for fragment in fragments]
    survivors=_fold_survivors(fragments,fold['skip'])
    export=[(fragment,0,False) for fragment in survivors]
    if cut_mode=='unfolded':export.extend((fragment,1,True) for fragment in survivors)
    return export


def export_project(source_bytes, document):
    """Export only exact referenced intervals; unknown/lossy semantics fail closed."""
    schema=document.get('schema_version')
    if (isinstance(schema,bool) or schema not in (1,2,3)
            or document.get('source_sha256') != hashlib.sha256(source_bytes).hexdigest()):
        raise PatternLayoutExportError('invalid_source_identity')
    placements=document.get('placements', [])
    if schema==1:
        # Legacy documents keep their exact output, including fabric width and
        # length metadata. Only v2 fold semantics cannot be silently ignored.
        fabric=document.get('fabric')
        if isinstance(fabric,dict) and 'lay_mode' in fabric:
            _fold_error()
        if any(isinstance(placement,dict) and ('cut_mode' in placement or 'fold_edge' in placement) for placement in placements):
            _fold_error()
        lay_mode=None
        fold_side=_fabric_fold_side(document,schema)
    else:
        lay_mode=_fabric_lay_mode(document)
        fold_side=_fabric_fold_side(document,schema)
        if lay_mode=='folded' and fold_side=='bottom' and schema!=3:_fold_error()
    # 旧工程按保存时的指令边界和坐标解释重建源线。
    flags = layout_parse_flags(document.get('source_metadata'))
    flags.update(layout_parse_flags(document))
    parsed, paths = source_paths(source_bytes,
                                 apply_page_offsets=flags.get('page_x_offsets_applied', False),
                                 apply_command_boundaries=flags.get('layout_command_boundaries_applied', False),
                                 command_boundaries_version=flags.get('layout_command_boundaries_version', 1))
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
    for placement in placements:
        candidate = candidates.get(placement['piece_id'])
        if candidate is None: raise PatternLayoutExportError('invalid_source_fragment')
        angle = finite(placement.get('rotation_deg',0), 'rotation_deg')
        rad = math.radians(angle); c,s = math.cos(rad), math.sin(rad)
        mirror = placement.get('mirror_x',False)
        if not isinstance(mirror,bool): raise PatternLayoutExportError('invalid_numeric_value')
        anchor = placement.get('anchor', {'x':0,'y':0})
        ax,ay = finite(anchor['x'],'anchor.x'),finite(anchor['y'],'anchor.y')
        dx,dy = finite(placement['x'],'x'), finite(placement['y'],'y')
        def layout_point(p):
            x,y = p[0]-ax,p[1]-ay
            if mirror: x=-x
            return c*x-s*y+dx, s*x+c*y+dy
        def transform(p):
            layout=layout_point(p)
            return [layout[0]*units, -layout[1]*units]
        cut_mode=_placement_cut_mode(placement,lay_mode,schema)
        fold=None
        if placement.get('fold_edge') is not None:
            fold=_validate_fold_edge(placement['fold_edge'],candidate,lookup)
        export_fragments=_fold_export_fragments(candidate,cut_mode,fold)
        if cut_mode=='on_fold':
            # Layout millimetres keep the recognition y-down convention; top
            # folds at y=0 with the piece on y>=0, bottom folds at half the
            # unfolded fabric width with the piece on y<=width_mm/2. Other
            # width/length bounds are intentionally not blocking here.
            fold_y=0
            if fold_side=='bottom':
                fold_y=_fold_number(document['fabric']['width_mm'])/2
            for edge_point in (fold['a'],fold['b']):
                if abs(layout_point(edge_point)[1]-fold_y)>fold['tol']:_fold_error()
            for fragment,_chain,reflected in export_fragments:
                path=lookup.get(fragment['path_id'])
                for t in (fragment['t0'],fragment['t1']):
                    y=layout_point(_fold_export_point(path,fragment['segment_index'],t,reflected,fold['axis']))[1]
                    if fold_side=='top':
                        if y<-fold['tol']:_fold_error()
                    elif y>fold_y+fold['tol']:_fold_error()
        previous = None
        for fragment,chain,reflected in export_fragments:
            path = lookup.get(fragment['path_id'])
            index = fragment['segment_index']
            if path is None or isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(path['points'])-1:
                raise PatternLayoutExportError('invalid_source_fragment')
            t0,t1 = finite(fragment['t0'],'t0'),finite(fragment['t1'],'t1')
            if not 0<=t0<t1<=1: raise PatternLayoutExportError('invalid_source_fragment')
            a,b = path['points'][index:index+2]
            pa,pb = transform(_fold_export_point(path,index,t0,reflected,fold['axis'] if fold is not None else None)),transform(_fold_export_point(path,index,t1,reflected,fold['axis'] if fold is not None else None))
            style = path['segment_styles'][index]
            lt = style['lt_argument'] or ''
            key = (path['id'],index,t1,lt,style.get('lt_generation'),style.get('line_width_units',path.get('line_width_units')),chain)
            continuous = previous is not None and previous[6]==chain and previous[0]==path['id'] and previous[3]==lt and previous[4]==style.get('lt_generation') and ((previous[1]==index and previous[2]==t0) or (previous[1]+1==index and previous[2]==1 and t0==0))
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
                            axis=fold['axis'] if fold is not None else None
                            da=transform(_fold_export_point(path,index,u0,reflected,axis))
                            db=transform(_fold_export_point(path,index,u1,reflected,axis))
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
