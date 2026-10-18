#!/usr/bin/env python3
"""Offline prototype: PDF -> current inspect/filter/analyze/build -> scene v1.

Run from a plt-converter checkout with its existing dependencies:
  python scripts/prototype_projection_scene.py --pdf input.pdf --output scene.json

Only a new output file is created. Existing inputs/results are never replaced.
This reuses current PDF page filtering/layout rules; it does not start an API,
queue, store, service or HTTP client. PLT is outside this PDF-only prototype.
Direct local functions do not prove RQ/supervision/deployment or device behavior.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import socket
import sys
import time


def disable_network():
    """Guard this command's Python socket use, including DNS and connect_ex."""
    def deny(*args,**kwargs):
        raise RuntimeError('offline scene prototype prohibits network')
    socket.create_connection=deny
    socket.getaddrinfo=deny
    socket.socket.connect=deny
    socket.socket.connect_ex=deny


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf',required=True,help='Original PDF; read only')
    parser.add_argument('--output',required=True,help='New scene JSON; must not exist')
    parser.add_argument('--metadata-mode',choices=('current','original'),default='current',help='Existing analyzer choice; current matches the current projection prototype')
    args=parser.parse_args()
    disable_network()
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from app.projection_scene import SceneUnsupported,check_output_path,export_scene,write_scene_output
    try:
        # Input/output protection precedes importing the heavier analyzer stack.
        output=check_output_path(args.output,[args.pdf])
        from app.projection_child import inspect
        from app.projection_layout import analyze_source,build_layout
        source_path=Path(args.pdf);source=source_path.read_bytes()
        digest=hashlib.sha256(source).hexdigest();stats={}
        start=time.perf_counter();meta=inspect(source);stats['inspect_seconds']=time.perf_counter()-start
        indexes=meta['page_filter']['included_page_indexes']
        start=time.perf_counter()
        suggestion=analyze_source(source,args.metadata_mode,meta.get('embedded_layout'),indexes)
        stats['analyze_seconds']=time.perf_counter()-start
        if suggestion.get('confidence') not in ('medium','high'):
            raise SceneUnsupported('existing analyzer did not produce an accepted layout; no scene written')
        layout=build_layout(meta['pages'],suggestion['rows'],suggestion['columns'],suggestion['order'],suggestion.get('crop_margins_mm') or {},suggestion['page_slots'],suggestion.get('source') or 'vector_seams',indexes,suggestion.get('source_rects_mm'))
        start=time.perf_counter();scene=export_scene(source,layout);stats['scene_seconds']=time.perf_counter()-start
        if hashlib.sha256(source_path.read_bytes()).hexdigest()!=digest:
            raise SceneUnsupported('input changed during analysis; no scene written')
        size=write_scene_output(scene,output,[source_path])
        stats.update(output=str(output),source_sha256=digest,source_unchanged=True,network='blocked',metadata_mode=args.metadata_mode,pages=meta['page_count'],included_pages=len(indexes),excluded_pages=meta['page_filter']['excluded_pages'],confidence=suggestion['confidence'],layout_source=layout['source'],width_mm=layout['width_mm'],height_mm=layout['height_mm'],scene_bytes=size)
    except (OSError,ValueError,RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(stats,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
