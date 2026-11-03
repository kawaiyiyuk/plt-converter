"""V5 原线闭面关系：扩展几何证书，不把几何归附当裁片语义真值。

接入 audit_face_relations(result, paths, numeric_epsilon_mm=eps)。仅改变已登记
待核 auxiliary 的状态，母片、原path、源区间及坐标不改；原记录保存在证据中。
相比 V4，允许完整原path组成的精确母闭链，以及两端在母边界的简单曲线分割。
线型仅记证据：虚线、细线或面积小均不能自行证明不是独立裁片。
"""
import copy
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union, polygonize_full
from .face_policy import Context, evaluate, rings, digest

VERSION = 'original-chain-curved-crosscut-v5'


class RelationContext(Context):
    """仅扩展母片证书；保留 V4 的内容完整性和多 owner 检查。"""

    def parent_certificate(self, parent):
        cert = super().parent_certificate(parent)
        if cert['path_ids']:
            return cert
        poly = self.parents[parent['id']]
        if not poly.is_valid or poly.interiors:
            return cert
        # 完整源path须精确位于母边界，不能切取片段拼出想要的母壳。
        ids = sorted(pid for pid, line in self.lines.items()
                     if line.length > self.eps and line.is_simple
                     and poly.boundary.covers(line))
        if not ids:
            return cert
        network = unary_union([self.lines[pid] for pid in ids])
        faces, cuts, dangles, invalid = polygonize_full(network)
        exact = (network.equals(poly.boundary) and len(faces.geoms) == 1
                 and faces.geoms[0].equals(poly) and cuts.is_empty
                 and dangles.is_empty and invalid.is_empty)
        if not exact:
            return cert
        refs = []
        for pid in ids:
            source = self.paths[pid]
            for si, (a, b) in enumerate(zip(source['points'], source['points'][1:])):
                if a == b:
                    continue
                refs.append({'path_id': pid, 'segment_index': si, 't0': 0., 't1': 1.,
                             'a': copy.deepcopy(a), 'b': copy.deepcopy(b),
                             'style': copy.deepcopy(source['segment_styles'][si]
                                      if si < len(source['segment_styles']) else None)})
        cert = {'kind': 'exact_complete_original_paths_closed_chain',
                'outer': rings(poly)['outer'], 'holes': [], 'path_ids': ids,
                'fragments': refs, 'raw_polygonized_shells': [rings(faces.geoms[0])],
                'independent_of_current_fragment_assignment': True,
                'complete_source_paths_only': True, 'numeric_buffer_used': False}
        self.parent_certs[parent['id']] = cert
        return cert


def original_return_cycles(source):
    """保护原顶点返回的闭合子链，即使整条path仍有开放尾巴。"""
    seen = {}
    cycles = []
    for end, point in enumerate(source['points']):
        key = tuple(point)
        for start in ([seen[key]] if key in seen else []):
            if end - start < 3:
                continue
            line = LineString(source['points'][start:end + 1])
            if line.is_ring and line.is_simple and Polygon(line).is_valid:
                cycles.append({'path_id': source['id'], 'segment_start': start,
                               'segment_end_exclusive': end,
                               'outer': rings(Polygon(line))['outer']})
        seen[key] = end
    return cycles


def curved_crosscut_proof(context, source_id, mother):
    """要求完整简单原path；只与母边界在两个原端点相交，不接受靠近或补线。"""
    line = context.lines[source_id]
    if line.is_closed or not line.is_simple or line.length <= context.eps:
        return None
    ends = [Point(line.coords[0]), Point(line.coords[-1])]
    if ends[0].equals(ends[1]) or not all(mother.boundary.covers(e) for e in ends):
        return None
    if not mother.covers(line):
        return None
    intersection = line.intersection(mother.boundary)
    if not intersection.equals(unary_union(ends)):
        return None
    return {'path_id': source_id, 'role': 'simple_original_curved_parent_crosscut',
            'original_points': copy.deepcopy(context.paths[source_id]['points']),
            'original_segment_styles': copy.deepcopy(context.paths[source_id]['segment_styles']),
            'exact_endpoint_boundary_contacts_only': True,
            'numeric_buffer_used': False}


def non_mother_boundary_cycle_risk(context, face, mother_ids, involved):
    """非母片原段若足以支持面周界，保留中段闭链的身份歧义。

    不要求整个原path属于该面，也不要求交点已是原顶点。逐原segment作数值
    比较带覆盖，避免GEOS对插值子区间差集的浮点误拒；比较带不生成新闭环。
    这里只增加待核风险，不能把近带覆盖当作精确独立裁片证书。
    """
    original_segments = []
    segment_records = []
    band = face.boundary.buffer(context.eps)
    for pid in sorted(involved - set(mother_ids)):
        source = context.paths[pid]
        for si, (a, b) in enumerate(zip(source['points'], source['points'][1:])):
            line = LineString([a, b])
            if line.length <= context.eps or not line.intersects(band):
                continue
            original_segments.append(line)
            segment_records.append({'path_id': pid, 'segment_index': si,
                                    'a': copy.deepcopy(a), 'b': copy.deepcopy(b)})
    coverage = unary_union([line.buffer(context.eps) for line in original_segments])
    missing = face.boundary.difference(coverage).length
    risk = bool(original_segments) and missing <= context.eps
    return {'potential_independent_non_mother_boundary_cycle': risk,
            'original_source_segments': segment_records,
            'unsupported_boundary_length_mm': missing,
            'numeric_epsilon_mm': context.eps,
            'comparison_band_only_no_geometry_changed': True,
            'does_not_require_whole_source_path_or_original_vertex_intersections': True,
            'is_exact_closed_chain_certificate': False}


