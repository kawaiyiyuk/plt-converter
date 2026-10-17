"""Projection-only numbered sheet candidates, verified by vector seam evidence."""
import math
import re
from .projection_pdf_runtime import engine


def _style(drawing):
    color=drawing.get('color')
    dash=re.search(r'\[([^]]+)\]',drawing.get('dashes') or '')
    if color is None or not dash or not drawing.get('stroke_opacity',1):
        return None
    try:
        values=tuple(float(v) for v in dash.group(1).split())
    except ValueError:
        return None
    if not values or not any(v>0 for v in values):
        return None
    return tuple(color),float(drawing.get('width') or 0),values


def _same_style(a,b):
    return (len(a[0])==len(b[0]) and max(abs(x-y) for x,y in zip(a[0],b[0]))<=.03
            and abs(a[1]-b[1])<=.2 and len(a[2])==len(b[2])
            and max(abs(x-y) for x,y in zip(a[2],b[2]))<=.15)


def _frame(page,fitz):
    groups=[]
    for drawing in page.get_drawings():
        style=_style(drawing)
        if style is None:
            continue
        group=next((g for g in groups if _same_style(g[0],style)),None)
        if group is None:
            group=[style,[]];groups.append(group)
        for item in drawing['items']:
            if item[0]!='l':
                continue
            a,b=(point*page.rotation_matrix for point in item[1:3])
            if abs(a.y-b.y)<=.15 and abs(a.x-b.x)>=page.rect.width*.8 and 0<a.y<page.rect.height:
                group[1].append(('h',(a.y+b.y)/2,min(a.x,b.x),max(a.x,b.x)))
            if abs(a.x-b.x)<=.15 and abs(a.y-b.y)>=page.rect.height*.8 and 0<a.x<page.rect.width:
                group[1].append(('v',(a.x+b.x)/2,min(a.y,b.y),max(a.y,b.y)))
    candidates=[]
    for _,lines in groups:
        axes={}
        for axis in ('h','v'):
            clusters=[]
            for line in sorted((v for v in lines if v[0]==axis),key=lambda v:v[1]):
                if clusters and abs(line[1]-clusters[-1][0][1])<=.15:
                    clusters[-1].append(line)
                else:
                    clusters.append([line])
            if len(clusters)!=2:
                break
            axes[axis]=[(sum(v[1] for v in c)/len(c),min(v[2] for v in c),max(v[3] for v in c)) for c in clusters]
        if len(axes)!=2:
            continue
        left,right=(v[0] for v in axes['v']);top,bottom=(v[0] for v in axes['h'])
        if not(0<=left<right<=page.rect.width and 0<=top<bottom<=page.rect.height):
            continue
        if min(right-left,bottom-top)<engine.MM_TO_PT:
            continue
        if any(start>left+.5 or end<right-.5 for _,start,end in axes['h']):
            continue
        if any(start>top+.5 or end<bottom-.5 for _,start,end in axes['v']):
            continue
        candidates.append((left,top,right,bottom))
    return candidates[0] if len(candidates)==1 else None


def _footer(page,frame,fitz):
    spans=[]
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines',[]):
            for span in line['spans']:
                bbox=fitz.Rect(span['bbox'])*page.rotation_matrix
                if bbox.y0<0 or bbox.y1>page.rect.height or (bbox.y0+bbox.y1)/2<page.rect.height*.75:
                    continue
                if not re.fullmatch(r'[\d\s-]+',span['text']):
                    continue
                spans.append((bbox,span))
    lines=[]
    for rect,span in sorted(spans,key=lambda v:(v[0].y0,v[0].x0)):
        group=next((g for g in lines if abs(g[0][0].y0-rect.y0)<=.5),None)
        if group is None:
            group=[];lines.append(group)
        group.append((rect,span))
    candidates=[]
    for line in lines:
        line.sort(key=lambda v:v[0].x0)
        text=''.join(v[1]['text'] for v in line)
        # Some PDFs split digits into distinct text spans without emitting the
        # intervening space. Geometry supplies a separator, never stream order.
        if not re.fullmatch(r'\s*\d{1,2}[\s-]+\d{1,2}\s*',text):
            text=' '.join(v[1]['text'].strip() for v in line if v[1]['text'].strip())
        match=re.fullmatch(r'\s*(\d{1,2})[\s-]+(\d{1,2})\s*',text)
        if not match:
            continue
        row,column=map(int,match.groups())
        if not(1<=row<=24 and 1<=column<=24):
            continue
        fonts={v[1]['font'] for v in line};sizes=[v[1]['size'] for v in line]
        if len(fonts)!=1 or max(sizes)-min(sizes)>.5:
            continue
        candidates.append((row,column,next(iter(fonts)),sum(sizes)/len(sizes),
                           line[0][0].x0-frame[0],line[0][0].y0-frame[1]))
    return candidates[0] if len(candidates)==1 else None


