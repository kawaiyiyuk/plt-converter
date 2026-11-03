"""纯闭合轮廓试验：原单环、多段闭壳、嵌套家族与真实多边形内全部线段。"""
from pathlib import Path
import json,math,hashlib,argparse,collections
from shapely.geometry import LineString,Polygon,box,GeometryCollection,mapping
from shapely.ops import unary_union,polygonize_full
from .quality_guards import apply_quality_guards
from .independent_shells import discover,whole_path_owners,support_refs
from .layout_marker_v5 import classify_layout_markers
# AI 实验假设：仅扩大疑框边界和近包含关系容差；不证明裁片语义。
# 原点、完整源环、内线与待核观察记录仍按冻结 V5 逻辑保留。
PARAMS={'numeric_epsilon_plotter_units':1e-7,'near_containment_outside_mm2':10.,'near_containment_relative':.0002,'frame_body_witness_area_mm2':1000.,'frame_boundary_fraction':.05,'circle_arc_chord_error_mm':.05,'whole_path_near_band_plotter_units':2.,'structural_shell_area_epsilon_mm2':1e-6,'layout_marker_min_span_to_symbol_ratio':20.0,'min_candidate_area_mm2':100.}
ROOT=Path(__file__).resolve().parents[1]
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def bounds_of_points(pts):
    if not pts:return None
    x,y=zip(*pts);return {'x':min(x),'y':min(y),'w':max(x)-min(x),'h':max(y)-min(y)}
def polygon_coords(p):return [list(v) for v in p.exterior.coords]
def segments(line):
    if line.is_empty:return []
    if line.geom_type in ('LineString','LinearRing'):return [line]
    return [q for g in getattr(line,'geoms',[]) for q in segments(g)]
def interval_parts(a,b,region,eps):
    line=LineString([a,b]);length=line.length
    if length<=eps:return []
    result=[]
    for part in segments(line.intersection(region)):
        if part.length<=eps:continue
        ts=[line.project(__import__('shapely').geometry.Point(p))/length for p in part.coords];result.append((max(0,min(ts)),min(1,max(ts))))
    return sorted(result)
def merge_intervals(parts,eps=1e-12):
    out=[]
    for a,b in sorted(parts):
        if out and a<=out[-1][1]+eps:out[-1][1]=max(b,out[-1][1])
        else:out.append([a,b])
    return out
def complement(parts):
    parts=merge_intervals(parts);out=[];cursor=0.
    for a,b in parts:
        if a>cursor+1e-12:out.append((cursor,a))
        cursor=max(cursor,b)
    if cursor<1-1e-12:out.append((cursor,1.))
    return out
