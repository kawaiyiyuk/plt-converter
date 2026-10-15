import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
import test_projection as fixtures

MM_PT = 72 / 25.4


def trig(angle):
    cardinal = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}
    return cardinal.get(angle % 360) or (math.cos(math.radians(angle)), math.sin(math.radians(angle)))


def viewport(width_mm, height_mm, angle, ppmm=2):
    c, s = trig(angle)
    points = [(c*x-s*y, s*x+c*y) for x, y in ((0, 0), (width_mm, 0), (0, height_mm), (width_mm, height_mm))]
    x, y = min(p[0] for p in points)-10, min(p[1] for p in points)-10
    width_px = math.ceil((max(p[0] for p in points)-x+10)*ppmm)
    height_px = math.ceil((max(p[1] for p in points)-y+10)*ppmm)
    return {'x_mm': x, 'y_mm': y, 'width_mm': width_px/ppmm, 'height_mm': height_px/ppmm,
            'width_px': width_px, 'height_px': height_px, 'rotation_deg': angle}


def centroid(pixmap, color):
    pixels = pixmap.samples
    coords = []
    for y in range(pixmap.height):
        for x in range(pixmap.width):
            r,g,b = pixels[(y*pixmap.width+x)*3:(y*pixmap.width+x)*3+3]
            if ((color == 'black' and max(r,g,b) < 60)
                or (color == 'red' and r > 200 and max(g,b) < 60)
                or (color == 'blue' and b > 200 and max(r,g) < 60)):
                coords.append((x+.5,y+.5))
    if not coords:
        raise AssertionError(f'{color} marker absent')
    return tuple(sum(p[i] for p in coords)/len(coords) for i in (0,1))


def marker_source():
    doc = fitz.open(); page = doc.new_page(width=130*MM_PT,height=130*MM_PT)
    page.draw_circle((10*MM_PT,10*MM_PT),1.5*MM_PT,color=None,fill=(0,0,0))
    page.draw_circle((110*MM_PT,10*MM_PT),1.5*MM_PT,color=None,fill=(1,0,0))
    result = doc.tobytes();doc.close();return result


