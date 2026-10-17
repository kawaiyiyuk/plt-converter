"""Conservative projection-only page suggestions; never mutates the document.

Engineering heuristics, not PDF semantics: users can restore every suggestion.
"""
import json


def normalize_selection(value, page_count):
    from .projection_pdf_runtime import engine
    if isinstance(value, str):
        try:
            value = json.loads(value)
            if not isinstance(value,list):
                raise ValueError('invalid page list')
        except (ValueError, TypeError) as error:
            raise ValueError('included_page_indexes 必须是 JSON 页码列表') from error
    return engine.normalize_pdf_page_indexes(value, page_count)


def selection_details(meta, indexes):
    suggestions = {p['index']:p for p in (meta.get('page_filter') or {}).get('excluded_pages', [])}
    return {'included_page_indexes':indexes,
            'excluded_pages':[suggestions.get(i, {'index':i,'reason':'user','label':'已排除'})
                              for i in range(meta['page_count']) if i not in indexes]}



def _visible_drawing_count(page, fitz, drawings=None):
    count = 0
    for drawing in page.get_drawings() if drawings is None else drawings:
        stroke = drawing.get('color')
        fill = drawing.get('fill')
        has_stroke = stroke is not None and any(c < 1 for c in stroke) and drawing.get('stroke_opacity', 1) > 0
        has_fill = fill is not None and any(c < 1 for c in fill) and drawing.get('fill_opacity', 1) > 0
        rect = fitz.Rect(drawing['rect'])
        if has_fill and (rect * page.rotation_matrix).intersects(page.rect):
            count += 1
            continue
        if not has_stroke:
            continue
        padding = max(float(drawing.get('width') or 0) / 2, .01)
        stroke_bounds = []
        for item in drawing['items']:
            if item[0] == 're':
                box = item[1]
                # A rectangle may enclose a page while all four stroked edges
                # are outside it. The enclosing bbox is not visible ink.
                stroke_bounds.extend((fitz.Rect(box.x0,box.y0,box.x1,box.y0),
                                      fitz.Rect(box.x1,box.y0,box.x1,box.y1),
                                      fitz.Rect(box.x0,box.y1,box.x1,box.y1),
                                      fitz.Rect(box.x0,box.y0,box.x0,box.y1)))
            elif item[0] == 'l':
                first,second=item[1:3]
                stroke_bounds.append(fitz.Rect(min(first.x,second.x),min(first.y,second.y),
                                               max(first.x,second.x),max(first.y,second.y)))
            else:
                # Curves/quads use a conservative bound: uncertainty retains
                # a source page rather than discarding potentially visible ink.
                stroke_bounds.append(rect)
        for bound in stroke_bounds:
            bound = fitz.Rect(bound.x0-padding,bound.y0-padding,bound.x1+padding,bound.y1+padding)
            if (bound * page.rotation_matrix).intersects(page.rect):
                count += 1
                break
    return count


def _has_visible_vector_content(page, fitz):
    """Conservative subpixel protection before declaring a raster pure white."""
    if page.get_text().strip() or _visible_drawing_count(page, fitz):
        return True
    for annotation in page.annots() or ():
        if (annotation.rect * page.rotation_matrix).intersects(page.rect):
            return True
    return False


def _edge_cut_edges(page, fitz, drawings=None):
    box=page.rect
    rx0,ry0,rx1,ry1=box.x0-1,box.y0-1,box.x1+1,box.y1+1
    segments=0
    edges=set()
    for drawing in page.get_drawings() if drawings is None else drawings:
        stroke=drawing.get('color')
        if not (stroke is not None and any(c < 1 for c in stroke) and drawing.get('stroke_opacity', 1) > 0):
            continue
        for item in drawing['items']:
            if item[0]!='l':
                continue
            a,b=(point * page.rotation_matrix for point in item[1:3])
            dx,dy=b.x-a.x,b.y-a.y
            t0,t1=0.0,1.0
            visible=True
            for slope,offset in ((-dx,a.x-rx0),(dx,rx1-a.x),(-dy,a.y-ry0),(dy,ry1-a.y)):
                if not slope:
                    if offset<0:
                        visible=False
                        break
                    continue
                ratio=offset/slope
                if slope<0:
                    if ratio>t1:
                        visible=False
                        break
                    t0=max(t0,ratio)
                else:
                    if ratio<t0:
                        visible=False
                        break
                    t1=min(t1,ratio)
            if not visible:
                continue
            hit=set()
            for point in (a,b):
                if point.x-box.x0<=1: hit.add('left')
                if box.x1-point.x<=1: hit.add('right')
                if point.y-box.y0<=1: hit.add('top')
                if box.y1-point.y<=1: hit.add('bottom')
            if hit:
                segments+=1
                edges|=hit
    return segments,edges


