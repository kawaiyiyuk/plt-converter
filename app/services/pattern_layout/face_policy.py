"""闭面边界/内容覆盖观察：强证书才标观察项，不宣称真假裁片。"""
import copy,json,hashlib,math
from collections import defaultdict
from shapely.geometry import Polygon,LineString,Point,GeometryCollection
from shapely.ops import unary_union,polygonize_full
from shapely.strtree import STRtree
VERSION='atomic-subdivision-v1'
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def polygon_parts(g):return [p for p in getattr(g,'geoms',[g]) if p.geom_type=='Polygon' and not p.is_empty]
def line_parts(g):
    if g.is_empty:return []
    if g.geom_type in ('LineString','LinearRing'):return [g]
    return [p for x in getattr(g,'geoms',[]) for p in line_parts(x)]
def rings(p):return {'outer':[list(v) for v in p.exterior.coords],'holes':[[list(v) for v in h.coords] for h in p.interiors]}
def merged(parts):
    result=[]
    for a,b in sorted(parts):
        if result and a<=result[-1][1]+1e-12:result[-1][1]=max(result[-1][1],b)
        else:result.append([a,b])
    return result
def missing_ranges(target,covered):
    a,b=target;cursor=a;out=[]
    for c,d in merged(covered):
        if d<=cursor:continue
        if c>b:break
        if c>cursor+1e-12:out.append([cursor,min(c,b)])
        cursor=max(cursor,d)
        if cursor>=b-1e-12:break
    if cursor<b-1e-12:out.append([cursor,b])
    return out
class Context:
    def __init__(self,result,paths,eps):
        self.result=result;self.paths={p['id']:p for p in paths};self.lines={p['id']:LineString(p['points']) for p in paths if len(p['points'])>=2};self.eps=eps;self.segments=[];self.segment_meta=[];self.parents={};self.parent_certs={};self.ownership=defaultdict(list)
        for p in paths:
            distance=0.
            for i,(a,b) in enumerate(zip(p['points'],p['points'][1:])):
                l=LineString([a,b])
                if l.length>eps:self.segments.append(l);self.segment_meta.append((p['id'],i,distance))
                distance+=l.length
        self.tree=STRtree(self.segments)
        for p in result['pieces']:
            for f in p['fragments']:self.ownership[(f['path_id'],f['segment_index'])].append((p['id'],f['t0'],f['t1']))
            try:self.parents[p['id']]=Polygon(p['outer'],p.get('holes',[]))
            except Exception:pass
        self.parent_numeric_bands={pid:poly.buffer(eps) for pid,poly in self.parents.items() if poly.is_valid}
        frame=set(result.get('geometry_audit',{}).get('frame_path_ids',[]));network=unary_union([l for id,l in self.lines.items() if id not in frame]);faces,_,_,_=polygonize_full(network);self.raw_faces=list(faces.geoms)
    def refs(self,geometry):
        result=[]
        for index in self.tree.query(geometry.envelope):
            index=int(index);l=self.segments[index];pid,si,distance=self.segment_meta[index];p=self.paths[pid];a,b=p['points'][si:si+2]
            for part in line_parts(l.intersection(geometry)):
                if part.length<=self.eps:continue
                ts=[l.project(Point(v))/l.length for v in part.coords];t0=max(0.,min(ts));t1=min(1.,max(ts))
                if t1<=t0:continue
                result.append({'path_id':pid,'segment_index':si,'t0':t0,'t1':t1,'a':[a[k]+(b[k]-a[k])*t0 for k in (0,1)],'b':[a[k]+(b[k]-a[k])*t1 for k in (0,1)],'style':copy.deepcopy(p['segment_styles'][si] if si<len(p['segment_styles']) else None),'distance_from_path_start_mm':distance+t0*l.length})
        return result
    def parent_certificate(self,parent):
        id=parent['id']
        if id in self.parent_certs:return self.parent_certs[id]
        p=self.parents[id];identity=[]
        for pid,l in self.lines.items():
            if l.is_ring and l.is_valid:
                shape=Polygon(l)
                if shape.is_valid and shape.equals(p):identity.append(pid)
        # 只接受明确整原单环母片；复杂多径证书本轮保守不推广。
        refs=[{'path_id':pid,'segment_index':i,'t0':0.,'t1':1.,'a':a,'b':b,'style':copy.deepcopy(self.paths[pid]['segment_styles'][i])} for pid in identity for i,(a,b) in enumerate(zip(self.paths[pid]['points'],self.paths[pid]['points'][1:])) if math.dist(a,b)>self.eps]
        certificate={'outer':rings(p)['outer'],'holes':rings(p)['holes'],'path_ids':identity,'fragments':refs,'raw_polygonized_shells':[rings(Polygon(self.lines[pid])) for pid in identity],'kind':'exact_original_whole_closed_path_identity' if identity else 'unproved_multi_path_parent','independent_of_current_fragment_assignment':True};self.parent_certs[id]=certificate;return certificate

