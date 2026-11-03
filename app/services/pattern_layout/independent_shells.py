"""保留原子面，用原线独立支持提案；有界/借边本身不等于真实裁片。"""
import math
from shapely.geometry import LineString,Polygon,Point,GeometryCollection
from shapely.ops import unary_union,polygonize_full

def polygon_parts(g):return [p for p in getattr(g,'geoms',[g]) if p.geom_type=='Polygon' and not p.is_empty]
def nearby_contains(outer,inner,params):
    if outer.area<=inner.area:return False
    area=inner.difference(outer).area
    return area<=params['near_containment_outside_mm2'] and area/inner.area<=params['near_containment_relative']
def shell_of_network(lines,region,eps,delta,area_epsilon):
    net=unary_union(lines);faces,_,_,invalid=polygonize_full(net);fill=unary_union([Polygon(f.exterior) for f in faces.geoms]);witness=[]
    for p in polygon_parts(fill):
        missing=region.difference(p).area;outside=p.difference(region.buffer(delta)).area
        if missing<=area_epsilon and outside<=area_epsilon:witness.append({'area_mm2':p.area,'missing_area_mm2':missing,'outside_near_band_area_mm2':outside})
    return {'complete':bool(witness),'valid_shells':witness,'invalid_ring_count':len(invalid.geoms),'missing_area_mm2':region.difference(fill).area}
def support_refs(poly,paths,eps):
    refs=[];band=poly.boundary.buffer(eps);minx,miny,maxx,maxy=band.bounds
    for source in paths:
        dist=0.
        for si,(a,b) in enumerate(zip(source['points'],source['points'][1:])):
            line=LineString([a,b]);length=line.length
            if length<=eps:continue
            if max(a[0],b[0])<minx or min(a[0],b[0])>maxx or max(a[1],b[1])<miny or min(a[1],b[1])>maxy:
                dist+=length;continue
            hit=line.intersection(band)
            for g in getattr(hit,'geoms',[hit]):
                if g.geom_type!='LineString' or g.length<=eps:continue
                ts=[line.project(Point(v))/length for v in g.coords];t0=max(0,min(ts));t1=min(1,max(ts))
                refs.append({'path_id':source['id'],'segment_index':si,'t0':t0,'t1':t1,'a':[a[k]+(b[k]-a[k])*t0 for k in (0,1)],'b':[a[k]+(b[k]-a[k])*t1 for k in (0,1)],'style':source['segment_styles'][si] if si<len(source['segment_styles']) else None,'distance_from_path_start_mm':dist+t0*length,'assignment':'source_boundary_observation'})
            dist+=length
    return refs