def _page0_evidence_drawings(page, fitz):
    """Exclude only structurally proven measuring decorations from tile evidence.

    Rendering, blank-page checks and body density keep the original drawings.
    Mixed or incomplete objects stay, even if a subset resembles a decoration.
    """
    from .projection_numbered_layout import _frame, _style, _same_style
    drawings=page.get_drawings()
    tolerance=.15  # Same point tolerance as the existing numbered-frame probe.
    omitted=set()

    def near(a,b):
        return abs(a-b)<=tolerance

    def point_key(point):
        # Buckets keep the matching bounded. Boundary misses conservatively
        # retain a decoration; every match is checked geometrically below.
        return round(point.x/tolerance),round(point.y/tolerance)

    def line_key(a,b):
        return tuple(sorted((point_key(a),point_key(b))))

    fills={};arrows={}
    for index,drawing in enumerate(drawings):
        items=drawing['items']
        if len(items)!=2 or any(item[0]!='l' for item in items):
            continue
        a,b=items[0][1:3];c,d=items[1][1:3]
        if drawing.get('type')=='f' and a==d and b==c:
            fills.setdefault(line_key(a,b),[]).append((index,a,b))
        elif drawing.get('type')=='s' and near(b,c):
            arrows.setdefault(point_key(b),[]).append((index,a,b,d))
    for index,baseline in enumerate(drawings):
        items=baseline['items']
        if baseline.get('type')!='s' or len(items)!=1 or items[0][0]!='l':
            continue
        a,b=items[0][1:3]
        length=abs(b-a)
        if length<=tolerance:
            continue
        matching_fills=[entry for entry in fills.get(line_key(a,b),[])
                        if (near(entry[1],a) and near(entry[2],b)
                            or near(entry[1],b) and near(entry[2],a))
                        and drawings[entry[0]].get('fill')==baseline.get('color')
                        and drawings[entry[0]].get('fill_opacity',1)==baseline.get('stroke_opacity',1)]
        if not matching_fills:
            continue
        for tip,end in ((a,b),(b,a)):
            direction=(end-tip)/length
            for arrow_index,first,vertex,last in arrows.get(point_key(tip),[]):
                arrow=drawings[arrow_index]
                if (not near(vertex,tip) or any(arrow.get(key)!=baseline.get(key)
                        for key in ('color','width','dashes','stroke_opacity'))):
                    continue
                u,v=first-vertex,last-vertex
                along_u=u.x*direction.x+u.y*direction.y
                along_v=v.x*direction.x+v.y*direction.y
                cross_u=u.x*direction.y-u.y*direction.x
                cross_v=v.x*direction.y-v.y*direction.x
                if (0<along_u<length and 0<along_v<length
                        and abs(along_u-along_v)<=tolerance
                        and abs(cross_u+cross_v)<=tolerance
                        and cross_u*cross_v<0 and min(abs(cross_u),abs(cross_v))>tolerance):
                    omitted.update((index,arrow_index))
                    omitted.update(entry[0] for entry in matching_fills)

    # Reuse the existing unique, complete same-style frame recognizer. An
    # isolated dashed line, incomplete frame or mixed drawing is never removed.
    styled=[(i,_style(drawing)) for i,drawing in enumerate(drawings)]
    if any(style is not None for _,style in styled):
        frame=_frame(page,fitz)
        if frame is not None:
            left,top,right,bottom=frame
            groups=[]
            for index,style in styled:
                if style is None or drawings[index].get('type')!='s':
                    continue
                sides=set()
                for item in drawings[index]['items']:
                    if item[0]!='l':
                        sides.clear();break
                    a,b=(point*page.rotation_matrix for point in item[1:3])
                    if abs(a.y-b.y)<=tolerance and abs(a.x-b.x)>=page.rect.width*.8:
                        side=next((label for value,label in ((top,'top'),(bottom,'bottom'))
                                   if abs(a.y-value)<=tolerance and abs(b.y-value)<=tolerance),None)
                    elif abs(a.x-b.x)<=tolerance and abs(a.y-b.y)>=page.rect.height*.8:
                        side=next((label for value,label in ((left,'left'),(right,'right'))
                                   if abs(a.x-value)<=tolerance and abs(b.x-value)<=tolerance),None)
                    else:
                        side=None
                    if side is None:
                        sides.clear();break
                    sides.add(side)
                if not sides:
                    continue
                group=next((group for group in groups if _same_style(group[0],style)),None)
                if group is None:
                    group=[style,set(),[]];groups.append(group)
                group[1].update(sides);group[2].append(index)
            for _,sides,indexes in groups:
                if len(sides)==4:
                    omitted.update(indexes)
    return [drawing for index,drawing in enumerate(drawings) if index not in omitted]