def evaluate(context,face,original=None,face_id=None):
    eps=context.eps;reasons=[];evidence={};fr=rings(face);parents=[p for p in context.result['pieces'] if p['id'] in context.parents and context.parents[p['id']].is_valid and (context.parents[p['id']].covers(face) or context.parent_numeric_bands[p['id']].covers(face)) and not context.parents[p['id']].equals(face)];row={'face_id':face_id,'item_id':original.get('id') if original else None,'face_rings':fr,'decision':'deferred','blocking_reasons':reasons,'evidence':evidence}
    if not face.is_valid or not face.exterior.is_ring:reasons.append('invalid_or_non_simple_face')
    if face.interiors:reasons.append('face_holes_not_eligible_for_safe_attachment')
    evidence['geometrically_containing_mother_piece_ids']=[p['id'] for p in parents]
    boundary=context.refs(face.boundary.buffer(eps));content=context.refs(face);evidence.update(source_boundary_fragments=boundary,content_fragments_before=content,content_origin='recomputed_from_unchanged_source_not_incomplete_support_refs')
    coverage=unary_union([LineString([f['a'],f['b']]).buffer(eps) for f in boundary]);gap=face.boundary.difference(coverage).length;evidence['boundary_coverage_missing_length_mm']=gap
    if gap>eps:reasons.append('incomplete_source_boundary_coverage')
    if len(parents)!=1:reasons.append('mother_not_unique');return row
    parent=parents[0];pid=parent['id'];mp=context.parents[pid];evidence['parent_containment_certificate']={'exact_covers':mp.covers(face),'numeric_epsilon_mm':eps,'numeric_band_covers_entire_face':context.parent_numeric_bands[pid].covers(face),'face_outside_numeric_band_empty':face.difference(context.parent_numeric_bands[pid]).is_empty,'outside_original_parent_area_mm2':face.difference(mp).area,'method':'entire_raw_face_within_existing_numeric_epsilon_only','buffer_used_to_create_or_replace_source_or_shell':False};cert=context.parent_certificate(parent);evidence.update(owner_piece_id=pid,owner_loop_id=parent.get('loop_id'),parent_raw_closed_chain=cert)
    if not cert['path_ids']:reasons.append('parent_independent_original_closed_path_unproved')
    if parent.get('issues'):reasons.append('parent_has_unresolved_risks')
    if any(pid in x.get('piece_ids',[]) for x in context.result.get('boundary_audit',{}).get('multi_owner_interiors',[])):reasons.append('parent_has_unresolved_multi_owner')
    for other in context.result['pieces']:
        if other['id']==pid or other['id'] not in context.parents:continue
        op=context.parents[other['id']]
        if op.intersection(face).area>0 or op.boundary.intersection(face.boundary).length>eps:reasons.append('cross_piece_or_shared_boundary_conflict');break
    owner_intervals=defaultdict(list)
    for f in parent['fragments']:owner_intervals[(f['path_id'],f['segment_index'])].append((f['t0'],f['t1']))
    missing=[];other_owners=[]
    for f in boundary+content:
        for other_id,a,b in context.ownership[(f['path_id'],f['segment_index'])]:
            if other_id!=pid and min(b,f['t1'])-max(a,f['t0'])>1e-12:other_owners.append({'piece_id':other_id,'path_id':f['path_id'],'segment_index':f['segment_index'],'t0':max(a,f['t0']),'t1':min(b,f['t1'])})
        lacks=missing_ranges((f['t0'],f['t1']),owner_intervals[(f['path_id'],f['segment_index'])])
        if lacks:missing.append({'path_id':f['path_id'],'segment_index':f['segment_index'],'requested_t0':f['t0'],'requested_t1':f['t1'],'missing_intervals':lacks})
    evidence['owner_fragments_covering_content']=[copy.deepcopy(f) for f in parent['fragments'] if (f['path_id'],f['segment_index']) in {(q['path_id'],q['segment_index']) for q in boundary+content}];evidence['content_coverage_missing_intervals']=missing
    if missing:reasons.append('face_content_not_completely_selected_by_mother')
    evidence['multi_owner_source_interval_conflicts']=other_owners
    if other_owners:reasons.append('face_source_interval_has_other_owner')
    involved={f['path_id'] for f in boundary+content};mother_paths=set(cert['path_ids']);independent=[];complete_chain=[];crosscuts=[];source_proofs=[]
    for source_id in sorted(involved-mother_paths):
        l=context.lines[source_id]
        if not l.is_simple:reasons.append('self_intersecting_or_backtracking_source_path');continue
        if l.is_ring and l.is_valid:independent.append(source_id)
        if l.difference(face.boundary.buffer(eps)).length<=eps:complete_chain.append(source_id)
        if l.intersection(face.boundary.buffer(eps)).length<=eps:continue
        if l.difference(mp.boundary.buffer(eps)).length<=eps:source_proofs.append({'path_id':source_id,'role':'coincident_parent_boundary_source'});continue
        end0=Point(l.coords[0]);end1=Point(l.coords[-1]);straight=LineString([l.coords[0],l.coords[-1]])
        is_chord=not l.is_closed and straight.length>eps and l.difference(straight.buffer(eps)).length<=eps and end0.distance(mp.boundary)<=eps and end1.distance(mp.boundary)<=eps and l.difference(mp).length<=eps
        if is_chord:crosscuts.append(source_id);source_proofs.append({'path_id':source_id,'role':'simple_straight_parent_crosscut','original_points':context.paths[source_id]['points'],'endpoints':[list(l.coords[0]),list(l.coords[-1])]})
        else:reasons.append('non_crosscut_source_boundary_requires_independent_review')
    evidence.update(independent_original_closed_path_ids=independent,crosscut_source_path_ids=crosscuts,source_continuity_proofs=source_proofs)
    if independent:reasons.append('independent_original_closed_path')
    if complete_chain:
        faces,_,_,_=polygonize_full(unary_union([context.lines[id] for id in complete_chain]));chains=[p for p in faces.geoms if p.area>0];evidence['independent_closed_chain']={'path_ids':complete_chain,'polygonized_shells':[rings(p) for p in chains]}
        if chains:reasons.append('independent_original_closed_chain')
    if not crosscuts:reasons.append('no_proven_crosscut_subdivision')
    row['blocking_reasons']=sorted(set(reasons));row['evidence']['independent_source_declaration_found']=bool(independent or evidence.get('independent_closed_chain',{}).get('polygonized_shells'))
    if not row['blocking_reasons']:row['decision']='boundary_and_content_covered_observation'
    return row

