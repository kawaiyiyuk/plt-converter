"""同源原segment区间的只读审计；几何证据永远不等于语义独占归属。"""
import copy
import math
from functools import lru_cache
from collections import defaultdict
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union


def audit_source_overlap(pieces, paths, *, numeric_epsilon_mm, interval_epsilon=1e-12):
    """只拆分审计区间，不修改输入；传入的epsilon仅用于既有数值误差。"""
    if not math.isfinite(numeric_epsilon_mm) or numeric_epsilon_mm < 0 or not math.isfinite(interval_epsilon) or interval_epsilon < 0:
        raise ValueError('数值误差必须是有限非负数')
    audit = {'version':'v4a-source-overlap-audit-v1', 'automatic_dedup_implemented':False,
             'applied':[], 'deferred':[], 'protected_shared':[], 'suggestions':[],
             'tolerances':{'numeric_epsilon_mm':numeric_epsilon_mm, 'interval_epsilon':interval_epsilon,
                           'meaning':'existing_numeric_error_only_not_physical_allowance'},
             'stats':{}, 'limitations':['几何不能排除真实重叠或内部标记双用；自动去重未实现']}
    raw = {p['id']:p for p in paths}
    owners = {p['id']:p for p in pieces}
    polys = {}
    for p in pieces:
        try:
            poly = Polygon(p.get('outer', []))
            if poly.is_valid and not poly.is_empty and poly.area > 0: polys[p['id']] = poly
        except (ValueError, TypeError):
            pass
    refs = defaultdict(list)
    path_refs = defaultdict(list)
    path_owner_ids = defaultdict(set)
    invalid = []
    for piece in pieces:
        for f in piece.get('fragments', []):
            try:
                lo, hi = float(f['t0']), float(f['t1'])
                if not isinstance(f['segment_index'],int) or isinstance(f['segment_index'],bool) or f['segment_index'] < 0:
                    raise ValueError('invalid source segment index')
                if not all(math.isfinite(x) for x in (lo,hi)) or lo < 0 or hi > 1 or hi < lo:
                    raise ValueError('invalid interval')
                row = {'piece_id':piece['id'], 'fragment':f}
                refs[(f['path_id'], f['segment_index'])].append(row)
                path_refs[(piece['id'], f['path_id'])].append(f)
                path_owner_ids[f['path_id']].add(piece['id'])
            except (KeyError, TypeError, ValueError):
                invalid.append({'piece_id':piece['id'], 'fragment':copy.deepcopy(f), 'reason':'invalid_source_interval'})

    def point(a,b,t):
        return [a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t]

    def covered(intervals):
        end = 0.
        for lo,hi in sorted(intervals):
            if lo > end + interval_epsilon: return False
            end = max(end,hi)
        return end >= 1.-interval_epsilon

    def fragment_valid(f, source):
        try:
            a,b = source['points'][f['segment_index']:f['segment_index']+2]
            return all(math.dist(f[k],point(a,b,f[t])) <= numeric_epsilon_mm
                       for k,t in [('a','t0'),('b','t1')])
        except (KeyError,TypeError,ValueError):
            return False

    def boundary_line(poly,line):
        # 不buffer、不扩大裁片；无法精确覆盖即保守defer，避免累计容差变成物理门槛。
        return poly is not None and line.difference(poly.boundary).is_empty

    def interval_boundary_evidence(owner_id, source, segment_index, lo, hi, line, owner_refs):
        """先证明完整原segment；合法参数子区间继承边界关系，不依赖插值舍入。"""
        poly=polys.get(owner_id)
        declared=all(f.get('on_boundary',False) for f in owner_refs)
        valid_domain=0<=lo<hi<=1
        proof={'method':'unproven','raw_segment':{'path_id':source['id'] if source else None,
               'segment_index':segment_index,'a':None,'b':None},
               'parameter_interval':{'t0':lo,'t1':hi,'within_original_segment':valid_domain},
               'full_original_segment_exact_boundary':False,'interpolated_interval_exact_boundary':None,
               'original_references_match_source':False,'verified_boundary':False,
               'declared_on_boundary':declared,'verified_role':'unproven','failure_reason':None}
        if poly is None:proof['failure_reason']='owner_polygon_not_proven';return proof
        try:
            aa,bb=source['points'][segment_index:segment_index+2]
            proof['raw_segment'].update(a=copy.deepcopy(aa),b=copy.deepcopy(bb))
            if math.dist(aa,bb)==0:proof['failure_reason']='zero_length_original_segment';return proof
        except (KeyError,TypeError,ValueError):
            proof['failure_reason']='original_source_segment_unavailable';return proof
        proof['original_references_match_source']=all(fragment_valid(f,source) for f in owner_refs)
        proof['full_original_segment_exact_boundary']=boundary_line(poly,LineString([aa,bb]))
        if not valid_domain:proof['failure_reason']='invalid_parameter_domain';return proof
        if not proof['original_references_match_source']:
            proof['failure_reason']='source_fragment_endpoint_mismatch';return proof
        if proof['full_original_segment_exact_boundary']:
            proof['method']='full_original_segment_exact_boundary_implies_interval'
            proof['verified_boundary']=True;proof['verified_role']='boundary'
            return proof
        if line is not None:
            proof['method']='exact_interpolated_interval_fallback'
            proof['interpolated_interval_exact_boundary']=boundary_line(poly,line)
            proof['verified_boundary']=proof['interpolated_interval_exact_boundary']
            proof['verified_role']='boundary' if proof['verified_boundary'] else ('interior' if poly.contains(line) else 'unproven')
        if not proof['verified_boundary']:
            proof['failure_reason']='original_segment_not_wholly_boundary_and_interval_not_exact_boundary'
        return proof

    def complete_path(owner_id, source, require_boundary=False):
        points=source.get('points',[])
        if len(points)<2: return False
        ff=path_refs[(owner_id,source['id'])]
        if any(not fragment_valid(f,source) for f in ff): return False
        if not any(math.dist(a,b)!=0 for a,b in zip(points,points[1:])): return False
        for si,(a,b) in enumerate(zip(points,points[1:])):
            if math.dist(a,b)==0: continue
            subset=[f for f in ff if f['segment_index']==si]
            if not covered([(f['t0'],f['t1']) for f in subset]): return False
            if any(not fragment_valid(f,source) for f in subset): return False
            if require_boundary and (any(not f.get('on_boundary',False) for f in subset) or
                                    not boundary_line(polys.get(owner_id),LineString([a,b]))): return False
        return True

    @lru_cache(maxsize=None)
    def independent_shell(owner_id, target_path):
        poly=polys.get(owner_id)
        if poly is None: return None
        supports=[];ids=[]
        for source in paths:
            if len(source.get('points',[]))<2: continue
            line=LineString(source['points'])
            if line.length>0 and boundary_line(poly,line):
                supports.append(line);ids.append(source['id'])
        if not supports: return None
        missing=poly.boundary.difference(unary_union(supports))
        if not missing.is_empty: return None
        return {'source_path_ids':ids,'closed_outer':list(poly.exterior.coords),
                'boundary_missing_length_mm':missing.length,'coverage':'exact_original_source_network_union',
                'target_path_included':target_path in ids,'independent_of_candidate_fragment_assignment':True}

    def explicit_internal(piece, path_id, ff):
        if path_id in piece.get('source_closed_path_ids',[]): return True
        for name in ('source_internal_path_ids','internal_path_ids','notch_path_ids','source_notch_path_ids'):
            if path_id in piece.get(name,[]): return True
        for f in ff:
            assignment=str(f.get('assignment','')).lower()
            if assignment not in ('polygon_inside_or_boundary','') or any(f.get(k) for k in ('notch','is_notch','internal','declared_internal')):
                return True
        # 不把未理解的局部来源声明当成没有声明。
        def exact_reference(value):
            if isinstance(value,dict):
                return any((key in ('path_id','source_path_id') and child==path_id) or
                           (key in ('path_ids','source_path_ids') and isinstance(child,list) and path_id in child) or
                           (isinstance(child,(dict,list)) and exact_reference(child)) for key,child in value.items())
            if isinstance(value,list): return any(exact_reference(child) for child in value)
            return False
        return exact_reference(piece.get('source_evidence',[]))

    @lru_cache(maxsize=None)
    def has_branch(path_id, boundary_owner):
        source=raw[path_id];line=LineString(source['points']);poly=polys.get(boundary_owner)
        for other in paths:
            if other['id']==path_id or len(other.get('points',[]))<2: continue
            other_line=LineString(other['points'])
            # 完整沿B原边界的PU/PD分段是闭壳支撑，不是内部标记分支。
            if boundary_line(poly,other_line): continue
            hit=line.intersection(other_line)
            if hit.is_empty: continue
            return True
        return False

    for (path_id,si),rows in refs.items():
        cuts=sorted({f['fragment'][t] for f in rows for t in ('t0','t1')})
        for lo,hi in zip(cuts,cuts[1:]):
            if hi-lo <= interval_epsilon: continue
            active=[r for r in rows if r['fragment']['t0'] <= lo and r['fragment']['t1'] >= hi]
            owner_ids=sorted({r['piece_id'] for r in active},key=str)
            if len(owner_ids)<2: continue
            source=raw.get(path_id);reasons=[];a=b=None
            try:
                aa,bb=source['points'][si:si+2]
                a,b=point(aa,bb,lo),point(aa,bb,hi)
                if math.dist(aa,bb)==0: reasons.append('zero_length_source_segment')
                if any(not fragment_valid(r['fragment'],source) for r in active): reasons.append('source_endpoint_evidence_mismatch')
            except (KeyError,TypeError,ValueError): reasons.append('incomplete_original_source_path')
            interval={'path_id':path_id,'segment_index':si,'t0':lo,'t1':hi,'a':a,'b':b}
            snapshots=[dict(copy.deepcopy(r['fragment']),piece_id=r['piece_id']) for r in active]
            item={'source':interval,'source_interval':copy.deepcopy(interval),
                  'owners':[{'piece_id':oid,'roles':sorted({'boundary' if r['fragment'].get('on_boundary') else 'interior' for r in active if r['piece_id']==oid}),
                             'original_references':[copy.deepcopy(r['fragment']) for r in active if r['piece_id']==oid]} for oid in owner_ids],
                  'before_owner_piece_ids':owner_ids,'after_owner_piece_ids':owner_ids[:],
                  'before_fragments':snapshots,'after_fragments':copy.deepcopy(snapshots),'reasons':reasons}
            line=LineString([a,b]) if a is not None and a!=b else None
            for owner in item['owners']:
                owner['boundary_evidence']=interval_boundary_evidence(owner['piece_id'],source,si,lo,hi,line,
                    [r['fragment'] for r in active if r['piece_id']==owner['piece_id']])
            boundary_owners=[o['piece_id'] for o in item['owners'] if o['boundary_evidence']['verified_boundary']]
            if any(o['boundary_evidence']['verified_boundary']!=o['boundary_evidence']['declared_on_boundary'] for o in item['owners']):
                reasons.append('boundary_role_declaration_mismatch')
            if len(boundary_owners)==len(owner_ids) and not reasons:
                item['reasons']=['true_shared_source_interval_preserved'];audit['protected_shared'].append(item);continue
            if len(owner_ids)!=2: reasons.append('multiple_owner_conflict')
            if len(boundary_owners)!=1: reasons.append('no_unique_verified_boundary_owner')
            if len(owner_ids)==2 and len(boundary_owners)==1 and not reasons:
                target=boundary_owners[0];review=next(oid for oid in owner_ids if oid!=target)
                review_refs=path_refs[(review,path_id)]
                if explicit_internal(owners[review],path_id,review_refs): reasons.append('independent_internal_or_source_declaration')
                if any(r['fragment'].get('on_boundary') for r in active if r['piece_id']==review): reasons.append('declared_boundary_geometry_mismatch')
                if not complete_path(target,source,True): reasons.append('boundary_owner_lacks_complete_original_path')
                if complete_path(review,source): reasons.append('review_owner_references_complete_source_path')
                # 原path的邻接重复点仅代表零长segment，不跨点补线或改变原segment索引。
                nonzero=[i for i,(x,y) in enumerate(zip(source['points'],source['points'][1:])) if math.dist(x,y)!=0]
                source_line=LineString(source['points'])
                if not source_line.is_simple: reasons.append('original_source_self_intersection')
                if len(path_owner_ids[path_id])>2:
                    reasons.append('multiple_owner_conflict_across_original_path')
                if nonzero and any((f['t0']<=interval_epsilon and f['segment_index']>nonzero[0]) or
                                   (f['t1']>=1.-interval_epsilon and f['segment_index']<nonzero[-1])
                                   for f in review_refs if f['segment_index'] in nonzero):
                    reasons.append('original_source_vertex_clipping_requires_review')
                if has_branch(path_id,target): reasons.append('original_source_point_or_branch_requires_review')
                shell=independent_shell(target,path_id)
                if not shell: reasons.append('independent_complete_source_shell_not_proven')
                if polys.get(review) is None: reasons.append('review_owner_polygon_not_proven')
                elif not polys[review].contains(line): reasons.append('review_interval_not_strict_interior')
                if not reasons:
                    overlap=polys[target].intersection(polys[review])
                    item.update(status='program_suggestion_unapplied',suggested_owner_id=target,review_owner_id=review,
                                suggested_after={'status':'not_applied','owner_piece_ids':[target]},
                                evidence={'complete_original_path':copy.deepcopy(source),'boundary_continuity':shell,
                                          'actual_piece_intersection_area_mm2':overlap.area,
                                          'actual_piece_intersection_geometry':overlap.__geo_interface__,
                                          'target_complete_source_boundary':True,'review_only_partial_source':True,
                                          'original_path_continuity':{'nonzero_segment_indices':nonzero,
                                              'zero_length_segment_indices':[i for i,(x,y) in enumerate(zip(source['points'],source['points'][1:])) if math.dist(x,y)==0],
                                              'self_intersection':False,'branch_detected':False,
                                              'source_indices_and_vertices_unchanged':True,
                                              'raw_segment_boundary_coverage':[{'path_id':path_id,'segment_index':i,
                                                  't0':0.,'t1':1.,'a':copy.deepcopy(source['points'][i]),
                                                  'b':copy.deepcopy(source['points'][i+1]),'exact_boundary_covered':True}
                                                  for i in nonzero]},
                                          'review_interval_geometry_contains':True,
                                          'target_source_closed_path_ids':copy.deepcopy(owners[target].get('source_closed_path_ids',[])),
                                          'review_source_closed_path_ids':copy.deepcopy(owners[review].get('source_closed_path_ids',[]))},
                                risks=['真实正面积重叠不能排除','内部标记有意双用不能排除','几何不证明语义独占，禁止自动删除'])
                    reasons.append('ownership_ambiguous_unapplied')
                    audit['suggestions'].append(copy.deepcopy(item))
            audit['deferred'].append(item)
    audit['invalid_references']=invalid
    audit['stats']={'overlap_interval_count':len(audit['deferred'])+len(audit['protected_shared']),
                    'deferred_count':len(audit['deferred']),'protected_shared_count':len(audit['protected_shared']),
                    'suggestion_count':len(audit['suggestions']),'invalid_reference_count':len(invalid)}
    return audit