def evaluate_face(context, face, original=None, face_id=None):
    """先跑既有保护，再仅以新证书解除对应的两项旧限制。"""
    row = evaluate(context, face, original, face_id)
    evidence = row['evidence']
    owner = evidence.get('owner_piece_id')
    if not owner:
        return row
    cert = evidence['parent_raw_closed_chain']
    mother_ids = set(cert['path_ids'])
    involved = {f['path_id'] for key in ('source_boundary_fragments', 'content_fragments_before')
                for f in evidence.get(key, [])} - mother_ids
    proofs = []
    unresolved = []
    cycles = []
    for pid in sorted(involved):
        line = context.lines[pid]
        # 内部文字、孔、口袋等保留源闭合身份，不依赖原path是否整体闭合。
        for cycle in original_return_cycles(context.paths[pid]):
            polygon = Polygon(cycle['outer'])
            if polygon.intersection(face).area > 0 or polygon.boundary.intersection(face.boundary).length > context.eps:
                cycles.append(cycle)
        if line.intersection(face.boundary.buffer(context.eps)).length <= context.eps:
            continue
        if line.difference(context.parents[owner].boundary.buffer(context.eps)).length <= context.eps:
            continue
        proof = curved_crosscut_proof(context, pid, context.parents[owner])
        if proof:
            proofs.append(proof)
        else:
            unresolved.append(pid)
    reasons = set(row['blocking_reasons'])
    if proofs and not unresolved:
        reasons.discard('non_crosscut_source_boundary_requires_independent_review')
        reasons.discard('no_proven_crosscut_subdivision')
    if cycles:
        reasons.add('independent_original_vertex_return_cycle')
    chain_risk = non_mother_boundary_cycle_risk(context, face, mother_ids, involved)
    evidence['v5_non_mother_boundary_cycle_risk'] = chain_risk
    if chain_risk['potential_independent_non_mother_boundary_cycle']:
        reasons.add('potential_independent_non_mother_source_boundary_cycle')
    evidence['v5_curved_crosscut_proofs'] = proofs
    evidence['v5_unproved_boundary_path_ids'] = unresolved
    evidence['v5_original_vertex_return_cycles'] = cycles
    evidence['v5_style_is_observation_not_semantic_classifier'] = True
    row['blocking_reasons'] = sorted(reasons)
    row['decision'] = 'boundary_and_content_covered_observation' if not reasons else 'deferred'
    return row


def audit_face_relations(result, paths, *, numeric_epsilon_mm, apply=True):
    """返回可追溯实际状态变更；apply=False 只预览，不修改输入。歧义全部保留。"""
    context = RelationContext(result, paths, numeric_epsilon_mm)
    decisions = []
    changes = []
    for item in result['auxiliary']:
        if item['status'] != 'source_backed_closed_face_needs_review':
            continue
        original = copy.deepcopy(item)
        matches = [(i, face) for i, face in enumerate(context.raw_faces)
                   if Polygon(item['outer']).equals(Polygon(face.exterior))
                   and (not item.get('holes') or Polygon(item['outer'], item['holes']).equals(face))]
        if len(matches) == 1:
            index, face = matches[0]
            row = evaluate_face(context, face, original, face_id=f'atomic-{index:04d}')
        else:
            row = {'face_id': None, 'item_id': item['id'], 'decision': 'deferred',
                   'blocking_reasons': ['not_exact_current_atomic_face'], 'evidence': {},
                   'face_rings': {'outer': copy.deepcopy(item['outer']),
                                  'holes': copy.deepcopy(item.get('holes', []))}}
        row['original_record'] = original
        row['original_record_sha256'] = digest(original)
        decisions.append(row)
        if row['decision'] != 'boundary_and_content_covered_observation':
            continue
        attachment = {'proof_version': VERSION, **copy.deepcopy(row['evidence']),
                      'source_face_rings': copy.deepcopy(row['face_rings']),
                      'original_record': original, 'original_record_sha256': digest(original),
                      'content_kept_in_owner': True,
                      'risks': ['independent_piece_identity_not_confirmed']}
        changes.append({'item_id': item['id'], 'before_status': original['status'],
                        'after_status': 'attached_closed_face', 'original_record_sha256': digest(original),
                        'owner_piece_id': row['evidence']['owner_piece_id']})
        if apply:
            item['status'] = 'attached_closed_face'
            item['evidence_attachment'] = attachment
    return {'version': VERSION, 'decisions': decisions, 'changes': changes,
            'attached_count': len(changes) if apply else 0, 'proposed_count': len(changes),
            'deferred_count': sum(r['decision'] == 'deferred' for r in decisions),
            'raw_atomic_face_count': len(context.raw_faces), 'main_piece_content_unchanged': True,
            'classification_is_not_semantic_piece_truth': True, 'apply': apply}