def discover(paths,original,frame_ids,params,eps):
    lines={p['id']:LineString(p['points']) for p in paths if len(p['points'])>=2};available={id:l for id,l in lines.items() if id not in frame_ids};known=list(original);records=[];alternatives=[];nextid=max([int(l['id'][1:]) for l in original]+[0])+1
    def add(poly,kind,proof,source_ids=None):
        nonlocal nextid
        if poly.is_empty or not poly.is_valid or poly.area<=eps**2:return
        same=next((r for r in records if r['poly'].equals(poly)),None)
        if same:same['proofs'].append(proof);return same
        r={'poly':poly,'source_ids':source_ids or [],'kind':kind,'id':f'L{nextid:04d}','proofs':[proof]};nextid+=1;records.append(r);return r
    for l in original:records.append({**l,'proofs':[{'kind':'original_closed_path_identity','source_path_ids':l['source_ids']} ]})
    # 原path中明确返回同一原顶点的闭合子链也保留，不能因path还带内部笔画而漏掉。
    for source in paths:
        if source['id'] in frame_ids:continue
        seen={}
        for j,pt in enumerate(source['points']):
            key=tuple(pt)
            if key in seen and j-seen[key]>=3:
                i=seen[key];line=LineString(source['points'][i:j+1])
                if line.is_ring and line.is_valid:
                    poly=Polygon(line)
                    add(poly,'source_subpath_closed_cycle',{'kind':'original_vertex_return_cycle','path_id':source['id'],'source_segment_indices':list(range(i,j))})
            seen[key]=j
    original_roots=[r for r in records if not any(nearby_contains(s['poly'],r['poly'],params) for s in records if s is not r)]
    root_anchor_ids={id for r in original_roots for id in r['source_ids']}
    net=unary_union(list(available.values()));faces,cuts,dangles,invalid=polygonize_full(net);atomic=[]
    # EPS不变：缓存每条真实原path的buffer，再union；不buffer noded union来决定源支持。
    path_covers={pid:line.buffer(eps) for pid,line in available.items()};coverage_audit=[]
    # 这项独立链证书要求所有周边由完整的非借用原path支持；没有面积门槛。
    def complete_path_support(poly,exclude=frozenset()):
        ids=[id for id,l in available.items() if id not in exclude and l.length>eps and l.difference(poly.boundary.buffer(eps)).length<=eps]
        cover=unary_union([path_covers[id] for id in ids]);missing=poly.boundary.difference(cover).length
        return missing<=eps,ids,missing
    for index,f in enumerate(faces.geoms):
        poly=Polygon(f.exterior);valid,ids,missing=complete_path_support(poly);entry={'face_index':index,'poly':poly,'independent_complete_paths':valid,'complete_boundary_source_path_ids':ids,'unsupported_by_complete_paths_length_mm':missing};atomic.append(entry)
        if valid:add(poly,'multi_path_polygonized_shell',{'kind':'complete_original_paths_close_boundary','source_path_ids':ids,'atomic_face_index':index})
    # 不先把mixed整体搁置：逐个已知outer来源排除，仅在本地副本重建原闭壳。
    scenarios=[('all_source',frozenset()),('exclude_all_known_outers',frozenset(root_anchor_ids))]+[(f'exclude_{r["id"]}',frozenset(r['source_ids'])) for r in original_roots if r['source_ids']]
    scenario_audit=[]
    for name,excluded in scenarios:
        work={id:l for id,l in available.items() if id not in excluded};n=unary_union(list(work.values()));pg,_,_,bad=polygonize_full(n);filled=unary_union([Polygon(f.exterior) for f in pg.geoms]);count=0
        # 每scenario仅建一次覆盖几何；原线polygonize仍未经buffer，不能造闭合。
        source_cover=unary_union([path_covers[id] for id in work]);legacy_cover=n.buffer(eps)
        for poly in polygon_parts(filled):
            support_gap=poly.boundary.difference(source_cover).length;legacy_gap=poly.boundary.difference(legacy_cover).length
            support_record={'scenario':name,'epsilon_mm':eps,'method':'per_original_path_buffer_then_union','source_path_count':len(work),'boundary_length_mm':poly.length,'supported_missing_length_mm':support_gap,'legacy_union_then_buffer_missing_length_mm':legacy_gap,'raw_polygonization_closed':True,'source_points_modified':False,'buffer_used_to_create_shell':False};coverage_audit.append(support_record)
            if support_gap>eps:continue
            valid,ids,missing=complete_path_support(poly,excluded)
            # 未排除任何锚时只放行完整原path链支持的壳；其他只作混合观察。
            if not excluded and not valid:continue
            # 剩余壳若仍借多个现有独立outer作外围，不因换场景就整体放行。
            contributing=[r for r in original_roots if not (set(r['source_ids'])&set(excluded)) and r['poly'].boundary.intersection(poly.boundary).length>eps and not poly.equals(r['poly'])]
            if contributing:continue
            add(poly,'multi_path_polygonized_shell',{'kind':'independent_network_after_anchor_exclusion' if excluded else 'complete_original_paths_close_boundary','excluded_source_path_ids':sorted(excluded),'source_network_path_ids':sorted(work),'boundary_source_supported':True,'source_support_audit':support_record,'no_new_connecting_edge':True});count+=1
        scenario_audit.append({'scenario':name,'excluded_source_path_ids':sorted(excluded),'proposed_shells':count,'invalid_rings':len(bad.geoms)})
    # 若一个外围实际上是多个已独立闭壳的组合，保备用而非吞并；内环完全内含不算这种证据。
    independent=list(records);active=[]
    for r in independent:
        if r['kind'] in ('original_closed_path','source_subpath_closed_cycle'):active.append(r);continue
        witnesses=[s for s in independent if s is not r and r['poly'].area>s['poly'].area and r['poly'].covers(s['poly']) and s['poly'].boundary.intersection(r['poly'].boundary).length>eps]
        separated=any(not a['poly'].covers(b['poly']) and not b['poly'].covers(a['poly']) for i,a in enumerate(witnesses) for b in witnesses[:i])
        if separated:alternatives.append({**r,'kind':'mixed_shell_alternative','reason':'source_composite_shell_not_single_body','proof_level':'composite_not_certified_single_body'})
        else:active.append(r)
    # 每个原子闭面都留账；不能区分的借边小片/假口采用同等级待核，不按面积决定真假。
    for f in atomic:
        poly=f['poly']
        if any(r['poly'].covers(poly) for r in active):continue
        if any(a['poly'].equals(poly) for a in alternatives):continue
        alternatives.append({'poly':poly,'source_ids':[],'kind':'source_backed_closed_face_needs_review','id':f'FACE{f["face_index"]:04d}','reason':'source_support_dependency','proof_level':'source_backed_but_independent_ownership_unproved','proofs':[{'kind':'atomic_face_boundary_only','face_index':f['face_index'],'complete_path_boundary':f['independent_complete_paths']}]})
    return active,alternatives,{'source_support_coverage_audit':coverage_audit,'source_support_method':'per_original_path_buffer_then_union','source_support_epsilon_mm':eps,'scenario_audit':scenario_audit,'atomic_faces':[{'face_index':f['face_index'],'outer':[list(p) for p in f['poly'].exterior.coords],'area_mm2':f['poly'].area,'independent_complete_paths':f['independent_complete_paths'],'complete_boundary_source_path_ids':f['complete_boundary_source_path_ids']} for f in atomic],'atomic_face_count':len(atomic),'cut_count':len(cuts.geoms),'dangle_count':len(dangles.geoms),'invalid_ring_count':len(invalid.geoms)}

