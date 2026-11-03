"""Non-semantic, single-rectangle alternatives. Originals are never modified."""
import math
from shapely.geometry import LineString, Polygon
from shapely import make_valid
from shapely.ops import unary_union


PARAMETERS={'numeric_epsilon_plotter_units':1e-7,'one_rectangle_per_alternative':True}


def is_rectangle(polygon,eps):
    points=[list(p) for p in polygon.exterior.coords[:-1]]
    changed=True
    while changed and len(points)>4:
        changed=False
        for i,b in enumerate(points):
            a,c=points[i-1],points[(i+1)%len(points)]
            u,v=[b[k]-a[k] for k in (0,1)],[c[k]-b[k] for k in (0,1)]
            if abs(u[0]*v[1]-u[1]*v[0])<=eps*(math.hypot(*u)+math.hypot(*v)):
                points.pop(i);changed=True;break
    if len(points)!=4:return False
    for i,b in enumerate(points):
        a,c=points[i-1],points[(i+1)%4]
        u,v=[b[k]-a[k] for k in (0,1)],[c[k]-b[k] for k in (0,1)]
        if min(math.hypot(*u),math.hypot(*v))<=eps:return False
        if abs(u[0]*v[0]+u[1]*v[1])>eps*(math.hypot(*u)+math.hypot(*v)):return False
    return True


def add_rectangle_alternatives(result):
    eps=PARAMETERS['numeric_epsilon_plotter_units']/result['source']['units_per_mm']
    paths={p['id']:p for p in result['paths']};rectangles=[]
    for face in result['source_discovery_audit']['atomic_faces']:
        ids=face.get('complete_boundary_source_path_ids',[])
        if not face.get('independent_complete_paths') or not ids:continue
        polygon=Polygon(face['outer'])
        if not polygon.is_valid or polygon.area<=eps*eps:continue
        if not is_rectangle(polygon,eps):continue
        source_lines=[LineString(paths[id]['points']) for id in ids]
        if any(line.difference(polygon.boundary).length>eps for line in source_lines):continue
        if polygon.boundary.difference(unary_union(source_lines)).length>eps:continue
        if any(polygon.equals(r['polygon']) and set(ids)==set(r['path_ids']) for r in rectangles):continue
        rectangles.append({'polygon':polygon,'path_ids':sorted(ids),'face_index':face['face_index']})
    originals=list(result['pieces']);variants=[];changes=[]
    for original in originals:
        if not original.get('outer') or not original.get('fragments'):continue
        region=make_valid(Polygon(original['outer']))
        for rectangle in rectangles:
            polygon=rectangle['polygon'];ids=set(rectangle['path_ids'])
            if ids.intersection(original.get('source_closed_path_ids',[])):continue
            if region.equals(polygon) or not region.covers(polygon):continue
            chosen=[f for f in original['fragments'] if f['path_id'] in ids]
            # Only offer subtraction where the candidate has the complete
            # independent rectangle. Never infer missing edges or remove the
            # candidate's own outline. All non-rectangle fragments stay identical.
            complete=True
            for id in ids:
                path=paths[id]
                for index,(a,b) in enumerate(zip(path['points'],path['points'][1:])):
                    if math.dist(a,b)<=eps:continue
                    intervals=sorted((f['t0'],f['t1']) for f in chosen if f['path_id']==id and f['segment_index']==index)
                    cursor=0.
                    for lo,hi in intervals:
                        if lo>cursor+1e-12:break
                        cursor=max(cursor,hi)
                    if cursor<1-1e-12:complete=False;break
                if not complete:break
            if not complete:continue
            variant=dict(original)
            variant.update(id=original['id']+'-omitrect-'+str(rectangle['face_index']),
                           parent_candidate_id=original['id'],status='rectangle_line_alternative_needs_review',
                           variant_label_cn='不带一组独立矩形框线（可能移除本片真实标记）',
                           fragments=[f for f in original['fragments'] if f['path_id'] not in ids],
                           excluded_rectangle={'source_path_ids':rectangle['path_ids'],'outer':face_outer(polygon)},
                           issues=list(original.get('issues',[]))+[{'code':'may_remove_real_piece_internal_rectangle'}, {'code':'rectangle_nearby_annotations_preserved_not_semantically_classified'}],
                           semantic_piece_verified=False)
            variants.append(variant);changes.append({'candidate_id':variant['id'],'original_id':original['id'],'removed_path_ids':rectangle['path_ids'],'removed_fragment_count':len(chosen)})
    result['pieces'].extend(variants)
    result['rectangle_alternative_audit']={'parameters':PARAMETERS,'originals_modified':False,'variants':changes,'semantic_classification':False}
    return result


def face_outer(polygon):return [list(p) for p in polygon.exterior.coords]


def recognize_with_variants(raw,name='source.plt'):
    from .pattern_layout_api import recognize_source
    return add_rectangle_alternatives(recognize_source(raw,name))
