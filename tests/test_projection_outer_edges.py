"""Real PDF geometry/pixel regressions for projection-only outer preservation."""
import unittest

import pymupdf as fitz
import test_projection as fixtures
from test_projection_rotation import viewport, trig
from app.projection import render_tile, _source_over_block
from app.projection_layout import build_layout

PT = 72 / 25.4
CROP = dict(left=10, right=10, top=10, bottom=10)


def pages(sizes):
    return [dict(index=i, width_mm=w, height_mm=h) for i, (w,h) in enumerate(sizes)]


def source_pdf(count=4):
    doc = fitz.open()
    for i in range(count):
        page = doc.new_page(width=100*PT, height=100*PT)
        page.draw_rect(fitz.Rect(2*PT,2*PT,98*PT,98*PT),color=(0,0,0),width=PT)
        page.insert_text((3*PT,8*PT),'OUTER %s' % i, fontsize=9, color=(0,0,0))
        page.draw_circle((5*PT,5*PT),PT,color=None,fill=(1,0,0))
        annot=page.add_rect_annot(fitz.Rect(92*PT,92*PT,97*PT,97*PT))
        annot.set_colors(stroke=(0,0,1)); annot.set_border(width=PT); annot.update()
        # Interior marker used to prove that restoring edges never shifts seams.
        page.draw_circle((50*PT,50*PT),PT,color=None,fill=(0,1,0))
    result=doc.tobytes(); doc.close(); return result


def near(pix,x,y,color):
    return any(all(abs(pix.pixel(px,py)[c]-color[c])<65 for c in range(3))
               for px in range(max(0,round(x)-3),min(pix.width,round(x)+4))
               for py in range(max(0,round(y)-3),min(pix.height,round(y)+4)))