def build_geometry(paths,width,height,units):
    eps=PARAMS['numeric_epsilon_plotter_units']/units;lines={p['id']:LineString(p['points']) for p in paths if len(p['points'])>=2};byid={p['id']:p for p in paths};loops=[]
    for pid,line in lines.items():
        if line.is_ring and line.is_valid:
            poly=Polygon(line)
            if poly.is_valid and poly.area>eps**2:
                existing=next((l for l in loops if l['poly'].equals(poly)),None)
                if existing:existing['source_ids'].append(pid)
                else:loops.append({'poly':poly,'source_ids':[pid],'kind':'original_closed_path','id':f'L{len(loops)+1:04d}'})
    # 只有近整图四边的精确源矩形、多个独立主体见证且无真实共边，才单列疑框。
    frames=[]
    for l in loops:
        p=l['poly'];x0,y0,x1,y1=p.bounds
        near=width>0 and height>0 and max(abs(x0),abs(width-x1))<=width*PARAMS['frame_boundary_fraction'] and max(abs(y0),abs(height-y1))<=height*PARAMS['frame_boundary_fraction']
        witnesses=[q for q in loops if q is not l and q['poly'].area>=PARAMS['frame_body_witness_area_mm2'] and q['poly'].area<p.area and p.covers(q['poly'].representative_point())]
        independent=any(not a['poly'].covers(b['poly']) and not b['poly'].covers(a['poly']) and a['poly'].intersection(b['poly']).area<=eps**2 for i,a in enumerate(witnesses) for b in witnesses[:i]);no_shared=all(p.boundary.intersection(q['poly'].boundary).length<=eps for q in witnesses)
        if p.equals(p.envelope) and near and independent and no_shared:frames.append(l)
    frame_ids={id for l in frames for id in l['source_ids']}
    original=[l for l in loops if l not in frames]
    usable,alternatives,discovery=discover(paths,original,frame_ids,PARAMS,eps)
    min_candidate_area=PARAMS['min_candidate_area_mm2']
    ignored_closed=[l for l in usable if l['poly'].area<min_candidate_area];usable=[l for l in usable if l['poly'].area>=min_candidate_area]
    ignored_alternatives=[l for l in alternatives if l['poly'].area<min_candidate_area];alternatives=[l for l in alternatives if l['poly'].area>=min_candidate_area]
    loops=frames+usable
    net=unary_union([line for id,line in lines.items() if id not in frame_ids]);faces,cuts,dangles,invalid=polygonize_full(net)
    # 大环完整/近包含小环只构成家族假设，原点与原外环不移动。
    parents={l['id']:[] for l in usable};near_relations=[]
    for small in usable:
        for large in usable:
            if large['poly'].area<=small['poly'].area:continue
            outside=small['poly'].difference(large['poly']).area
            if outside<=PARAMS['near_containment_outside_mm2'] and outside/small['poly'].area<=PARAMS['near_containment_relative']:
                parents[small['id']].append(large['id']);near_relations.append({'inner_loop_id':small['id'],'outer_loop_id':large['id'],'outside_area_mm2':outside,'exact_containment':outside==0})
    roots=[l for l in usable if not parents[l['id']]];loop_byid={l['id']:l for l in usable};families={}
    for root in roots:
        ids={root['id']};changed=True
        while changed:
            changed=False
            for l in usable:
                if l['id'] not in ids and any(p in ids for p in parents[l['id']]):ids.add(l['id']);changed=True
        families[root['id']]=ids
    path_families=collections.defaultdict(set)
    for rid,ids in families.items():
        for lid in ids:
            for pid in loop_byid[lid]['source_ids']:path_families[pid].add(rid)
    whole_owners,ownership_audit=whole_path_owners(paths,roots,families,loop_byid,units,eps,PARAMS)
    selected=collections.defaultdict(list);pieces=[];auxiliary=[];outside_records=[];source_total=0.
    def make_piece(loop,pid,status,member_ids):
        p=loop['poly'];fragments=[];issues=[];crossings=[];member_paths={source for lid in member_ids for source in loop_byid[lid]['source_ids']};foreign=[]
        # 内容域覆盖整个近包含家族；仅用原闭环区域并集，不移动点或替换 root 外环。
        # whole_owners、跨片共边及 on_boundary 仍以原 root 为准，扩大内容域不授予外片所有权。
        content_region=unary_union([loop_byid[lid]['poly'] for lid in sorted(member_ids)])
        added_region=content_region.difference(p)
        contacts=[]
        if not added_region.is_empty:
            for other in roots:
                if other['id']==loop['id']:continue
                if added_region.intersects(other['poly']):
                    hit=added_region.intersection(other['poly'])
                    contacts.append({'other_loop_id':other['id'],'overlap_area_mm2':hit.area,'intersection_geometry_length_mm':hit.length,'intersection_geometry_type':hit.geom_type,'automatic_exclusive_owner_chosen':False})
        content_audit={'method':'union_of_original_family_regions_for_content_only','root_loop_id':loop['id'],'family_loop_ids':sorted(member_ids),'root_area_mm2':p.area,'content_region_area_mm2':content_region.area,'outside_root_area_mm2':added_region.area,'source_points_modified':False,'outer_replaced':False,'buffer_or_gap_fill_used':False,'on_boundary_reference':'original_root_outer','other_root_contacts':contacts}
        if contacts:issues.append({'code':'family_content_region_contacts_other_root_needs_review','detail':contacts})
        for source in paths:
            sid=source['id'];points=source['points'];distance=0.
            if status=='suspected_layout_frame' and sid not in member_paths:continue
            for si,(a,b) in enumerate(zip(points,points[1:])):
                segment=LineString([a,b]);length=segment.length;style=source['segment_styles'][si] if si<len(source['segment_styles']) else None
                if length<=eps:continue
                inside=interval_parts(a,b,content_region,eps)
                if sid in frame_ids and status!='suspected_layout_frame':
                    if inside:foreign.append({'path_id':sid,'segment_index':si,'reason':'suspected_frame_kept_separately','inside_intervals':inside})
                    distance+=length;continue
                own=whole_owners.get(sid)==loop['id'] or status=='suspected_layout_frame' and sid in member_paths
                other_owner=whole_owners.get(sid)
                blocked=(other_owner is not None and other_owner!=loop['id']) or (path_families.get(sid) and loop['id'] not in path_families[sid])
                if blocked and status!='suspected_layout_frame':
                    possible=[other_owner] if other_owner else sorted(path_families[sid])
                    shared_boundary=unary_union([p.boundary.intersection(loop_byid[rid]['poly'].boundary) for rid in possible if rid in loop_byid])
                    parts=interval_parts(a,b,shared_boundary,eps)
                    if inside:foreign.append({'path_id':sid,'segment_index':si,'reason':'unique_structural_owner_or_foreign_family_only_shared_boundary_may_be_reused','inside_intervals':inside,'shared_boundary_intervals':parts})
                else:parts=[(0.,1.)] if own else inside
                if own and segment.difference(p).length>eps:crossings.append({'path_id':sid,'segment_index':si,'outside_length_mm':segment.difference(p).length,'preserved_complete_family_member':True})
                elif inside and segment.difference(p).length>eps:crossings.append({'path_id':sid,'segment_index':si,'outside_length_mm':segment.difference(p).length,'preserved_complete_family_member':False})
                for t0,t1 in parts:
                    aa=[a[k]+(b[k]-a[k])*t0 for k in (0,1)];bb=[a[k]+(b[k]-a[k])*t1 for k in (0,1)];on_boundary=LineString([aa,bb]).difference(p.boundary).length<=eps
                    f={'path_id':sid,'segment_index':si,'t0':t0,'t1':t1,'a':aa,'b':bb,'style':style,'distance_from_path_start_mm':distance+t0*length,'on_boundary':on_boundary,'assignment':'original_family_member' if own else ('family_region_inside_or_boundary' if LineString([aa,bb]).difference(p).length>eps else 'polygon_inside_or_boundary')};fragments.append(f)
                    if status!='nested_content_loop' and status!='mixed_shell_alternative':selected[(sid,si)].append({'piece_id':pid,'t0':t0,'t1':t1,'on_boundary':on_boundary})
                distance+=length
        conflicted_ids=sorted({f['path_id'] for f in fragments}&set(ownership_audit.get('original_closed_family_conflict_path_ids',[])))
        if conflicted_ids:issues.append({'code':'original_closed_path_family_conflict','path_ids':conflicted_ids,'action':'interval_only_multi_owner_needs_review'})
        if crossings:issues.append({'code':'external_or_cross_boundary_segments','count':len(crossings),'detail':crossings})
        if foreign:issues.append({'code':'foreign_closed_contour_or_frame_intersections_not_borrowed','count':len(foreign),'detail':foreign})
        content_audit['selected_outside_root_length_mm']=sum(LineString([f['a'],f['b']]).difference(p).length for f in fragments)
        points=[point for f in fragments for point in (f['a'],f['b'])];b=bounds_of_points(points) or bounds_of_points(polygon_coords(p));return {'id':pid,'status':status,'bounds':b,'outer':polygon_coords(p),'fragments':fragments,'issues':issues,'loop_id':loop['id'],'loop_kind':loop['kind'],'source_evidence':loop.get('proofs',[]),'content_region_audit':content_audit,'area_mm2':p.area,'family_loop_ids':sorted(member_ids),'source_closed_path_ids':sorted(member_paths),'semantic_piece_verified':False}
    for i,l in enumerate(roots):pieces.append(make_piece(l,f'P{i+1:03d}','closed_shell' if l['kind']=='original_closed_path' else 'multi_path_closed_shell_needs_review',families[l['id']]))
    for i,l in enumerate(frames):
        loop_byid[l['id']]=l;auxiliary.append(make_piece(l,f'A{i+1:03d}','suspected_layout_frame',{l['id']}))
    def original_path_fragments(ids):
        result=[]
        for sid in ids:
            source=byid[sid];distance=0.
            for si,(a,b) in enumerate(zip(source['points'],source['points'][1:])):
                length=math.dist(a,b)
                if length>eps:result.append({'path_id':sid,'segment_index':si,'t0':0.,'t1':1.,'a':a,'b':b,'style':source['segment_styles'][si] if si<len(source['segment_styles']) else None,'distance_from_path_start_mm':distance,'assignment':'auxiliary_original_source_observation_only'})
                distance+=length
        return result
    for l in usable:
        if l in roots:continue
        # 内部闭环全部入观察账；片内容已由外壳选择，不把它们静默删掉。
        auxiliary.append({'id':f'A{len(auxiliary)+1:03d}','status':'nested_content_loop','bounds':bounds_of_points(polygon_coords(l['poly'])),'outer':polygon_coords(l['poly']),'fragments':original_path_fragments(l['source_ids']),'issues':[{'code':'nested_loop_included_in_outer_content_not_separate_semantic_piece'}],'loop_id':l['id'],'source_closed_path_ids':l['source_ids'],'area_mm2':l['poly'].area,'parent_loop_ids':parents[l['id']]})
    for l in alternatives:auxiliary.append({'id':f'A{len(auxiliary)+1:03d}','status':l['kind'],'proof_level':l.get('proof_level'),'bounds':bounds_of_points(polygon_coords(l['poly'])),'outer':polygon_coords(l['poly']),'fragments':support_refs(l['poly'],paths,eps),'issues':[{'code':l.get('reason','source_support_dependency')}],'source_evidence':l.get('proofs',[])})
    shared=[];multi=[];unassigned=[];zero=[]
    for source in paths:
        distance=0.
        for si,(a,b) in enumerate(zip(source['points'],source['points'][1:])):
            length=math.dist(a,b);source_total+=length;refs=selected[(source['id'],si)]
            if length<=eps:zero.append({'path_id':source['id'],'segment_index':si,'point':a});continue
            style=source['segment_styles'][si] if si<len(source['segment_styles']) else None
            for t0,t1 in complement([(r['t0'],r['t1']) for r in refs]):unassigned.append({'path_id':source['id'],'segment_index':si,'t0':t0,'t1':t1,'a':[a[k]+(b[k]-a[k])*t0 for k in (0,1)],'b':[a[k]+(b[k]-a[k])*t1 for k in (0,1)],'style':style,'distance_from_path_start_mm':distance+t0*length})
            cuts_t=sorted(set([v for r in refs for v in (r['t0'],r['t1'])]))
            for t0,t1 in zip(cuts_t,cuts_t[1:]):
                owners=[r for r in refs if r['t0']<=(t0+t1)/2<=r['t1']]
                if len(owners)>1:(shared if all(r['on_boundary'] for r in owners) else multi).append({'path_id':source['id'],'segment_index':si,'t0':t0,'t1':t1,'piece_ids':sorted({r['piece_id'] for r in owners}),'length_mm':(t1-t0)*length})
            distance+=length
    result={'pieces':pieces,'auxiliary':auxiliary,'issues':[{'code':'geometry_only_not_semantic_piece_truth'},{'code':'cross_piece_interior_intervals','count':len(multi)}],'unassigned':{'count':len(unassigned),'length_mm':sum(math.dist(f['a'],f['b']) for f in unassigned),'fragments':unassigned,'zero_length_segments':zero},'ownership_audit':ownership_audit,'source_discovery_audit':discovery,'boundary_audit':{'definitions':{'shared_segments':'原segment参数区间同时在多个owner的完整边界上；可同源双引用','multi_owner_interiors':'原segment参数区间同时选入>=2 owner且至少一方并非完整边界；不是裁片数/准确率'},'shared_interval_count':len(shared),'multi_owner_interval_count':len(multi),'multi_owner_distinct_source_segment_count':len({(f['path_id'],f['segment_index']) for f in multi}),'multi_owner_total_interval_length_mm':sum(f['length_mm'] for f in multi),'shared_segments':shared,'multi_owner_interiors':multi,'near_containment_relations':near_relations},'geometry_audit':{'original_closed_path_count':len([l for l in loops if l['kind']=='original_closed_path']),'polygonized_face_count':len(faces.geoms),'cut_count':len(cuts.geoms),'dangle_count':len(dangles.geoms),'invalid_ring_count':len(invalid.geoms),'source_total_line_length_mm':source_total,'frame_path_ids':sorted(frame_ids),'loop_registry':[{'id':l['id'],'kind':l['kind'],'source_path_ids':l['source_ids'],'outer':polygon_coords(l['poly']),'area_mm2':l['poly'].area} for l in loops],'source_points_moved':False,'gap_filling':False,'coordinate_snapping':False},'small_candidate_filter':{'threshold_mm2':min_candidate_area,'ignored_closed_loop_count':len(ignored_closed),'ignored_face_alternative_count':len(ignored_alternatives),'ignored_source_contour_count':0,'ignored_count':len(ignored_closed)+len(ignored_alternatives)}}
    return apply_quality_guards(result,paths,units,eps,PARAMS)
