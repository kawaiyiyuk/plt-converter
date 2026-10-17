import unittest
import pymupdf as fitz
from app.projection_page_filter import detect_page_filter
from app.projection_layout import build_layout

class PageFilterTests(unittest.TestCase):
    def fixture(self, small='blank', cover=False):
        d=fitz.open()
        if cover:
            p=d.new_page(width=560,height=650)
            pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,160,160),False)
            for y in range(160):
                for x in range(160): pix.set_pixel(x,y,(x*255//159,y*255//159,(x+y)*255//318))
            p.insert_image(fitz.Rect(40,40,550,600),pixmap=pix)
        for _ in range(3):
            p=d.new_page(width=595,height=842)
            for i in range(6): p.draw_line((20,20+i*50),(570,60+i*50))
        p=d.new_page(width=43,height=16)
        if small=='outside': p.draw_rect(fitz.Rect(-20,-16,473,111))
        if small=='line': p.draw_line((1,1),(40,12))
        if small=='faint': p.draw_line((1,1),(40,12),color=(.999,.999,.999),width=.001)
        if small=='annotation':
            a=p.add_rect_annot(fitz.Rect(1,1,40,12)); a.set_colors(stroke=(1,0,0)); a.update()
        b=d.tobytes();d.close();return b
    def detect(self,b):
        with fitz.open(stream=b,filetype='pdf') as d:return detect_page_filter(d)
    def test_offpage_geometry_is_blank_but_visible_small_content_retained(self):
        for kind in ('blank','outside'):
            self.assertEqual(self.detect(self.fixture(kind))['excluded_pages'][0]['index'],3)
        for kind in ('line','annotation','faint'):
            self.assertEqual(self.detect(self.fixture(kind))['excluded_pages'],[])
    def test_photo_cover_only_in_multi_page_vector_document(self):
        result=self.detect(self.fixture('line',True))
        self.assertEqual(result['excluded_pages'],[{'index':0,'reason':'cover','label':'封面'}])
        with fitz.open(stream=self.fixture('line',True),filetype='pdf') as d:
            one=fitz.open();one.insert_pdf(d,from_page=0,to_page=0)
            self.assertEqual(detect_page_filter(one)['excluded_pages'],[])
    def test_subset_layout_preserves_source_indexes_and_empty_positions(self):
        pages=[{'index':i,'width_mm':200,'height_mm':300} for i in range(5)]
        result=build_layout(pages,2,2,'row',{},[1,None,3,4],included_page_indexes=[1,3,4])
        self.assertEqual([p['page_index'] for p in result['placements']],[1,3,4])
        self.assertEqual(result['page_slots'],[1,None,3,4])
        self.assertEqual(result['placements'][1]['y_mm'],300)
    def test_selection_rejects_empty_bool_duplicate_out_of_range_and_strings(self):
        from app.projection_page_filter import normalize_selection
        for value in ([], [True], [0,0], [9], ['0'], 'null', 'broken'):
            with self.subTest(value=value),self.assertRaises(ValueError): normalize_selection(value,4)
        self.assertEqual(normalize_selection(None,4),[0,1,2,3])
        self.assertEqual(normalize_selection('[3,1]',4),[1,3])
    def test_scan_only_document_never_classified_as_cover(self):
        with fitz.open(stream=self.fixture(cover=True),filetype='pdf') as src:
            d=fitz.open()
            for original in src:
                p=d.new_page(width=original.rect.width,height=original.rect.height)
                p.insert_image(p.rect,pixmap=original.get_pixmap())
            result=detect_page_filter(d)
            self.assertNotIn('cover',[item['reason'] for item in result['excluded_pages']])
    def test_restore_all_layout_retains_legitimate_mixed_sizes(self):
        pages=[{'index':0,'width_mm':200,'height_mm':300},{'index':1,'width_mm':15,'height_mm':6}]
        r=build_layout(pages,1,2,'row',{},included_page_indexes=[0,1])
        self.assertEqual(r['width_mm'],215)
        self.assertEqual(r['placements'][1]['width_mm'],15)

    def test_rotated_small_vector_page_is_preserved(self):
        with fitz.open(stream=self.fixture('line'),filetype='pdf') as d:
            d[-1].set_rotation(90)
            self.assertEqual(detect_page_filter(d)['excluded_pages'],[])
    def test_original_metadata_subset_keeps_empty_original_slot(self):
        from app.projection_layout import analyze_source
        metadata={'complete_layout':True,'rows':2,'columns':2,'order':'row',
                  'page_slots':[0,1,2,None],'crop_margins_mm':{}}
        result=analyze_source(self.fixture(),'original',metadata,[0,2])
        self.assertEqual(result['page_slots'],[0,None,2,None])
    def test_same_size_color_scan_without_cover_marker_is_retained(self):
        with fitz.open(stream=self.fixture('line',True),filetype='pdf') as d:
            d[0].set_mediabox(fitz.Rect(0,0,595,842))
            self.assertNotIn('cover',[item['reason'] for item in detect_page_filter(d)['excluded_pages']])

    def test_detection_supports_legacy_fitz_module_when_pymupdf_name_is_absent(self):
        import builtins
        import fitz as legacy_fitz
        original_import=builtins.__import__
        def legacy_import(name,*args,**kwargs):
            if name=='pymupdf' or name.startswith('pymupdf.'):
                raise ImportError('legacy package exposes fitz only')
            return original_import(name,*args,**kwargs)
        with legacy_fitz.open(stream=self.fixture('outside',True),filetype='pdf') as document:
            with patch('builtins.__import__',side_effect=legacy_import):
                result=detect_page_filter(document)
        self.assertEqual([item['index'] for item in result['excluded_pages']],[0,4])

import json
from unittest.mock import patch
import test_projection as fixtures
from app.projection_layout import submit_analysis, publish_analysis, projection_queue
BASE=fixtures.BASE

class PageSelectionAPITests(unittest.TestCase):
    setUp=fixtures.ProjectionTests.setUp
    identity=fixtures.ProjectionTests.identity
    upload=fixtures.ProjectionTests.upload
    metadata=fixtures.ProjectionTests.metadata
    tile=fixtures.ProjectionTests.tile
    queue_environment=fixtures.ProjectionTests.queue_environment
    execute_analysis=fixtures.ProjectionTests.execute_analysis
    def post(self,document,path,data):
        return self.client.post(f'{BASE}/{document}/{path}',headers={'Authorization':'x'},data=data)
    def test_upload_summary_keep_pages_source_bytes_and_suggestions(self):
        source=PageFilterTests().fixture('outside',True)
        body=self.upload(source).get_json();doc=body['document_id']
        self.assertEqual(body['page_count'],5)
        self.assertEqual(body['page_filter']['included_page_indexes'],[1,2,3])
        self.assertEqual(self.ledger.source_path(doc).read_bytes(),source)
        summary=self.client.get(f'{BASE}/{doc}',headers={'Authorization':'x'}).get_json()
        self.assertEqual(summary['page_filter'],body['page_filter'])
        # Cover and excluded original blank remain addressable.
        self.assertEqual(self.tile(doc,page_index=0).status_code,200)
        self.assertEqual(self.tile(doc,page_index=4).status_code,200)
    def test_manual_subset_apply_restore_and_old_client_all_pages(self):
        body=self.upload(fixtures.pdf_bytes(3)).get_json();doc=body['document_id']
        r=self.post(doc,'layout',{'method':'manual','rows':1,'columns':2,'included_page_indexes':'[1,2]'})
        self.assertEqual(r.status_code,200,r.get_json())
        self.assertEqual(r.get_json()['layout']['page_slots'],[1,2])
        self.assertEqual(r.get_json()['layout']['excluded_pages'],[{'index':0,'reason':'user','label':'已排除'}])
        r=self.post(doc,'layout',{'method':'manual','rows':1,'columns':3})
        self.assertEqual(r.status_code,200,r.get_json())
        self.assertEqual(r.get_json()['layout']['included_page_indexes'],[0,1,2])
    def test_invalid_selection_rejected_at_both_routes(self):
        doc=self.upload(fixtures.pdf_bytes(3)).get_json()['document_id']
        for value in ('[]','[true]','[0,0]','[3]','["0"]','null','{}','[1.0]'):
            for path in ('layout','layout-analysis'):
                r=self.post(doc,path,{'method':'manual','rows':1,'columns':3,'included_page_indexes':value})
                self.assertIn(r.status_code,(400,422),(path,value,r.status_code))
    def test_selection_switch_has_new_analysis_id_rejects_late_result_and_wrong_apply(self):
        doc=self.upload(fixtures.pdf_bytes(3)).get_json()['document_id']
        environment,redis=self.queue_environment()
        with environment,self.app.app_context():
            meta=self.ledger.load(doc,self.owner)
            first=submit_analysis(self.ledger,doc,self.owner,meta,'current',[0,1])
            first_generation=meta['_generation']
            second=submit_analysis(self.ledger,doc,self.owner,meta,'current',[1,2])
            self.assertNotEqual(first['analysis_id'],second['analysis_id'])
            self.assertEqual(second['included_page_indexes'],[1,2])
            self.assertEqual(projection_queue(redis).job_ids,[second['analysis_id']])
            self.assertFalse(publish_analysis(self.ledger,doc,self.owner,first['analysis_id'],{'status':'ready'},first_generation))
            # Synthetic parallel line pages may be ambiguous; publish a geometry
            # suggestion explicitly to exercise route ownership/binding, not AI.
            self.assertTrue(publish_analysis(self.ledger,doc,self.owner,second['analysis_id'],{'status':'ready','suggestion':{'rows':1,'columns':2,'order':'row','page_slots':[1,2],'confidence':'high'}},first_generation))
            wrong=self.post(doc,'layout',{'method':'auto','analysis_id':second['analysis_id'],'included_page_indexes':'[0,1]'})
            self.assertEqual(wrong.status_code,409)
            right=self.post(doc,'layout',{'method':'auto','analysis_id':second['analysis_id'],'included_page_indexes':'[1,2]'})
            self.assertEqual(right.status_code,200,right.get_json())
            self.assertEqual(right.get_json()['layout']['included_page_indexes'],[1,2])
    def test_failed_analysis_does_not_replace_effective_layout(self):
        doc=self.upload(fixtures.pdf_bytes(3)).get_json()['document_id']
        self.post(doc,'layout',{'method':'manual','rows':1,'columns':2,'included_page_indexes':'[1,2]'})
        environment,redis=self.queue_environment()
        with environment,self.app.app_context():
            meta=self.ledger.load(doc,self.owner)
            analysis=submit_analysis(self.ledger,doc,self.owner,meta,'current',[0,1,2])
            publish_analysis(self.ledger,doc,self.owner,analysis['analysis_id'],{'status':'failed'},meta['_generation'])
        summary=self.client.get(f'{BASE}/{doc}',headers={'Authorization':'x'}).get_json()
        self.assertEqual(summary['layout']['included_page_indexes'],[1,2])
        self.assertEqual(summary['analysis']['included_page_indexes'],[0,1,2])
        self.assertEqual(summary['analysis']['status'],'failed')

    def test_auto_and_manual_apply_cannot_overwrite_concurrent_selection_analysis(self):
        from app.projection_layout import build_layout
        for method,existing in (('auto',True),('manual',True),('manual',False)):
            doc=self.upload(fixtures.pdf_bytes(3)).get_json()['document_id']
            meta=self.ledger.load(doc,self.owner)
            initial={'analysis_id':'analysis-A','status':'ready','included_page_indexes':[0,1],
                     'suggestion':{'rows':1,'columns':2,'order':'row','page_slots':[0,1],'confidence':'high'}}
            if existing:
                meta['analysis']=initial
            self.assertTrue(self.ledger.update(doc,self.owner,meta,meta['_generation']))
            newer=[]
            def switch_selection(*args,**kwargs):
                layout=build_layout(*args,**kwargs)
                current=self.ledger.load(doc,self.owner)
                lease=self.ledger.reserve(self.owner,'analysis',doc,current['_generation'])
                replacement={'analysis_id':'analysis-B','status':'analyzing','included_page_indexes':[1,2],'lease':lease}
                self.assertTrue(self.ledger.replace_analysis(doc,self.owner,replacement,current['_generation'],'analysis-A' if existing else None))
                newer.append(lease)
                return layout
            with patch('app.projection_layout.build_layout',side_effect=switch_selection):
                response=self.post(doc,'layout',{'method':method,'rows':1,'columns':2,'analysis_id':'analysis-A','included_page_indexes':'[0,1]'})
            self.assertEqual(response.status_code,409,response.get_json())
            latest=self.ledger.load(doc,self.owner)
            self.assertEqual(latest['analysis']['analysis_id'],'analysis-B')
            self.assertNotIn('layout',latest)
            self.assertTrue(self.ledger.valid_lease(newer[0]))

class MultipartBudgetTests(unittest.TestCase):
    setUp=fixtures.ProjectionTests.setUp
    identity=fixtures.ProjectionTests.identity
    upload=fixtures.ProjectionTests.upload
    def test_binary_pdf_larger_than_parser_read_chunk_uploads_without_memory_spool(self):
        boundary=b'projection-boundary'
        header=(b'--'+boundary+b'\r\nContent-Disposition: form-data; name="file"; filename="x.pdf"\r\nContent-Type: application/pdf\r\n\r\n')
        pdf=fixtures.pdf_bytes()
        # A CR at the decoder's first 64KiB boundary must be retained until
        # the next read. This reproduces the binary PDF's boundary carryover.
        carry=b'\r\n--projection-'
        source=pdf+b'\n%'+b'x'*(65536-len(header)-len(pdf)-2-len(carry))+carry+b'payload'*10000
        body=header+source+b'\r\n--'+boundary+b'--\r\n'
        response=self.client.post(BASE,headers={'Authorization':'x'},data=body,
                                  content_type='multipart/form-data; boundary=projection-boundary')
        self.assertEqual(response.status_code,201,response.get_json())
        self.assertEqual(self.ledger.source_path(response.get_json()['document_id']).read_bytes(),source)
    def test_existing_field_and_part_budgets_still_reject_oversized_form(self):
        import io
        for extra in ({'field':'x'*65537},{str(i):'x' for i in range(4)}):
            data={'file':(io.BytesIO(fixtures.pdf_bytes()),'x.pdf'),**extra}
            response=self.client.post(BASE,headers={'Authorization':'x'},data=data)
            self.assertEqual(response.status_code,413)
            response.request.environ['wsgi.input'].close()

class PerPageSourceRectangleTests(unittest.TestCase):
    def pdf(self):
        d=fitz.open()
        for row,column,shift in [(1,2,0),(2,1,5),(1,1,0),(2,2,5)]:
            p=d.new_page(width=260,height=360)
            left,top,right,bottom=20,20+shift,240,320+shift
            for a,b in [((left,top),(right,top)),((left,bottom),(right,bottom)),((left,top),(left,bottom)),((right,top),(right,bottom))]:
                p.draw_line(a,b,color=(.2,.6,.8),dashes='[3 3]')
            p.draw_line((0,top+120),(260,top+120))
            p.draw_line((left+90,0),(left+90,360))
            p.insert_text((100,top+280),f'{row} {column}',fontsize=12)
        b=d.tobytes();d.close();return b
    def test_numbered_frames_are_only_a_candidate_until_real_seams_verify(self):
        from app.projection_layout import analyze_source
        result=analyze_source(self.pdf(),'current',None,[0,1,2,3])
        self.assertEqual(result['source'],'numbered_vector_frames')
        self.assertEqual(result['page_slots'],[2,0,1,3])
        self.assertEqual(result['confidence'],'medium')
        self.assertEqual(result['perfect_seams'],4)
        self.assertEqual(len(result['source_rects_mm']),4)
    def test_per_page_source_offsets_render_identically_to_translated_reference_at_all_angles(self):
        from app.projection import render_tile
        from app.projection_child import inspect
        source=self.pdf();pages=inspect(source)['pages'];unit=25.4/72
        rects=[{'page_index':i,'x_mm':20*unit,'y_mm':(20+(5 if i in(1,3) else 0))*unit,
                'width_mm':220*unit,'height_mm':300*unit} for i in range(4)]
        layout=build_layout(pages,2,2,'row',{},[2,0,1,3],included_page_indexes=[0,1,2,3],source_rects_mm=rects)
        # Reference has identical visible coordinates rendered from source clips
        # into normalized pages. No lossy bitmap conversion is involved.
        with fitz.open(stream=source,filetype='pdf') as original:
            reference=fitz.open()
            for i in range(4):
                page=reference.new_page(width=220,height=300)
                shift=5 if i in(1,3) else 0
                page.show_pdf_page(page.rect,original,i,clip=fitz.Rect(20,20+shift,240,320+shift))
            normalized=reference.tobytes()
        plain=build_layout(inspect(normalized)['pages'],2,2,'row',{},[2,0,1,3])
        for angle in (0,90,31):
            values={'x_mm':-30,'y_mm':-30,'width_mm':250,'height_mm':250,'width_px':500,'height_px':500,'rotation_deg':angle}
            actual=fitz.Pixmap(render_tile(source,values,layout=layout));expected=fitz.Pixmap(render_tile(normalized,values,layout=plain))
            if angle in (0,90):
                self.assertEqual(actual.samples,expected.samples,angle)
            else:
                # Check independent geometric control points. PDF form versus
                # direct clips have different AA at oblique edge pixels.
                import math
                cosine,sine=math.cos(math.radians(angle)),math.sin(math.radians(angle))
                scale=500/(250/unit)
                checked=0
                for placement in layout['placements']:
                    for local_x,local_y in [(x,120) for x in (30,60,150,200)]+[(90,y) for y in (40,80,200,250)]:
                        x=placement['x_mm']/unit+local_x;y=placement['y_mm']/unit+local_y
                        px=round((cosine*x-sine*y+30/unit)*scale)
                        py=round((sine*x+cosine*y+30/unit)*scale)
                        if 2<=px<498 and 2<=py<498:
                            checked+=1
                            self.assertTrue(any(max(actual.pixel(px+dx,py+dy))<220
                                                for dx in (-1,0,1) for dy in (-1,0,1)),(angle,px,py))
                self.assertGreaterEqual(checked,8)
    def test_source_rectangles_reject_invalid_or_incomplete_geometry(self):
        pages=[{'index':0,'width_mm':100,'height_mm':200}]
        for rects in ([],[{'page_index':0,'x_mm':-1,'y_mm':0,'width_mm':10,'height_mm':20}],
                      [{'page_index':0,'x_mm':0,'y_mm':0,'width_mm':101,'height_mm':20}],
                      [{'page_index':0,'x_mm':0,'y_mm':0,'width_mm':float('nan'),'height_mm':20}]):
            with self.subTest(rects=rects),self.assertRaises(ValueError):
                build_layout(pages,1,1,'row',{},source_rects_mm=rects)