class ProjectionRotationTests(unittest.TestCase):
    setUp = fixtures.ProjectionTests.setUp
    metadata = fixtures.ProjectionTests.metadata
    identity = fixtures.ProjectionTests.identity
    upload = fixtures.ProjectionTests.upload
    tile = fixtures.ProjectionTests.tile

    def test_rotation_37_moves_markers_without_changing_100mm_distance(self):
        body = self.upload(marker_source()).get_json()
        values = viewport(130,130,37)
        response = self.tile(body['document_id'], **values)
        self.assertEqual(response.status_code,200)
        pixmap = fitz.Pixmap(response.data)
        black,red = centroid(pixmap,'black'),centroid(pixmap,'red')
        c,s = trig(37)
        for actual,(px,py) in ((black,(10,10)),(red,(110,10))):
            expected = (2*(c*px-s*py-values['x_mm']),2*(s*px+c*py-values['y_mm']))
            self.assertLessEqual(math.dist(actual,expected),1)
        self.assertLessEqual(abs(math.dist(black,red)-200),1)

    def test_zero_and_full_turn_png_bytes_match_legacy_direct_render(self):
        source=fixtures.pdf_bytes();body=self.upload(source).get_json()
        default=self.tile(body['document_id'])
        with fitz.open(stream=source,filetype='pdf') as doc:
            expected=doc[0].get_pixmap(colorspace=fitz.csRGB,alpha=False).tobytes('png')
        self.assertEqual(default.data,expected)
        for angle in (0,360,-360,720):
            response=self.tile(body['document_id'],rotation_deg=angle)
            self.assertEqual(response.data,default.data)
            self.assertEqual(response.headers['X-Projection-Rotation'],'0')
        self.assertTrue(body['rotation_supported'])
        response=self.client.post(f"{fixtures.BASE}/{body['document_id']}/renew",headers={'Authorization':'x'})
        self.assertTrue(response.get_json()['rotation_supported'])
        meta=self.metadata(body['document_id'])
        self.assertNotIn('rotation_supported',meta)

    def test_query_rotation_integer_normalization_and_guards(self):
        doc_id=self.upload(marker_source()).get_json()['document_id']
        for invalid in ('1.5','nan','Infinity','-Infinity','1.00000000000000000001','1e309',''):
            self.assertEqual(self.tile(doc_id,rotation_deg=invalid).status_code,400)
        for angle in (-1,359,10**250+137):
            normalized=angle%360
            values=viewport(130,130,normalized)
            response=self.tile(doc_id,**{**values,'rotation_deg':str(angle)})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['X-Projection-Rotation'],str(normalized))
        self.assertEqual(self.tile(doc_id,rotation_deg=37,width_px=2049).status_code,400)
        self.owner='user:2'
        self.assertEqual(self.tile(doc_id,rotation_deg=37).status_code,404)
        self.assertEqual(self.client.get(f'{fixtures.BASE}/{doc_id}/tile').status_code,401)

    def test_all_selected_angles_preserve_marker_coordinates_and_100mm_length(self):
        source=marker_source();doc_id=self.upload(source).get_json()['document_id']
        for angle in (1,90,137,180,270,359):
            with self.subTest(angle=angle):
                values=viewport(130,130,angle)
                response=self.tile(doc_id,**values)
                self.assertEqual(response.status_code,200)
                pix=fitz.Pixmap(response.data)
                black,red=centroid(pix,'black'),centroid(pix,'red')
                c,s=trig(angle)
                for actual,(x,y) in ((black,(10,10)),(red,(110,10))):
                    expected=(2*(c*x-s*y-values['x_mm']),2*(s*x+c*y-values['y_mm']))
                    self.assertLessEqual(math.dist(actual,expected),1)
                self.assertLessEqual(abs(math.dist(black,red)-200),1)

    def test_adjacent_rotated_white_page_triangles_do_not_erase_prior_marker(self):
        doc=fitz.open()
        for index in range(2):
            page=doc.new_page(width=100*MM_PT,height=100*MM_PT)
            page.draw_rect(page.rect,color=None,fill=(1,1,1))
            page.draw_circle(((95 if index==0 else 5)*MM_PT,20*MM_PT),MM_PT,color=None,fill=(1,0,0) if index==0 else (0,0,1))
        source=doc.tobytes();doc.close()
        doc_id=self.upload(source).get_json()['document_id']
        result=self.client.post(f'{fixtures.BASE}/{doc_id}/layout',headers={'Authorization':'x'},data={'method':'manual','rows':1,'columns':2,'order':'row'})
        self.assertEqual(result.status_code,200)
        values=viewport(200,100,37)
        response=self.tile(doc_id,view='layout',**values)
        self.assertEqual(response.status_code,200)
        pix=fitz.Pixmap(response.data)
        c,s=trig(37)
        for color,(x,y) in (('red',(95,20)),('blue',(105,20))):
            expected=(2*(c*x-s*y-values['x_mm']),2*(s*x+c*y-values['y_mm']))
            self.assertLessEqual(math.dist(centroid(pix,color),expected),1)

    def test_mixed_crop_blank_intrinsic_rotate_image_annotation_and_meta_unchanged(self):
        doc=fitz.open();page=doc.new_page(width=100*MM_PT,height=100*MM_PT)
        page.draw_circle((10*MM_PT,10*MM_PT),MM_PT,color=None,fill=(1,0,0))
        page=doc.new_page(width=120*MM_PT,height=80*MM_PT)
        image=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,16,16),False)
        image.set_rect(image.irect,(0,255,0));page.insert_image(fitz.Rect(30*MM_PT,30*MM_PT,50*MM_PT,50*MM_PT),pixmap=image)
        page.insert_text((60*MM_PT,30*MM_PT),'Native text',fontsize=12)
        annotation=page.add_line_annot((20*MM_PT,20*MM_PT),(30*MM_PT,20*MM_PT))
        annotation.set_colors(stroke=(0,0,1));annotation.set_border(width=2*MM_PT);annotation.update()
        page.set_rotation(90);source=doc.tobytes();doc.close()
        body=self.upload(source).get_json();doc_id=body['document_id']
        response=self.client.post(f'{fixtures.BASE}/{doc_id}/layout',headers={'Authorization':'x'},data={'method':'manual','rows':2,'columns':2,'order':'row','crop_left_mm':5,'crop_right_mm':5,'crop_top_mm':5,'crop_bottom_mm':5})
        layout=response.get_json()['layout'];self.assertEqual(layout['page_slots'],[0,1,None,None])
        root=Path(self.temp.name)/'documents';meta_before=json.dumps(self.metadata(doc_id)).encode()
        values=viewport(layout['width_mm'],layout['height_mm'],137)
        rotated=self.tile(doc_id,view='layout',**values)
        self.assertEqual(rotated.status_code,200)
        pix=fitz.Pixmap(rotated.data);c,s=trig(137)
        # The source image center (40,40) becomes (40,40) after intrinsic 90°,
        # then the crop/slot map places its center at world (125,35).
        q=(2*(c*125-s*35-values['x_mm']),2*(s*125+c*35-values['y_mm']))
        color=pix.pixel(round(q[0]),round(q[1]));self.assertGreater(color[1],220);self.assertLess(max(color[0],color[2]),30)
        blue_expected=(2*(c*145-s*20-values['x_mm']),2*(s*145+c*20-values['y_mm']))
        self.assertLessEqual(math.dist(centroid(pix,'blue'),blue_expected),1)
        self.assertEqual(json.dumps(self.metadata(doc_id)).encode(),meta_before)
        self.assertEqual((root/f'{doc_id}.pdf').read_bytes(),source)
        # Intrinsic 90° source page is also independently viewable at a new angle.
        original=self.tile(doc_id,page_index=1,rotation_deg=37,**{k:v for k,v in viewport(80,120,37).items() if k!='rotation_deg'})
        self.assertEqual(original.status_code,200)

    def test_block_output_matches_full_native_reference_without_seams(self):
        from app.projection import render_tile,_source_over_block
        doc=fitz.open();page=doc.new_page(width=300,height=400)
        page.draw_rect(page.rect,color=None,fill=(1,1,1))
        for i in range(12):
            page.draw_line((0,15+i*30),(300,35+i*30),color=(0.2,0.5,0.8),width=1.1)
        page.insert_text((50,160),'Block seam test',fontsize=18)
        source=doc.tobytes();values=viewport(300/MM_PT,400/MM_PT,37,ppmm=3)
        actual=fitz.Pixmap(render_tile(source,values))
        c,s=trig(37);scale=3/MM_PT
        matrix=fitz.Matrix(c*scale,s*scale,-s*scale,c*scale,-values['x_mm']*3,-values['y_mm']*3)
        reference=page.get_pixmap(matrix=matrix,colorspace=fitz.csRGB,alpha=True)
        canvas=bytearray(b'\xff')*(values['width_px']*values['height_px']*3)
        _source_over_block(canvas,values['width_px'],reference,(0,0,values['width_px'],values['height_px']))
        actual_samples = actual.samples
        # MuPDF independently rasterizes each clipped block. At nonzero angles
        # its edge AA quantization can differ slightly from one full-page pass;
        # this is a reference tolerance, not a product rendering adjustment.
        self.assertLessEqual(max(abs(a-b) for a,b in zip(actual_samples,canvas)),6)
        for offset in range(0,len(canvas),3):
            reference_pixel = canvas[offset:offset+3]
            actual_pixel = actual_samples[offset:offset+3]
            if min(reference_pixel) < 240:
                self.assertLess(min(actual_pixel),250, 'block clipping must not erase visible ink')
            if tuple(reference_pixel) in ((0,0,0),(51,127,204)):
                self.assertEqual(actual_pixel,reference_pixel, 'solid stroke cores must stay exact')
        # Row extents independently check geometry across the 256px block grid.
        for y in range(actual.height):
            ref=[x for x in range(actual.width) if min(canvas[(y*actual.width+x)*3:(y*actual.width+x)*3+3]) < 128]
            got=[x for x in range(actual.width) if min(actual_samples[(y*actual.width+x)*3:(y*actual.width+x)*3+3]) < 128]
            if ref:
                self.assertTrue(got, 'no complete ink row may disappear')
                self.assertLessEqual(abs(ref[0]-got[0]),1)
                self.assertLessEqual(abs(ref[-1]-got[-1]),1)
        doc.close()

    def test_premultiplied_half_alpha_and_memory_budget_displaylist_cache(self):
        from app.projection import render_tile,_source_over_block
        doc=fitz.open();page=doc.new_page(width=1200,height=1200)
        page.draw_rect(page.rect,color=None,fill=(1,0,0),fill_opacity=.5)
        source=doc.tobytes();pix=page.get_displaylist().get_pixmap(colorspace=fitz.csRGB,alpha=True)
        self.assertEqual(pix.pixel(20,20),(126,0,0,126))
        tiny=bytearray((255,255,255))*1
        _source_over_block(tiny,1,pix,(0,0,1,1));self.assertEqual(tuple(tiny),(255,129,129))
        observed=[];original_get=fitz.DisplayList.get_pixmap;original_list=fitz.Page.get_displaylist
        lists=[]
        def inspect_get(instance,*args,**kwargs):
            result=original_get(instance,*args,**kwargs);observed.append((result.width,result.height));return result
        def inspect_list(instance,*args,**kwargs):
            lists.append(instance.number);return original_list(instance,*args,**kwargs)
        values={'x_mm':-50,'y_mm':-50,'width_mm':1000,'height_mm':1000,'width_px':2000,'height_px':2000,'rotation_deg':37}
        with patch.object(fitz.DisplayList,'get_pixmap',inspect_get),patch.object(fitz.Page,'get_displaylist',inspect_list):
            png=render_tile(source,values)
        self.assertEqual((fitz.Pixmap(png).width,fitz.Pixmap(png).height),(2000,2000))
        self.assertEqual(lists,[0]);self.assertTrue(observed)
        self.assertLessEqual(max(max(size) for size in observed),524)
        doc.close()

    def test_rotated_numeric_overflow_and_off_page_white(self):
        doc_id=self.upload(marker_source()).get_json()['document_id']
        response=self.tile(doc_id,rotation_deg=37,x_mm=1e38,y_mm=1e38)
        self.assertEqual(response.status_code,400)
        response=self.tile(doc_id,rotation_deg=37,x_mm=-5000,y_mm=-5000)
        self.assertEqual(response.status_code,200)
        self.assertEqual(set(fitz.Pixmap(response.data).samples),{255})
