"""纯闭合轮廓试验：原单环、多段闭壳、嵌套家族与真实多边形内全部线段。"""
from pathlib import Path
import json,math,hashlib,argparse,collections
from shapely import prepare
from shapely.geometry import LineString,Polygon,Point,box,GeometryCollection,mapping
from shapely.ops import unary_union,polygonize_full
from .fragment_index import PathSpatialIndex
from .quality_guards import apply_quality_guards
from .independent_shells import discover,whole_path_owners,support_refs,report_progress
from .layout_marker_v5 import classify_layout_markers
# AI 实验假设：仅扩大疑框边界和近包含关系容差；不证明裁片语义。
# 原点、完整源环、内线与待核观察记录仍按冻结 V5 逻辑保留。
PARAMS={'numeric_epsilon_plotter_units':1e-7,'near_containment_outside_mm2':10.,'near_containment_relative':.0002,'frame_body_witness_area_mm2':1000.,'frame_boundary_fraction':.05,'frame_min_envelope_fill':.9,'circle_arc_chord_error_mm':.05,'whole_path_near_band_plotter_units':2.,'structural_shell_area_epsilon_mm2':1e-6,'layout_marker_min_span_to_symbol_ratio':20.0,'min_candidate_area_mm2':100.,'near_closed_tolerance_mm':1.,'multi_stroke_join_tolerance_mm':.1,'short_tip_max_plotter_units':400.,'short_tip_min_angle_deg':70.,'short_tip_max_angle_deg':110.}
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
def interval_parts(a,b,region,eps,bounds=None,*,source_length=None):
    if bounds is not None:
        if not bounds:return []
        x0,y0,x1,y1=bounds
        if max(a[0],b[0])<x0 or min(a[0],b[0])>x1 or max(a[1],b[1])<y0 or min(a[1],b[1])>y1:return []
    line = None
    if source_length is None:
        if math.dist(a,b)<=eps:return []
        line = LineString([a, b])
        length = line.length
    else:
        length = source_length
    if length<=eps:return []
    if line is None:
        line = LineString([a, b])
    result=[]
    for part in segments(line.intersection(region)):
        if part.length<=eps:continue
        ts=[line.project(Point(p))/length for p in part.coords];result.append((max(0,min(ts)),min(1,max(ts))))
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
def build_geometry(paths,width,height,units,*,include_full_support_audit=True,include_research_audits=True,on_progress=None,path_index=None):
    eps=PARAMS['numeric_epsilon_plotter_units']/units
    if path_index is None:
        lines={p['id']:LineString(p['points']) for p in paths if len(p['points'])>=2}
        path_index=PathSpatialIndex(paths,lines)
    else:
        lines={p['id']:path_index.lines[idx] for idx,p in enumerate(paths) if path_index.lines[idx] is not None}
    byid={p['id']:p for p in paths};loops=[]
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
    usable,alternatives,discovery=discover(paths,original,frame_ids,PARAMS,eps,
                                          include_full_support_audit=include_full_support_audit,include_research_audits=include_research_audits,on_progress=on_progress,path_index=path_index)
    min_candidate_area=PARAMS['min_candidate_area_mm2']
    ignored_closed=[l for l in usable if l['poly'].area<min_candidate_area];usable=[l for l in usable if l['poly'].area>=min_candidate_area]
    ignored_alternatives=[l for l in alternatives if l['poly'].area<min_candidate_area];alternatives=[l for l in alternatives if l['poly'].area>=min_candidate_area]
    loops=frames+usable
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
    piece_work_total = len(roots) + len(frames) + len(alternatives) + 1
    piece_work_done = 0
    report_progress(on_progress, 'pieces', 0, piece_work_total)
    whole_owners,ownership_audit=whole_path_owners(paths,roots,families,loop_byid,units,eps,PARAMS,path_index=path_index)
    conflicted_path_set=set(ownership_audit.get('original_closed_family_conflict_path_ids',[]))
    selected=collections.defaultdict(list);pieces=[];auxiliary=[];outside_records=[];source_total=0.
    shared_boundary_cache={}
    def make_piece(loop,pid,status,member_ids):
        p=loop['poly'];fragments=[];issues=[];crossings=[];member_paths={source for lid in member_ids for source in loop_byid[lid]['source_ids']};foreign=[]
        has_crossings=False;has_foreign=False;fragment_path_ids=set()
        # 内容域覆盖整个近包含家族；仅用原闭环区域并集，不移动点或替换 root 外环。
        # whole_owners、跨片共边及 on_boundary 仍以原 root 为准，扩大内容域不授予外片所有权。
        content_region=unary_union([loop_byid[lid]['poly'] for lid in sorted(member_ids)]);content_bounds=content_region.bounds
        added_region=content_region.difference(p)
        contacts=[]
        if not added_region.is_empty:
            for other in roots:
                if other['id']==loop['id']:continue
                if added_region.intersects(other['poly']):
                    if include_research_audits:
                        hit=added_region.intersection(other['poly'])
                        contacts.append({'other_loop_id':other['id'],'overlap_area_mm2':hit.area,'intersection_geometry_length_mm':hit.length,'intersection_geometry_type':hit.geom_type,'automatic_exclusive_owner_chosen':False})
                    else:
                        contacts.append(other['id'])
        if include_research_audits:
            content_audit={'method':'union_of_original_family_regions_for_content_only','root_loop_id':loop['id'],'family_loop_ids':sorted(member_ids),'root_area_mm2':p.area,'content_region_area_mm2':content_region.area,'outside_root_area_mm2':added_region.area,'source_points_modified':False,'outer_replaced':False,'buffer_or_gap_fill_used':False,'on_boundary_reference':'original_root_outer','other_root_contacts':contacts}
        else:
            content_audit=None
        if contacts:
            issues.append({'code':'family_content_region_contacts_other_root_needs_review','detail':contacts} if include_research_audits else {'code':'family_content_region_contacts_other_root_needs_review'})
        prepare(content_region);prepare(p)
        own_extra=member_paths if status=='suspected_layout_frame' else {sid for sid,owner in whole_owners.items() if owner==loop['id']}
        candidate_indices=path_index.query_path_indices(content_bounds,extra_ids=own_extra)
        track_selected=(status!='nested_content_loop' and status!='mixed_shell_alternative')
        for idx in candidate_indices:
            source=path_index.paths[idx];sid=source['id'];points=source['points'];styles=source['segment_styles']
            if status=='suspected_layout_frame' and sid not in member_paths:continue
            line=path_index.lines[idx];seg_lengths=path_index.seg_lengths[idx]
            own=(whole_owners.get(sid)==loop['id']) or (status=='suspected_layout_frame' and sid in member_paths)
            is_frame_other=(sid in frame_ids and status!='suspected_layout_frame')
            other_owner=whole_owners.get(sid)
            blocked=(other_owner is not None and other_owner!=loop['id']) or (bool(path_families.get(sid)) and loop['id'] not in path_families[sid])
            if not own and (line is None or content_region.disjoint(line)):continue
            # 整路径严格片内快路径：绝不触碰边界顶点，零拆段且逐片段等价。
            if line is not None and not is_frame_other and (own or not blocked) and p.contains_properly(line):
                distance=0.;assign='original_family_member' if own else 'polygon_inside_or_boundary'
                for si,(a,b) in enumerate(zip(points,points[1:])):
                    length=seg_lengths[si]
                    if length<=eps:continue
                    style=styles[si] if si<len(styles) else None
                    aa=[a[k]+(b[k]-a[k])*0. for k in (0,1)];bb=[a[k]+(b[k]-a[k])*1. for k in (0,1)]
                    if include_research_audits:
                        fragments.append({'path_id':sid,'segment_index':si,'t0':0.,'t1':1.,'a':aa,'b':bb,'style':style,'distance_from_path_start_mm':distance,'on_boundary':False,'assignment':assign})
                    else:
                        fragments.append({'path_id':sid,'segment_index':si,'t0':0.,'t1':1.,'a':aa,'b':bb,'style':style})
                    if track_selected:selected[(sid,si)].append({'piece_id':pid,'t0':0.,'t1':1.,'on_boundary':False})
                    fragment_path_ids.add(sid)
                    distance+=length
                continue
            shared_boundary=None;shared_bounds=None
            if blocked and not is_frame_other and status!='suspected_layout_frame':
                possible=tuple([other_owner] if other_owner else sorted(path_families[sid]))
                cache_key=(loop['id'],possible)
                cached_sb=shared_boundary_cache.get(cache_key)
                if cached_sb is None:
                    sb=unary_union([p.boundary.intersection(loop_byid[rid]['poly'].boundary) for rid in possible if rid in loop_byid])
                    sb_bounds=sb.bounds if not sb.is_empty else ()
                    if sb_bounds:prepare(sb)
                    cached_sb=(sb,sb_bounds);shared_boundary_cache[cache_key]=cached_sb
                shared_boundary,shared_bounds=cached_sb
            content_contains_line=line is not None and not is_frame_other and content_region.contains_properly(line)
            distance=0.
            for si,(a,b) in enumerate(zip(points,points[1:])):
                length=seg_lengths[si]
                if length<=eps:continue
                style=styles[si] if si<len(styles) else None
                inside=[(0.,1.)] if content_contains_line else interval_parts(a,b,content_region,eps,content_bounds,source_length=length)
                if is_frame_other:
                    if inside:
                        has_foreign=True
                        if include_research_audits:foreign.append({'path_id':sid,'segment_index':si,'reason':'suspected_frame_kept_separately','inside_intervals':inside})
                    distance+=length;continue
                if blocked and status!='suspected_layout_frame':
                    parts=[] if not shared_bounds else interval_parts(a,b,shared_boundary,eps,shared_bounds,source_length=length)
                    if inside:
                        has_foreign=True
                        if include_research_audits:foreign.append({'path_id':sid,'segment_index':si,'reason':'unique_structural_owner_or_foreign_family_only_shared_boundary_may_be_reused','inside_intervals':inside,'shared_boundary_intervals':parts})
                else:parts=[(0.,1.)] if own else inside
                if own or inside:
                    if include_research_audits or not has_crossings:
                        segment=LineString([a,b])
                        if not p.covers(segment):
                            outside_len=segment.difference(p).length
                            if outside_len>eps:
                                has_crossings=True
                                if include_research_audits:crossings.append({'path_id':sid,'segment_index':si,'outside_length_mm':outside_len,'preserved_complete_family_member':bool(own)})
                for t0,t1 in parts:
                    aa=[a[k]+(b[k]-a[k])*t0 for k in (0,1)];bb=[a[k]+(b[k]-a[k])*t1 for k in (0,1)]
                    if include_research_audits:
                        seg_sub=LineString([aa,bb])
                        on_boundary=seg_sub.difference(p.boundary).length<=eps
                        f={'path_id':sid,'segment_index':si,'t0':t0,'t1':t1,'a':aa,'b':bb,'style':style,'distance_from_path_start_mm':distance+t0*length,'on_boundary':on_boundary,'assignment':'original_family_member' if own else ('family_region_inside_or_boundary' if seg_sub.difference(p).length>eps else 'polygon_inside_or_boundary')}
                    else:
                        on_boundary=False
                        f={'path_id':sid,'segment_index':si,'t0':t0,'t1':t1,'a':aa,'b':bb,'style':style}
                    fragments.append(f);fragment_path_ids.add(sid)
                    if track_selected:selected[(sid,si)].append({'piece_id':pid,'t0':t0,'t1':t1,'on_boundary':on_boundary})
                distance+=length
        conflicted_ids=sorted(fragment_path_ids&conflicted_path_set)
        if conflicted_ids:issues.append({'code':'original_closed_path_family_conflict','path_ids':conflicted_ids,'action':'interval_only_multi_owner_needs_review'})
        if has_crossings:issues.append({'code':'external_or_cross_boundary_segments','count':len(crossings),'detail':crossings} if include_research_audits else {'code':'external_or_cross_boundary_segments'})
        if has_foreign:issues.append({'code':'foreign_closed_contour_or_frame_intersections_not_borrowed','count':len(foreign),'detail':foreign} if include_research_audits else {'code':'foreign_closed_contour_or_frame_intersections_not_borrowed'})
        if include_research_audits:
            content_audit['selected_outside_root_length_mm']=sum(LineString([f['a'],f['b']]).difference(p).length for f in fragments)
        points=[point for f in fragments for point in (f['a'],f['b'])];b=bounds_of_points(points) or bounds_of_points(polygon_coords(p))
        piece_dict={'id':pid,'status':status,'bounds':b,'outer':polygon_coords(p),'fragments':fragments,'issues':issues,'loop_id':loop['id'],'loop_kind':loop['kind'],'area_mm2':p.area,'family_loop_ids':sorted(member_ids),'source_closed_path_ids':sorted(member_paths),'semantic_piece_verified':False}
        if include_research_audits:
            piece_dict['source_evidence']=loop.get('proofs',[])
            piece_dict['content_region_audit']=content_audit
        return piece_dict
    for i,l in enumerate(roots):
        pieces.append(make_piece(l,f'P{i+1:03d}','closed_shell' if l['kind']=='original_closed_path' else 'multi_path_closed_shell_needs_review',families[l['id']]))
        piece_work_done += 1
        report_progress(on_progress, 'pieces', piece_work_done, piece_work_total)
    for i,l in enumerate(frames):
        loop_byid[l['id']]=l;auxiliary.append(make_piece(l,f'A{i+1:03d}','suspected_layout_frame',{l['id']}))
        piece_work_done += 1
        report_progress(on_progress, 'pieces', piece_work_done, piece_work_total)
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
    for l in alternatives:
        alt_item={'id':f'A{len(auxiliary)+1:03d}','status':l['kind'],'proof_level':l.get('proof_level'),'bounds':bounds_of_points(polygon_coords(l['poly'])),'outer':polygon_coords(l['poly']),'fragments':support_refs(l['poly'],paths,eps,path_index=path_index),'issues':[{'code':l.get('reason','source_support_dependency')}]}
        if include_research_audits:alt_item['source_evidence']=l.get('proofs',[])
        auxiliary.append(alt_item)
        piece_work_done += 1
        report_progress(on_progress, 'pieces', piece_work_done, piece_work_total)
    shared=[];multi=[];unassigned=[];zero=[];unassigned_count=0;unassigned_lengths=[]
    for idx,source in enumerate(paths):
        distance=0.;points=source['points'];lengths=path_index.seg_lengths[idx];sid=source['id']
        styles=source.get('segment_styles',())
        for si,(a,b) in enumerate(zip(points,points[1:])):
            length=lengths[si];source_total+=length;refs=selected[(sid,si)]
            if length<=eps:
                if include_research_audits:zero.append({'path_id':sid,'segment_index':si,'point':a})
                continue
            style=styles[si] if si<len(styles) else None
            dx=b[0]-a[0];dy=b[1]-a[1]
            for t0,t1 in complement([(r['t0'],r['t1']) for r in refs]):
                aa=[a[0]+dx*t0,a[1]+dy*t0];bb=[a[0]+dx*t1,a[1]+dy*t1]
                unassigned_count+=1
                unassigned_lengths.append(math.dist(aa,bb))
                if include_research_audits:
                    unassigned.append({'path_id':sid,'segment_index':si,'t0':t0,'t1':t1,'a':aa,'b':bb,'style':style,'distance_from_path_start_mm':distance+t0*length})
            if include_research_audits:
                cuts_t=sorted(set([v for r in refs for v in (r['t0'],r['t1'])]))
                for t0,t1 in zip(cuts_t,cuts_t[1:]):
                    owners=[r for r in refs if r['t0']<=(t0+t1)/2<=r['t1']]
                    if len(owners)>1:(shared if all(r['on_boundary'] for r in owners) else multi).append({'path_id':sid,'segment_index':si,'t0':t0,'t1':t1,'piece_ids':sorted({r['piece_id'] for r in owners}),'length_mm':(t1-t0)*length})
            distance+=length
    unassigned_length_mm=sum(unassigned_lengths)
    small_filter={'threshold_mm2':min_candidate_area,'ignored_closed_loop_count':len(ignored_closed),'ignored_face_alternative_count':len(ignored_alternatives),'ignored_source_contour_count':0,'ignored_count':len(ignored_closed)+len(ignored_alternatives)}
    if not include_research_audits:
        result={'pieces':pieces,'auxiliary':auxiliary,'unassigned':{'count':unassigned_count,'length_mm':unassigned_length_mm},'source_discovery_audit':{'atomic_faces':discovery['atomic_faces']},'geometry_audit':{'frame_path_ids':sorted(frame_ids)},'small_candidate_filter':small_filter}
        report_progress(on_progress, 'pieces', piece_work_total, piece_work_total)
        return result
    result={'pieces':pieces,'auxiliary':auxiliary,'issues':[{'code':'geometry_only_not_semantic_piece_truth'},{'code':'cross_piece_interior_intervals','count':len(multi)}],'unassigned':{'count':unassigned_count,'length_mm':unassigned_length_mm,'fragments':unassigned,'zero_length_segments':zero},'ownership_audit':ownership_audit,'source_discovery_audit':discovery,'boundary_audit':{'definitions':{'shared_segments':'原segment参数区间同时在多个owner的完整边界上；可同源双引用','multi_owner_interiors':'原segment参数区间同时选入>=2 owner且至少一方并非完整边界；不是裁片数/准确率'},'shared_interval_count':len(shared),'multi_owner_interval_count':len(multi),'multi_owner_distinct_source_segment_count':len({(f['path_id'],f['segment_index']) for f in multi}),'multi_owner_total_interval_length_mm':sum(f['length_mm'] for f in multi),'shared_segments':shared,'multi_owner_interiors':multi,'near_containment_relations':near_relations},'geometry_audit':{'original_closed_path_count':len([l for l in loops if l['kind']=='original_closed_path']),'polygonized_face_count':discovery['atomic_face_count'],'cut_count':discovery['cut_count'],'dangle_count':discovery['dangle_count'],'invalid_ring_count':discovery['invalid_ring_count'],'source_total_line_length_mm':source_total,'frame_path_ids':sorted(frame_ids),'loop_registry':[{'id':l['id'],'kind':l['kind'],'source_path_ids':l['source_ids'],'outer':polygon_coords(l['poly']),'area_mm2':l['poly'].area} for l in loops],'source_points_moved':False,'gap_filling':False,'coordinate_snapping':False},'small_candidate_filter':small_filter}
    guarded = apply_quality_guards(result,paths,units,eps,PARAMS)
    report_progress(on_progress, 'pieces', piece_work_total, piece_work_total)
    return guarded
