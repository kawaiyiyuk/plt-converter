"""V5 原始线段证据与闭面关系；不推断无证据的排他归属。"""
from .dedup_policy import audit_source_overlap
from .face_policy import audit_atomic_faces
from .segment_chain_v5 import analyze_source_overlap
from .face_relations_v5 import audit_face_relations

def apply_quality_guards(result,paths,units,eps,params):
    """保留旧审计，再运行新规则；原线、主片和歧义共边保持。"""
    result['dedup_audit']=audit_source_overlap(result['pieces'],paths,numeric_epsilon_mm=eps,interval_epsilon=1e-12)
    result['atomic_face_audit']=audit_atomic_faces(result,paths,numeric_epsilon_mm=eps)
    result['segment_chain_audit']=analyze_source_overlap(result['pieces'],paths,numeric_epsilon_mm=eps,interval_epsilon=1e-12)
    result['v5_face_relation_audit']=audit_face_relations(result,paths,numeric_epsilon_mm=eps,apply=True)
    return result