class OuterGeometryTests(unittest.TestCase):
    def test_two_by_two_restores_outer_sides_without_moving_inner_seams(self):
        layout=build_layout(pages([(100,100)]*4),2,2,'row',CROP)
        self.assertIs(layout.get('preserve_outer_edges'),True)
        self.assertEqual((layout['width_mm'],layout['height_mm']),(180,180))
        expected=[(0,0,90,90,0,0),(90,0,90,90,10,0),
                  (0,90,90,90,0,10),(90,90,90,90,10,10)]
        for placement,(x,y,w,h,left,top) in zip(layout['placements'],expected):
            self.assertEqual((placement['x_mm'],placement['y_mm'],placement['width_mm'],placement['height_mm']), (x,y,w,h))
            self.assertEqual((placement['crop_margins_mm']['left'],placement['crop_margins_mm']['top']),(left,top))
        # Original cropped origins 0/80 are shifted globally by 10; internal source 10 stays there.
        self.assertEqual(layout['placements'][1]['x_mm']+10-layout['placements'][1]['crop_margins_mm']['left'],90)

    def test_single_page_retains_complete_boundary(self):
        layout=build_layout(pages([(100,120)]),1,1,'column',CROP)
        self.assertEqual((layout['width_mm'],layout['height_mm']),(100,120))
        self.assertEqual(layout['placements'][0]['crop_margins_mm'],dict.fromkeys(CROP,0))

    def test_holes_mixed_sizes_column_order_and_empty_grid_lines(self):
        sizes=[(100,120),(130,100),(80,110),(90,90)]
        # Column order: [0,1,None] / [2,None,3] / [None,None,None].
        slots=[0,1,None,2,None,3,None,None,None]
        layout=build_layout(pages(sizes),3,3,'column',CROP,slots)
        by_index={p['page_index']:p for p in layout['placements']}
        self.assertEqual(by_index[0]['crop_margins_mm'],dict(left=0,right=10,top=0,bottom=10))
        self.assertEqual(by_index[1]['crop_margins_mm'],dict(left=0,right=0,top=10,bottom=0))
        self.assertEqual(by_index[2]['crop_margins_mm'],dict(left=10,right=0,top=0,bottom=0))
        self.assertEqual(by_index[3]['crop_margins_mm'],dict.fromkeys(CROP,0))
        # Core widths 110,70,110; heights 100,80,70. Empty final column remains 110.
        self.assertEqual((layout['width_mm'],layout['height_mm']),(300,270))
        empty=build_layout(pages([(100,100)]*2),3,3,'row',CROP,[0,None,None,None,None,None,None,None,1])
        self.assertEqual((empty['width_mm'],empty['height_mm']),(260,260))

    def test_real_two_by_two_pdf_outer_text_outline_annotations_and_centers(self):
        source=source_pdf(); layout=build_layout(pages([(100,100)]*4),2,2,'row',CROP)
        pix=fitz.Pixmap(render_tile(source,dict(x_mm=0,y_mm=0,width_mm=180,height_mm=180,width_px=720,height_px=720),layout=layout))
        for x,y in ((2,40),(178,40),(40,2),(40,178)):
            self.assertTrue(near(pix,x*4,y*4,(0,0,0)))
        self.assertTrue(near(pix,172*4,175*4,(0,0,255)))
        for x,y in ((50,50),(130,50),(50,130),(130,130)):
            self.assertTrue(near(pix,x*4,y*4,(0,255,0)))
        # Text in the first outer 10mm strip survives, with more ink than just border/circle.
        text_ink=sum(max(pix.pixel(x,y))<80 for y in range(22,34) for x in range(40,130))
        self.assertGreater(text_ink,20)
        # The internal source outer outlines at x=98/2 were removed, not duplicated at the join.
        self.assertEqual(pix.pixel(88*4,40*4),(255,255,255))
        self.assertEqual(pix.pixel(92*4,40*4),(255,255,255))

    def test_rotated_clip_negative_axis_keeps_the_inclusive_source_edge(self):
        # At 90/180 degrees an inverse axis runs backwards. Pixel 0 is exactly
        # the included source minimum; it must survive rather than be rounded
        # out as though it were the excluded maximum.
        pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,2,1),True)
        pix.set_rect(pix.irect,(0,0,0,255))
        canvas=bytearray(b'\xff')*6
        _source_over_block(canvas,2,pix,(0,0,2,1),((-1,1,0,0.5,1.5),))
        self.assertEqual(bytes(canvas),bytes((0,0,0,255,255,255)))

    def test_rotated_scanline_matches_independent_pixel_membership(self):
        pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,16,12),True)
        pix.set_rect(pix.irect,(200,0,0,255))
        cases=[((-1,8,0,.5,6.5),(0,0,1,0,2)),
               ((1,.25,0,2.75,9.75),(0,-.25,1,1.25,7.25)),
               ((0,.5,0,.5,1.5),),((0,1.5,0,.5,1.5),)]
        for angle in (0,1,37,89,90,137,180,217,270,317,359):
            c,s=trig(angle)
            for origin in (-3.25,0,2.75):
                cases.append(((c,origin,s,-4.25,8.75),(-s,-origin,c,-2.5,10.25)))
        for rows in cases:
            for block in ((0,0,16,12),(3,2,13,10)):
                with self.subTest(rows=rows,block=block):
                    canvas=bytearray(b'\xff')*(16*12*3)
                    _source_over_block(canvas,16,pix,block,rows)
                    for y in range(12):
                        for x in range(16):
                            inside=block[0]<=x<block[2] and block[1]<=y<block[3]
                            for a,origin,b,minimum,maximum in rows:
                                coordinate=origin+a*(x+.5)+b*(y+.5)
                                inside=inside and minimum<=coordinate<maximum
                            expected=bytes((200,0,0)) if inside else bytes((255,255,255))
                            offset=(y*16+x)*3
                            self.assertEqual(canvas[offset:offset+3],expected,(x,y))

    def test_rotated_layout_never_leaks_cropped_inner_borders(self):
        source=source_pdf()
        layout=build_layout(pages([(100,100)]*4),2,2,'row',CROP)
        # All these world regions are blank in the actual cropped layout.
        # MuPDF arbitrary-angle clip alone admits the source's discarded
        # x=2/98 and y=2/98 outlines through its transformed bounding box.
        points=[(98,40),(82,40),(40,82)]
        points += [(x,y) for x in range(80,101,2) for y in range(30,46,2)]
        points += [(x,y) for x in range(30,46,2) for y in range(80,89,2)]
        for angle in (0,37,90):
            values=viewport(180,180,angle,ppmm=4)
            pix=fitz.Pixmap(render_tile(source,values,layout=layout)); c,s=trig(angle)
            for x,y in points:
                px=round(4*(c*x-s*y-values['x_mm']))
                py=round(4*(s*x+c*y-values['y_mm']))
                self.assertEqual(pix.pixel(px,py),(255,255,255),(angle,x,y))

    def test_hole_overlap_keeps_both_pages_at_zero_and_rotated_angles(self):
        # Missing bottom-right slot restores page 1 bottom and page 2 right.
        # A later page has no ink at the first marker, so an opaque white
        # surface would erase it. Both equal and mixed paper sizes overlap.
        for second_height in (100,120):
            doc=fitz.open()
            for index in range(3):
                height=second_height if index==1 else 100
                page=doc.new_page(width=100*PT,height=height*PT)
                if index==1:
                    page.draw_circle((13*PT,(height-5)*PT),0.6*PT,color=None,fill=(1,0,0))
                if index==2:
                    page.draw_circle((95*PT,13*PT),0.6*PT,color=None,fill=(0,0,1))
            source=doc.tobytes();doc.close()
            layout=build_layout(pages([(100,100),(100,second_height),(100,100)]),2,2,'row',CROP)
            for angle in (0,37,90):
                with self.subTest(angle=angle,second_height=second_height):
                    values=viewport(layout['width_mm'],layout['height_mm'],angle,ppmm=4)
                    pix=fitz.Pixmap(render_tile(source,values,layout=layout)); c,s=trig(angle)
                    for (x,y),color in (((93,second_height-5),(255,0,0)),((95,second_height-7),(0,0,255))):
                        self.assertTrue(near(pix,4*(c*x-s*y-values['x_mm']),4*(s*x+c*y-values['y_mm']),color))

    def test_single_rotated_cropbox_userunit_page_matches_complete_source(self):
        source=fixtures.pdf_bytes(rotation=90,crop=True,userunit=2)
        with fitz.open(stream=source,filetype='pdf') as doc:
            page=doc[0]; size=(page.rect.width/PT,page.rect.height/PT)
            layout=build_layout(pages([size]),1,1,'row',CROP)
            values=dict(x_mm=0,y_mm=0,width_mm=size[0],height_mm=size[1],
                        width_px=round(page.rect.width),height_px=round(page.rect.height))
            self.assertEqual(render_tile(source,values,layout=layout),render_tile(source,values))
            for angle in (37,90):
                values=viewport(*size,angle)
                self.assertEqual(render_tile(source,values,layout=layout),render_tile(source,values))

    def test_legacy_layout_keeps_identical_bytes_with_global_crop(self):
        source=source_pdf(1)
        legacy=dict(crop_margins_mm=CROP,placements=[dict(page_index=0,x_mm=0,y_mm=0,width_mm=80,height_mm=80)])
        explicit=dict(legacy,placements=[dict(legacy['placements'][0],crop_margins_mm=CROP)])
        for angle in (0,37,90):
            values=viewport(80,80,angle)
            self.assertEqual(render_tile(source,values,layout=legacy),render_tile(source,values,layout=explicit))