def audit_atomic_faces(result,paths,*,numeric_epsilon_mm):
    context=Context(result,paths,numeric_epsilon_mm);decisions=[];attached=0
    for item in result['auxiliary']:
        if item['status']!='source_backed_closed_face_needs_review':continue
        original=copy.deepcopy(item);matches=[(i,p) for i,p in enumerate(context.raw_faces) if Polygon(item['outer']).equals(Polygon(p.exterior)) and (not item.get('holes') or Polygon(item['outer'],item['holes']).equals(p))]
        if len(matches)!=1:
            row={'face_id':None,'item_id':item['id'],'face_rings':{'outer':copy.deepcopy(item['outer']),'holes':copy.deepcopy(item.get('holes',[]))},'decision':'deferred','blocking_reasons':['not_exact_current_atomic_face'],'evidence':{}}
        else:
            i,face=matches[0];row=evaluate(context,face,original,face_id=f'atomic-{i:04d}')
        row['original_record_sha256']=digest(original);row['original_record']=original;decisions.append(row)
        if row['decision']=='boundary_and_content_covered_observation':
            item['status']='attached_closed_face';item['evidence_attachment']={'proof_version':VERSION,**copy.deepcopy(row['evidence']),'source_face_rings':copy.deepcopy(row['face_rings']),'original_record':original,'original_record_sha256':row['original_record_sha256'],'risks':['independent_piece_identity_not_confirmed'],'content_kept_in_owner':True};attached+=1
    return {'version':VERSION,'decisions':decisions,'raw_atomic_face_count':len(context.raw_faces),'attached_count':attached,'deferred_count':sum(d['decision']=='deferred' for d in decisions),'main_piece_content_unchanged':True,'classification_is_not_semantic_piece_truth':True}
