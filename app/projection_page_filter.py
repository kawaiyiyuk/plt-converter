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



def _visible_drawing_count(page, fitz):
    count = 0
    for drawing in page.get_drawings():
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


def _edge_cut_edges(page, fitz):
    box=page.rect
    rx0,ry0,rx1,ry1=box.x0-1,box.y0-1,box.x1+1,box.y1+1
    segments=0
    edges=set()
    for drawing in page.get_drawings():
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


def _page0_pattern_evidence(page, fitz, samples):
    """Page 0 keeps its own paper-pattern ink: vector density or cut strokes."""
    count=_visible_drawing_count(page, fitz)
    if count>=3:
        ordered=sorted(samples);n=len(ordered)
        median=ordered[n//2] if n%2 else (ordered[n//2-1]+ordered[n//2])/2
        if count>=.5*median:
            return True
    segments,edges=_edge_cut_edges(page, fitz)
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
        image_area=max(((fitz.Rect(im['bbox']) * p.rotation_matrix & p.rect).get_area() for im in p.get_image_info()),default=0)
        different_size = abs(sizes[0][0]-sizes[group[0]][0])>.5 or abs(sizes[0][1]-sizes[group[0]][1])>.5
        cover_text = any(word in p.get_text() for word in ('效果图','封面','尺码表','款式图'))
        if image_area>=area*.2 and (different_size or cover_text):
            samples=[]
            for i in later:
                if document[i].get_image_info():
                    continue
                c=_visible_drawing_count(document[i],fitz)
                if c>=3:
                    samples.append(c)
                    if len(samples)>=8:
                        break
            if len(samples)>=3 and not _page0_pattern_evidence(p,fitz,samples):
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