def numbered_suggestion(source,indexes):
    fitz=engine.load_fitz()
    with fitz.open(stream=source,filetype='pdf') as document:
        indexes=engine.normalize_pdf_page_indexes(indexes,len(document))
        if len(indexes)<4:
            return None
        frames=[]
        for i in indexes:
            frame=_frame(document[i],fitz)
            if frame is None:
                return None
            frames.append(frame)
        width,height=frames[0][2]-frames[0][0],frames[0][3]-frames[0][1]
        if any(abs(f[2]-f[0]-width)>.5 or abs(f[3]-f[1]-height)>.5 for f in frames):
            return None
        footers=[_footer(document[i],frame,fitz) for i,frame in zip(indexes,frames)]
        if any(footer is None for footer in footers):
            return None
        ref=footers[0]
        if any(f[2]!=ref[2] or abs(f[3]-ref[3])>.5 or abs(f[4]-ref[4])>1 or abs(f[5]-ref[5])>1 for f in footers):
            return None
        positions=[f[:2] for f in footers]
        if len(set(positions))!=len(indexes) or min(r for r,c in positions)!=1 or min(c for r,c in positions)!=1:
            return None
        rows=max(r for r,c in positions);columns=max(c for r,c in positions)
        if not 0<=rows*columns-len(indexes)<=engine.effective_pdf_layout_max_blank_cells():
            return None
        slots=[None]*(rows*columns)
        for local,(r,c) in enumerate(positions):
            slots[(r-1)*columns+c-1]=local
        _,segments=engine._extract_vector_segments(document,fitz,page_indexes=indexes)
        normalized=[ [((a[0]-f[0],a[1]-f[1]),(b[0]-f[0],b[1]-f[1])) for a,b in lines]
                    for f,lines in zip(frames,segments)]
        signatures=engine.build_edge_signatures(normalized,width,height,0)
        result=engine._evaluate_layout(rows,columns,'row',slots,signatures)
        conflict=engine._blank_seam_conflicts(result,signatures)
        evidence=result['seam_evidence'];mean=result['confidence_score']
        positive=result['matched_seams']/evidence if evidence else 0
        perfect=result['perfect_seams']/evidence if evidence else 0
        if not conflict and evidence>=max(6,min(20,len(indexes)//2)) and mean>=.92 and positive>=.9 and perfect>=.7:
            confidence='high'
        elif not conflict and evidence>=4 and mean>=.75 and positive>=.75:
            confidence='medium'
        else:
            confidence='low'
        result.update(confidence=confidence,source='numbered_vector_frames',
                      page_slots=[indexes[i] if i is not None else None for i in slots],
                      crop_margins_mm=dict.fromkeys(engine.EDGE_NAMES,0),
                      source_rects_mm=[{'page_index':i,'x_mm':f[0]/engine.MM_TO_PT,'y_mm':f[1]/engine.MM_TO_PT,
                                        'width_mm':(f[2]-f[0])/engine.MM_TO_PT,'height_mm':(f[3]-f[1])/engine.MM_TO_PT}
                                       for i,f in zip(indexes,frames)],
                      blank_seam_conflict=bool(conflict),layout_ambiguous=False)
        if confidence=='low':
            result['reason']='页面编号与真实接缝未能相互验证，原分页保留'
        return result
