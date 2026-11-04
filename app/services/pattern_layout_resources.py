"""Lossless layered delivery: small index, source vectors, selected candidate, audit.

No source segment/candidate/audit is discarded. Duplication-heavy audit stays in
its own gzip resource. Clients need not materialize it to select or place a piece.
"""
import gzip
import json
import io
import math
from pathlib import Path


class BudgetWriter:
    def __init__(self,stream,budget):self.stream,self.budget=stream,budget
    def write(self,data):
        if self.budget['used']+len(data)>self.budget['maximum']:raise ValueError('operator-configured result storage budget exceeded')
        self.budget['used']+=len(data);return self.stream.write(data)
    def flush(self):return self.stream.flush()
    def tell(self):return self.stream.tell()


def write_gzip(path,data,budget):
    with path.open('wb') as raw:
        limited=BudgetWriter(raw,budget)
        with gzip.GzipFile(filename='',fileobj=limited,mode='wb',mtime=0) as compressed:
            with io.TextIOWrapper(compressed,encoding='utf-8') as stream:
                json.dump(data,stream,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def read_gzip(path):
    with gzip.open(path,'rt',encoding='utf-8') as stream:return json.load(stream)


def candidate_metadata(candidate):
    fields=('id','status','bounds','area_mm2','source_closed_path_ids','semantic_piece_verified','content_complete_for_region','text_ids','variant_label_cn','parent_candidate_id','excluded_rectangle')
    result={key:candidate[key] for key in fields if key in candidate}
    outer=candidate.get('outer',[])
    if len(outer)>=2:
        from shapely.geometry import LineString
        preview=[list(p) for p in LineString(outer).simplify(.5).coords]
        result['preview_outline']={'points':preview,'tolerance_mm':.5,'simplified':True,'not_for_export':True}
    result.update(fragment_count=len(candidate.get('fragments',[])),
                  issue_codes=sorted({issue['code'] for issue in candidate.get('issues',[]) if 'code' in issue}),
                  resource_id=candidate['id'])
    return result


def write_resources(folder,result,maximum_bytes):
    budget={'used':0,'maximum':maximum_bytes}
    folder=Path(folder);resources=folder/'resources';resources.mkdir()
    # Keep full original experimental state, including every previous candidate
    # and complete auxiliary/alternative observations, available for exact restore.
    write_gzip(resources/'audit.json.gz',result,budget)
    styles=[];style_ids={};paths=[]
    for path in result['paths']:
        identifiers=[]
        for style in path['segment_styles']:
            key=json.dumps(style,sort_keys=True,separators=(',',':'))
            if key not in style_ids:style_ids[key]=len(styles);styles.append(style)
            identifiers.append(style_ids[key])
        paths.append({key:path[key] for key in ('id','points','pen','line_width_units')} | {'segment_style_ids':identifiers})
    write_gzip(resources/'source-geometry.json.gz',{'schema_version':1,'source':result['source'],'paths':paths,'style_table':styles,'non_path_shapes':result['non_path_shapes']},budget)
    candidates=resources/'candidates';candidates.mkdir();seen=set()
    from .pattern_layout_api import source_phase_prefixes
    phases=source_phase_prefixes(result['paths']);source_paths={p['id']:p for p in result['paths']}
    for collection in ('pieces','auxiliary'):
        for candidate in result[collection]:
            if candidate['id'] in seen:raise ValueError('duplicate candidate resource identity')
            seen.add(candidate['id'])
            # Detailed issue geometry and all audits remain in audit, not the
            # phone's working set. Source intervals themselves stay unmodified.
            data={key:candidate[key] for key in ('id','status','bounds','outer','fragments','semantic_piece_verified','content_complete_for_region','text_ids','variant_label_cn','parent_candidate_id','excluded_rectangle') if key in candidate}
            if 'fragments' in data:
                data['fragments']=[dict(f,preview_phase_mm=phases[f['path_id']][f['segment_index']]+f['t0']*math.dist(*source_paths[f['path_id']]['points'][f['segment_index']:f['segment_index']+2])) for f in data['fragments']]
            data['issue_codes']=candidate_metadata(candidate)['issue_codes']
            write_gzip(candidates/(candidate['id']+'.json.gz'),data,budget)
    index={'schema_version':1,'delivery_version':1,'source':result['source'],
           'pieces':[candidate_metadata(p) for p in result['pieces']],
           'auxiliary':[candidate_metadata(p) for p in result['auxiliary']],
           'resources':{'source_geometry':True,'candidate_fragments':True,'full_audit':True},
           'unassigned':{'count':result['unassigned']['count'],'length_mm':result['unassigned']['length_mm']}}
    with (folder/'index.json').open('wb') as stream:
        writer=BudgetWriter(stream,budget)
        for chunk in json.JSONEncoder(ensure_ascii=False,separators=(',',':'),allow_nan=False).iterencode(index):writer.write(chunk.encode('utf-8'))
    write_archive(folder,result['source'],budget)
    return budget['used']


def write_archive(folder,source,budget):
    """Lossless, bounded transport of compressed members; no giant JSON load."""
    import hashlib
    import zipfile
    folder=Path(folder)
    files=[folder/'index.json']+sorted((folder/'resources').rglob('*.gz'))
    manifest={'schema_version':1,'source_sha256':source['sha256'],'runtime_version':source.get('runtime_version'),'files':[]}
    for path in files:
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            while chunk:=stream.read(1024*1024):digest.update(chunk)
        manifest['files'].append({'path':str(path.relative_to(folder)),'sha256':digest.hexdigest(),'bytes':path.stat().st_size})
    manifest_data=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    with (folder/'archive.zip').open('wb') as raw:
        limited=BudgetWriter(raw,budget)
        with zipfile.ZipFile(limited,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            def member_info(name):
                # Fixed headers keep archive identity stable across retries,
                # directories, filesystem mtimes and process umasks as well.
                info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
                info.compress_type=zipfile.ZIP_STORED
                info.create_system=3
                info.external_attr=0o100600<<16
                return info
            archive.writestr(member_info('manifest.json'),manifest_data)
            for path in files:
                info=member_info(str(path.relative_to(folder)))
                info.file_size=path.stat().st_size
                with path.open('rb') as source,archive.open(info,'w',force_zip64=True) as target:
                    while chunk:=source.read(64*1024):target.write(chunk)