class OuterHTTPTests(unittest.TestCase):
    setUp=fixtures.ProjectionTests.setUp
    metadata=fixtures.ProjectionTests.metadata
    identity=fixtures.ProjectionTests.identity
    upload=fixtures.ProjectionTests.upload
    queue_environment=fixtures.ProjectionTests.queue_environment
    analyze=fixtures.ProjectionTests.analyze
    analysis_status=fixtures.ProjectionTests.analysis_status
    execute_analysis=fixtures.ProjectionTests.execute_analysis

    def test_real_embedded_analysis_auto_manual_summary_dimensions_match(self):
        from app.services.pdf_metadata import encode_pdf_layout_metadata
        metadata=dict(version=1,rows=2,columns=2,order='row',page_slots=[0,1,2,3],
                      crop_margins_mm=CROP,drawing_width_mm=160,drawing_height_mm=160,complete_layout=True)
        with fitz.open(stream=source_pdf(),filetype='pdf') as doc:
            doc.set_metadata({'subject':encode_pdf_layout_metadata(metadata)}); source=doc.tobytes()
        body=self.upload(source).get_json(); doc_id=body['document_id']
        environment,_=self.queue_environment()
        with environment:
            response=self.analyze(doc_id,'original'); self.assertEqual(response.status_code,202)
            aid=response.get_json()['analysis_id']
            self.assertEqual(self.execute_analysis(doc_id,aid,'original')['status'],'ready')
            suggestion=self.analysis_status(doc_id).get_json()['suggestion']
            self.assertTrue(suggestion['preserve_outer_edges'])
            self.assertAlmostEqual(suggestion['width_mm'],180,places=4)
            self.assertAlmostEqual(suggestion['height_mm'],180,places=4)
            response=self.client.post(f'{fixtures.BASE}/{doc_id}/layout',headers={'Authorization':'x'},data=dict(method='auto',analysis_id=aid))
            self.assertEqual(response.status_code,200)
            auto=response.get_json()['layout']; self.assertEqual(auto['width_mm'],suggestion['width_mm'])
            self.assertTrue(auto['preserve_outer_edges'])
            # A previously cached ready suggestion may lack the new capability
            # and contain cropped dimensions. Re-apply rebuilds from source pages.
            meta=self.ledger.load(doc_id,self.owner)
            meta['analysis']['suggestion'].pop('preserve_outer_edges')
            meta['analysis']['suggestion']['width_mm']=160
            meta['analysis']['suggestion']['height_mm']=160
            self.assertTrue(self.ledger.update(doc_id,self.owner,meta,meta['_generation']))
            response=self.client.post(f'{fixtures.BASE}/{doc_id}/layout',headers={'Authorization':'x'},data=dict(method='auto',analysis_id=aid))
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.get_json()['layout'],auto)
            summary=self.client.get(f'{fixtures.BASE}/{doc_id}',headers={'Authorization':'x'}).get_json()
            self.assertEqual(summary['layout'],auto)
            response=self.client.post(f'{fixtures.BASE}/{doc_id}/layout',headers={'Authorization':'x'},
                data=dict(method='manual',rows=2,columns=2,order='row',**{f'crop_{k}_mm':v for k,v in CROP.items()}))
            self.assertEqual(response.status_code,200)
            manual=response.get_json()['layout']; self.assertTrue(manual['preserve_outer_edges'])
            self.assertAlmostEqual(manual['width_mm'],180,places=4)
            self.assertAlmostEqual(manual['height_mm'],180,places=4)
        self.assertEqual(self.ledger.source_path(doc_id).read_bytes(),source)
