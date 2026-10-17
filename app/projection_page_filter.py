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
        body=sum(not document[i].get_image_info() and _visible_drawing_count(document[i],fitz)>=3 for i in later)
        p=document[0];area=p.rect.get_area()
        image_area=max(((fitz.Rect(im['bbox']) * p.rotation_matrix & p.rect).get_area() for im in p.get_image_info()),default=0)
        different_size = abs(sizes[0][0]-sizes[group[0]][0])>.5 or abs(sizes[0][1]-sizes[group[0]][1])>.5
        cover_text = any(word in p.get_text() for word in ('效果图','封面','尺码表','款式图'))
        if body>=3 and image_area>=area*.2 and (different_size or cover_text):
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