def _page0_pattern_evidence(page, fitz, samples, drawing_count=None, edge_evidence=None):
    """Page 0 keeps its own paper-pattern ink: vector density or cut strokes."""
    count=_visible_drawing_count(page, fitz) if drawing_count is None else drawing_count
    if count>=3:
        ordered=sorted(samples);n=len(ordered)
        median=ordered[n//2] if n%2 else (ordered[n//2-1]+ordered[n//2])/2
        if count>=.5*median:
            return True
    segments,edges=_edge_cut_edges(page, fitz) if edge_evidence is None else edge_evidence
    return segments>=4 and len(edges)>=2


def detect_page_filter(document):
    from .projection_pdf_runtime import load_fitz
    fitz = load_fitz()
    count=len(document)
    excluded=[]
    if count < 4:
        return {'version':1,'included_page_indexes':list(range(count)),'excluded_pages':[]}
    # Largest size cluster supplies context; neither small size nor images alone
    # exclude a page. Display rect accounts for page rotation and crop box.
    sizes=[(p.rect.width,p.rect.height) for p in document]
    groups=[[i for i,(w,h) in enumerate(sizes) if abs(w-a)<=.5 and abs(h-b)<=.5] for a,b in sizes]
    group=max(groups,key=len)
    dominant=sizes[group[0]][0]*sizes[group[0]][1]
    if len(group)>=3:
        for i,(w,h) in enumerate(sizes):
            if w*h > dominant*.1:
                continue
            p=document[i]
            if _has_visible_vector_content(p,fitz):
                continue
            scale=min(2,2048/max(w,h))
            pix=p.get_pixmap(matrix=fitz.Matrix(scale,scale),colorspace=fitz.csRGB,alpha=False,annots=True)
            if min(pix.samples,default=255)==255:
                excluded.append({'index':i,'reason':'blank_small_page','label':'异常空白小页'})
        later=[i for i in group if i>0]
        # Photo cover requires several independent signals, and a body formed
        # by vector paper pages. Pure scanned documents are never auto-filtered.
        p=document[0];area=p.rect.get_area()
        images=p.get_image_info()
        image_area=max(((fitz.Rect(im['bbox']) * p.rotation_matrix & p.rect).get_area() for im in images),default=0)
        different_size = abs(sizes[0][0]-sizes[group[0]][0])>.5 or abs(sizes[0][1]-sizes[group[0]][1])>.5
        text=p.get_text() if image_area>=area*.2 and not different_size else ''
        cover_text = any(word in text for word in ('效果图','封面','尺码表','款式图'))
        if image_area>=area*.2 and (different_size or cover_text):
            evidence_drawings=_page0_evidence_drawings(p,fitz)
            count0=_visible_drawing_count(p,fitz,evidence_drawings)
            segments,edges=_edge_cut_edges(p,fitz,evidence_drawings)
            samples=[]
            # Edge-cut evidence independently protects a real tile. For fewer
            # than three drawings the median guard cannot apply, so only three
            # qualifying body pages are needed; otherwise retain all eight.
            if not (segments>=4 and len(edges)>=2):
                for i in later:
                    if document[i].get_image_info():
                        continue
                    c=_visible_drawing_count(document[i],fitz)
                    if c>=3:
                        samples.append(c)
                        if len(samples)>=(3 if count0<3 else 8):
                            break
            if len(samples)>=3 and not _page0_pattern_evidence(p,fitz,samples,count0,(segments,edges)):
                pix=p.get_pixmap(matrix=fitz.Matrix(min(.5,2048/max(p.rect.width,p.rect.height)),min(.5,2048/max(p.rect.width,p.rect.height))),colorspace=fitz.csRGB,alpha=False,annots=True)
                data=pix.samples
                colored_count = 0
                bins = set()
                for r,g,b in zip(data[0::3],data[1::3],data[2::3]):
                    if max(r,g,b)-min(r,g,b)>=24:
                        colored_count += 1
                        bins.add((r//16,g//16,b//16))
                if colored_count>=pix.width*pix.height*.1 and len(bins)>=128:
                    excluded.append({'index':0,'reason':'cover','label':'封面'})
    excluded.sort(key=lambda item:item['index'])
    omitted={item['index'] for item in excluded}
    return {'version':1,'included_page_indexes':[i for i in range(count) if i not in omitted],
            'excluded_pages':excluded}
