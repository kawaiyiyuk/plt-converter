"""Structure-only verification of browser recognition results.

Never reruns geometry recognition. Candidate ownership and semantics remain the
browser's untrusted claims; the server only binds fragments to the original
source and recomputes bounds/text containment from submitted outers.
"""
import hashlib
import json
import math
import re
from shapely import make_valid
from shapely.geometry import Point, Polygon
from .pattern_layout.closed_loop import PARAMS, bounds_of_points
from .pattern_layout_api import ENGINE_VERSION, source_paths
from .pattern_layout.parse_source import layout_parse_flags
from .pattern_layout_fingerprint import recognition_code_version

FORMAT='pattern-layout-client-result'
FORMAT_VERSION=1
MAX_CANDIDATES=5000
MAX_FRAGMENTS=500000
MAX_OUTER_POINTS=50000
ID_PATTERN=re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
HEX64_PATTERN=re.compile(r'^[0-9a-f]{64}$')
RUNTIME_FIELDS=('pyodide','python','numpy','shapely','geos')
CANDIDATE_OPTIONAL=('parent_candidate_id','variant_label_cn','loop_id','source_closed_path_ids','area_mm2','excluded_rectangle','content_complete_for_region')


class ClientResultError(ValueError):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)


def invalid(message):raise ClientResultError('client_result_invalid',message)


def finite_number(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)


def is_integer(value):
    return not isinstance(value,bool) and isinstance(value,int)


def is_scalar(value):
    return value is None or isinstance(value,(str,bool)) or finite_number(value)


def require_finite_tree(value):
    stack=[value]
    while stack:
        item=stack.pop()
        if isinstance(item,dict):stack.extend(item.values())
        elif isinstance(item,list):stack.extend(item)
        elif isinstance(item,float) and not math.isfinite(item):invalid('数字必须有限')
        elif not (item is None or isinstance(item,(str,int,float,bool))):invalid('结果包含不支持的JSON类型')


def text_ids_for(outer,non_path_shapes,metrics,units):
    if len(outer)<3:return []
    polygon=make_valid(Polygon(outer))
    return [shape['id'] for shape in non_path_shapes if shape['type']=='text' and polygon.covers(Point((shape['point']['x']-metrics['min_x'])/units,(metrics['max_y']-shape['point']['y'])/units))]


