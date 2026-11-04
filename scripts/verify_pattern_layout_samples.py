"""Sequential read-only PLT fixture probe with explicit local manifest/output.

Manifest: [{"id":"F01","path":"/absolute/local/sample.plt","name":"optional"}].
Never check a private fixture manifest or raw user source into this repository.
"""
import argparse
import json
from pathlib import Path
import re
import time
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.pattern_layout_api import export_project, runtime_version
from app.services.pattern_layout_variants import recognize_with_variants as recognize_source


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--acknowledge-source-warnings',action='store_true',help='Explicitly acknowledge fixture warnings for this local exporter probe only')
    parser.add_argument('--layered',action='store_true',help='Write gzip resources/archive instead of a monolithic result JSON')
    parser.add_argument('--result-budget-bytes',type=int,help='Explicit local layered-output budget; required with --layered')
    args=parser.parse_args()
    if args.layered and not args.result_budget_bytes:parser.error('--layered requires --result-budget-bytes')
    rows=json.loads(args.manifest.read_text());output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True);report=[];frozen_runtime=runtime_version()
    (output/'runtime-freeze.json').write_text(json.dumps({'runtime_version':frozen_runtime},indent=2))
    for row in rows:
        identifier=row['id'];path=Path(row['path']).resolve()
        if not re.fullmatch(r'[A-Za-z0-9_-]+',identifier):raise ValueError('fixture id must be filename-safe')
        if path in ((output/(identifier+'.plt')).resolve(),(output/(identifier+'.json')).resolve()):raise ValueError('output would overwrite a source fixture')
        if runtime_version()!=frozen_runtime:raise RuntimeError('engine runtime changed during fixture run')
        start=time.monotonic();note={'id':identifier,'name':row.get('name',path.name)}
        try:
            raw=path.read_bytes();result=recognize_source(raw,note['name'])
            if runtime_version()!=frozen_runtime:raise RuntimeError('engine runtime changed during recognition')
            if args.layered:
                from app.services.pattern_layout_resources import write_resources
                folder=output/identifier;folder.mkdir()
                note['layered_bytes']=write_resources(folder,result,args.result_budget_bytes)
            else:
                with (output/(identifier+'.json')).open('w') as stream:json.dump(result,stream,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            note.update(status='recognized_experimental',source=result['source'],paths=len(result['paths']),pieces=len(result['pieces']),auxiliary=len(result['auxiliary']),source_alternatives=sum(p['id'].startswith('S-') for p in result['pieces']),text_shapes=len(result['non_path_shapes']))
            candidate=next((p for p in result['pieces'] if p['fragments']),None)
            if candidate:
                project={'schema_version':1,'source_sha256':result['source']['sha256'],'candidates':[candidate],'placements':[{'piece_id':candidate['id'],'x':0,'y':0,'rotation_deg':0,'mirror_x':False}]}
                if args.acknowledge_source_warnings:project['acknowledged_source_warnings']=result['source']['warnings']
                try:
                    data=export_project(raw,project);(output/(identifier+'.plt')).write_bytes(data);note['first_candidate_export']='passed_independent_linear_verify'
                except Exception as exc:note['first_candidate_export']='blocked: '+str(exc)
        except Exception as exc:note.update(status='failed',error=str(exc))
        note['elapsed_seconds']=round(time.monotonic()-start,3);report.append(note)
        print(json.dumps(note,ensure_ascii=False),flush=True)
        (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