def whole_path_owners(paths,roots,families,loop_byid,units,eps,params):
    lines={p['id']:LineString(p['points']) for p in paths if len(p['points'])>=2};delta=params['whole_path_near_band_plotter_units']/units;owners={};shared=[];audit=[];family_conflicts=[];closed_family_support={};shared_support={};regions={r['id']:r['poly'] for r in roots};family_paths={rid:{pid for lid in lids for pid in loop_byid[lid]['source_ids']} for rid,lids in families.items()};eligibility={}
    for pid,line in lines.items():
        common=[rid for rid,p in regions.items() if line.length>eps and line.difference(p.boundary.buffer(eps)).length<=eps]
        if len(common)>=2:shared.append(pid);shared_support[pid]=set(common);audit.append({'path_id':pid,'state':'protected_whole_boundary_shared','owner_loop_ids':common});continue
        family_candidates=[rid for rid,ids in family_paths.items() if pid in ids]
        whole_original_closed=bool(line.is_valid and line.is_ring and Polygon(line).is_valid)
        if whole_original_closed and len(family_candidates)==1:
            rid=family_candidates[0];owners[pid]=rid;closed_family_support[pid]=rid;audit.append({'path_id':pid,'state':'unique_original_closed_path_family_owner','owner_loop_id':rid,'family_loop_ids':family_candidates,'whole_original_closed_path':True,'source_closed_subpath_only':False,'inside_existing_whole_path_near_band':line.difference(regions[rid].buffer(delta)).length<=eps,'near_band_plotter_units':params['whole_path_near_band_plotter_units'],'near_band_mm':delta,'rule':'unique original whole closed path family identity; no coordinate/band change'});continue
        if whole_original_closed and len(family_candidates)>1:
            family_conflicts.append(pid);audit.append({'path_id':pid,'state':'original_closed_path_family_conflict','family_loop_ids':sorted(family_candidates),'whole_original_closed_path':True,'automatic_owner_chosen':False});continue
        candidates=[rid for rid,p in regions.items() if line.length>eps and line.difference(p.buffer(delta)).length<=eps and line.intersection(p).length>eps and not any(pid in refs and foreign!=rid for foreign,refs in family_paths.items())];eligibility[pid]=candidates
    def external_tail_subloop_proof(pid,region):
        # 只在原开放path内返回原顶点的真实子链上判证；该子链不能兜底保留片外余尾。
        line=lines[pid]
        if line.is_ring or line.difference(region).length<=eps:return None
        points=list(line.coords);seen={}
        for j,pt in enumerate(points):
            if pt in seen and j-seen[pt]>=3:
                i=seen[pt];sub=LineString(points[i:j+1])
                if sub.is_ring and sub.is_valid:
                    poly=Polygon(sub)
                    if poly.is_valid and region.difference(poly).area<=params['structural_shell_area_epsilon_mm2']:return {'closed_subpath_point_indices':[i,j],'external_whole_path_length_mm':line.difference(region).length,'whole_path_is_closed':False,'whole_restore_blocked':True}
            seen[pt]=j
        return None
    shell_cache={};after_cache={}
    for pid,eligible in eligibility.items():
        line=lines[pid];structural=[];per={};subloop_blocks=[]
        for rid in eligible:
            region=regions[rid];subloop=external_tail_subloop_proof(pid,region)
            if subloop:per[rid]=subloop;subloop_blocks.append(rid);continue
            identity=pid in family_paths[rid] and line.is_ring
            if identity:structural.append(rid);per[rid]={'identity':True};continue
            if line.intersection(region.boundary.buffer(eps)).length<=eps:continue
            base=sorted(id for id in lines if id!=pid and (eligibility.get(id)==[rid] or closed_family_support.get(id)==rid or rid in shared_support.get(id,set())));key=(rid,tuple(base));before=shell_cache.get(key)
            if before is None:before=shell_of_network([lines[id] for id in base],region,eps,delta,params['structural_shell_area_epsilon_mm2']);shell_cache[key]=before
            after_key=(rid,tuple(sorted(base+[pid])))
            if after_key not in after_cache:after_cache[after_key]=shell_of_network([lines[id] for id in base]+[line],region,eps,delta,params['structural_shell_area_epsilon_mm2'])
            after=after_cache[after_key];repair=not before['complete'] and after['complete'];per[rid]={'identity':False,'before':before,'after':after,'repairs_shell':repair,'base_source_path_ids':base}
            if repair:structural.append(rid)
        if len(eligible)==1 and len(structural)==1:owners[pid]=structural[0];audit.append({'path_id':pid,'state':'unique_structural_whole_owner','owner_loop_id':structural[0],'per_candidate':per,'near_band_plotter_units':params['whole_path_near_band_plotter_units'],'near_band_mm':delta})
        elif subloop_blocks:audit.append({'path_id':pid,'state':'open_subpath_only_whole_path_restore_blocked','eligible_loop_ids':eligible,'per_candidate':per})
        elif len(eligible)>1:audit.append({'path_id':pid,'state':'whole_path_owner_unresolved','eligible_loop_ids':eligible,'structural_loop_ids':structural,'per_candidate':per})
    return owners,{'original_closed_family_conflict_path_ids':family_conflicts,'protected_shared_whole_path_ids':shared,'path_audit':audit,'near_band_plotter_units':params['whole_path_near_band_plotter_units'],'near_band_mm':delta,'proof_rule':'unique eligible whole-path near band AND unique source identity or closed-shell repair; no length/ratio/BBox winner'}