def verify_client_result(raw_plt,payload):
    if not isinstance(payload,dict):invalid('结果必须为对象')
    require_finite_tree(payload)
    if payload.get('format')!=FORMAT or payload.get('format_version')!=FORMAT_VERSION:invalid('结果格式版本不受支持')
    source=payload.get('source');engine=payload.get('engine');unassigned=payload.get('unassigned')
    if not isinstance(source,dict) or not isinstance(engine,dict) or not isinstance(unassigned,dict):invalid('结果缺少source/engine/unassigned')
    pieces=payload.get('pieces');auxiliary=payload.get('auxiliary')
    if not isinstance(pieces,list) or not isinstance(auxiliary,list):invalid('pieces/auxiliary必须为数组')
    source_sha=source.get('sha256')
    if not isinstance(source_sha,str) or not HEX64_PATTERN.fullmatch(source_sha):invalid('source.sha256无效')
    name=source.get('name')
    if not isinstance(name,str) or len(name)>255:invalid('source.name无效')
    path_count=source.get('path_count');text_count=source.get('text_count')
    if not is_integer(path_count) or not is_integer(text_count):invalid('source计数无效')
    warnings=source.get('warnings')
    if not isinstance(warnings,list) or any(not isinstance(warning,str) for warning in warnings):invalid('source.warnings无效')
    engine_version=engine.get('engine_version')
    if not isinstance(engine_version,str) or len(engine_version)>128:invalid('engine.engine_version无效')
    code_version=engine.get('code_version');runtime_version_value=engine.get('runtime_version')
    if not isinstance(code_version,str) or not HEX64_PATTERN.fullmatch(code_version):invalid('engine.code_version无效')
    if not isinstance(runtime_version_value,str) or not HEX64_PATTERN.fullmatch(runtime_version_value):invalid('engine.runtime_version无效')
    client_runtime=engine.get('runtime')
    if not isinstance(client_runtime,dict) or any(not isinstance(client_runtime.get(key),str) for key in RUNTIME_FIELDS):invalid('engine.runtime无效')
    if hashlib.sha256(raw_plt).hexdigest()!=source_sha:raise ClientResultError('source_sha_mismatch','原件与浏览器识别结果不一致')
    if engine_version!=ENGINE_VERSION or code_version!=recognition_code_version():raise ClientResultError('engine_code_mismatch','浏览器识别代码版本与服务端不一致')
    parsed,paths=source_paths(raw_plt)
    metrics=parsed['metrics'];units=metrics['units_per_inch']/25.4
    non_path_shapes=[shape for shape in parsed['shapes'] if 'points' not in shape]
    text_shapes=[shape for shape in non_path_shapes if shape['type']=='text']
    if len(paths)!=path_count or len(text_shapes)!=text_count or list(parsed['warnings'])!=warnings:raise ClientResultError('parser_mismatch','浏览器解析结果与服务端原件不一致')
    if len(pieces)+len(auxiliary)>MAX_CANDIDATES:invalid('候选总数超过上限')
    total_fragments=sum(len(candidate.get('fragments',())) if isinstance(candidate,dict) and isinstance(candidate.get('fragments'),list) else 0 for candidate in pieces+auxiliary)
    if total_fragments>MAX_FRAGMENTS:invalid('候选片段总数超过上限')
    by_id={path['id']:path for path in paths};seen=set();minimum_x=metrics['min_x'];maximum_y=metrics['max_y'];width=metrics['width_mm'];height=metrics['height_mm']
    def convert(candidate,with_text):
        if not isinstance(candidate,dict):invalid('候选必须为对象')
        identifier=candidate.get('id')
        if not isinstance(identifier,str) or not ID_PATTERN.fullmatch(identifier):invalid('候选id无效')
        if identifier in seen:invalid('候选id重复')
        seen.add(identifier)
        status=candidate.get('status')
        if not isinstance(status,str) or len(status)>64:invalid('候选status无效')
        fragments=[];raw_fragments=candidate.get('fragments')
        if not isinstance(raw_fragments,list):invalid('候选fragments必须为数组')
        for fragment in raw_fragments:
            if not isinstance(fragment,list) or len(fragment)!=4:invalid('片段结构无效')
            path_id,segment_index,t0,t1=fragment
            if not isinstance(path_id,str):invalid('片段path_id无效')
            if not finite_number(t0) or not finite_number(t1):invalid('片段参数无效')
            if not is_integer(segment_index):raise ClientResultError('invalid_source_fragment','片段引用无效')
            path=by_id.get(path_id)
            if path is None or not 0<=segment_index<len(path['points'])-1 or segment_index>=len(path['segment_styles']):raise ClientResultError('invalid_source_fragment','片段引用无效')
            if not 0<=t0<t1<=1:raise ClientResultError('invalid_source_fragment','片段引用无效')
            a,b=path['points'][segment_index:segment_index+2]
            fragments.append({'path_id':path_id,'segment_index':segment_index,'t0':t0,'t1':t1,
                              'a':[a[k]+(b[k]-a[k])*t0 for k in (0,1)],'b':[a[k]+(b[k]-a[k])*t1 for k in (0,1)],
                              'style':path['segment_styles'][segment_index],'assignment':'client_result'})
        outer=candidate.get('outer')
        if not isinstance(outer,list):invalid('候选outer必须为数组')
        if len(outer)>MAX_OUTER_POINTS:invalid('候选outer点数超过上限')
        points=[]
        for point in outer:
            if not isinstance(point,list) or len(point)!=2 or not finite_number(point[0]) or not finite_number(point[1]):invalid('候选outer点无效')
            x,y=point
            if not -1<=x<=width+1 or not -1<=y<=height+1:raise ClientResultError('outer_out_of_bounds','浏览器识别轮廓超出原件范围')
            points.append([x,y])
        # 浏览器结果同服务端识别一样不声明语义裁片已核实。
        result={'id':identifier,'status':status,'outer':points,'fragments':fragments,'semantic_piece_verified':False}
        bound_points=points if points else [value for fragment in fragments for value in (fragment['a'],fragment['b'])]
        result['bounds']=bounds_of_points(bound_points)
        issues=candidate.get('issues')
        if issues is not None:
            if not isinstance(issues,list):invalid('候选issues必须为数组')
            cleaned=[]
            for issue in issues:
                if not isinstance(issue,dict) or not isinstance(issue.get('code'),str) or len(issue['code'])>96:invalid('候选issue无效')
                cleaned.append({'code':issue['code']})
            result['issues']=cleaned
        for key in CANDIDATE_OPTIONAL:
            if key not in candidate:continue
            value=candidate[key]
            if key=='parent_candidate_id' and not isinstance(value,str):invalid('parent_candidate_id无效')
            if key=='variant_label_cn' and (not isinstance(value,str) or len(value)>64):invalid('variant_label_cn无效')
            if key=='loop_id' and not is_scalar(value):invalid('loop_id无效')
            if key=='source_closed_path_ids' and (not isinstance(value,list) or any(not isinstance(item,str) for item in value)):invalid('source_closed_path_ids无效')
            if key=='area_mm2' and not finite_number(value):invalid('area_mm2无效')
            if key=='content_complete_for_region' and not isinstance(value,bool):invalid('content_complete_for_region无效')
            result[key]=value
        if with_text:result['text_ids']=text_ids_for(points,non_path_shapes,metrics,units)
        return result
    verified_pieces=[convert(candidate,True) for candidate in pieces]
    verified_auxiliary=[convert(candidate,False) for candidate in auxiliary]
    count=unassigned.get('count');length=unassigned.get('length_mm')
    count=count if is_integer(count) else None
    length=length if finite_number(length) else None
    result = {'schema_version':1,'pieces':verified_pieces,'auxiliary':verified_auxiliary,'issues':[],
            'unassigned':{'count':count,'length_mm':length},'paths':paths,'non_path_shapes':non_path_shapes,
            'source':{'sha256':source_sha,'name':name,'width_mm':width,'height_mm':height,'units_per_mm':units,
                      'raw_bounds':[metrics['min_x'],metrics['min_y'],metrics['max_x'],metrics['max_y']],
                      'warnings':list(parsed['warnings']),'algorithm_parameters':PARAMS,'semantic_verified':False,
                      'engine_version':ENGINE_VERSION,'runtime_version':runtime_version_value,
                      'recognition_origin':'browser','server_verification':'structure_only',
                      'client_runtime':{key:client_runtime[key] for key in RUNTIME_FIELDS},'code_version':code_version}}
    result['source'].update(layout_parse_flags(parsed))
    return result
