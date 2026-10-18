"""Offline semantic tests. Run python -m unittest -v test_projection_scene.py."""
import copy
import hashlib
import json
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock
try:
    from app.projection_scene import SceneUnsupported, export_svg, export_scene, parse_path, transform, write_scene_output
except ModuleNotFoundError:
    from projection_scene import SceneUnsupported, export_svg, export_scene, parse_path, transform, write_scene_output

def svg(body,defs=''):
    return '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100" viewBox="0 0 100 100">'+('<defs>'+defs+'</defs>' if defs else '')+body+'</svg>'
def leaves(n):
    if n['type']=='path':return [n]
    return [p for child in n['children'] for p in leaves(child)]

class Paths(unittest.TestCase):
    def test_relative_implicit_and_close(self):
        self.assertEqual(parse_path('m1 2 3 4h5v-2l-1-1z m2 0l1 1'),[['M',1,2],['L',4,6],['L',9,6],['L',9,4],['L',8,3],['Z'],['M',3,2],['L',4,3]])
    def test_smooth_controls_and_reset(self):
        self.assertEqual(parse_path('M0 0 C1 2 3 4 5 6S7 8 9 10 L1 1S3 4 5 6')[-3:],[['C',7,8,7,8,9,10],['L',1,1],['C',1,1,3,4,5,6]])
        self.assertEqual(parse_path('M1 1q1 2 3 4t5 6')[-2:],[['Q',2,3,4,5],['Q',6,7,9,11]])
    def test_invalid_commands_and_nonfinite(self):
        for text in ['M0 0A1 1 0 0 0 3 3','M0','L0 1','M0 0z1 1','M1e999 0','M0!0']:
            with self.subTest(text=text),self.assertRaises(SceneUnsupported):parse_path(text)
    def test_transform_composition(self):
        self.assertEqual(transform('translate(2 3) scale(4 5)'),[4,0,0,5,2,3])
        r=transform('rotate(90 2 3)');x,y=2*r[0]+3*r[2]+r[4],2*r[1]+3*r[3]+r[5]
        self.assertAlmostEqual(x,2);self.assertAlmostEqual(y,3)
        for value in ['matrix(1 2 3)','translate(0)x','matrix(1,0,0,1,1e999,0)','foo(1)']:
            with self.subTest(value=value),self.assertRaises(SceneUnsupported):transform(value)

class Semantics(unittest.TestCase):
    def test_shared_glyphs_inherit_use_fill_but_keep_own_fill(self):
        page=export_svg(svg('<use xlink:href="#glyph" fill="#ff0000" transform="matrix(10,0,0,10,20,30)"/><use xlink:href="#glyph" fill="#0000ff"/>','<path id="glyph" d="M0 0H1V1Z"/>'))
        a,b=leaves(page['tree']);self.assertEqual(a['path'],b['path']);self.assertEqual(len(page['paths']),1);self.assertEqual(a['fill'],'#ff0000');self.assertEqual(b['fill'],'#0000ff')
        self.assertEqual(page['tree']['children'][0]['transform'],[10,0,0,10,20,30])
        page=export_svg(svg('<use href="#glyph" fill="#ff0000"/>','<path id="glyph" fill="#00ff00" d="M0 0L1 1"/>'));self.assertEqual(leaves(page['tree'])[0]['fill'],'#00ff00')
    def test_group_opacity_not_multiplied_to_children(self):
        page=export_svg(svg('<g opacity=".3"><path d="M0 0L10 10"/><path d="M0 10L10 0"/></g>'));g=page['tree']['children'][0]
        self.assertEqual(g['opacity'],.3);self.assertTrue(all(p['fillOpacity']==1 for p in leaves(g)));self.assertEqual(len(g['children']),2)
    def test_clip_transform_winding_and_dash_stroke(self):
        page=export_svg(svg('<g transform="translate(2 3)" clip-path="url(#c)"><path fill="none" stroke="#123" stroke-width="2" stroke-dasharray="2,3" stroke-dashoffset="1" d="M0 0L10 10"/></g>','<clipPath id="c"><path clip-rule="evenodd" transform="scale(2)" d="M0 0H5V5Z"/></clipPath>'))
        g=page['tree']['children'][0];self.assertEqual(g['transform'],[1,0,0,1,2,3]);self.assertEqual(g['clip'][0]['fillRule'],'evenodd');self.assertEqual(g['clip'][0]['transform'],[2,0,0,2,0,0]);p=leaves(g)[0]
        self.assertEqual((p['fill'],p['stroke'],p['dash'],p['dashOffset']),(None,'#112233',[2,3],1))
    def test_path_opacity_composites_fill_and_stroke_once(self):
        page=export_svg(svg('<path opacity=".5" fill="#fff" stroke="#000" d="M0 0H5V5Z"/>'));g=page['tree']['children'][0]
        self.assertEqual(g['type'],'group');self.assertEqual(g['opacity'],.5);self.assertEqual(g['children'][0]['fillOpacity'],1)
    def test_malformed_and_unimplemented_are_explicit(self):
        bodies=['<image width="1" height="1"/>','<g mask="url(#missing-mask)"/>','<rect width="-2" height="2"/>','<path d="M0 0L1 1" style="fill:red"/>','<g style="mix-blend-mode:unknown"/>','<use href="#missing"/>','<path d="M0 0" stroke="url(#gradient)"/>','<path d="M0 0" vector-effect="non-scaling-stroke"/>','<path d="M0 0" opacity="2"/>','<g filter="url(#x)"/>']
        for body in bodies:
            with self.subTest(body=body),self.assertRaises(SceneUnsupported):export_svg(svg(body))
    def test_cycles_and_clip_union_fail(self):
        with self.assertRaisesRegex(SceneUnsupported,'cyclic'):export_svg(svg('<use href="#a"/>','<g id="a"><use href="#a"/></g>'))
        with self.assertRaisesRegex(SceneUnsupported,'exactly one'):export_svg(svg('<g clip-path="url(#c)"/>','<clipPath id="c"><path d="M0 0H1V1Z"/><path d="M5 5H9V9Z"/></clipPath>'))
    @staticmethod
    def vector_fixture():
        import pymupdf
        document=pymupdf.open()
        for i in range(2):
            page=document.new_page(width=100*72/25.4,height=100*72/25.4)
            page.draw_line((10,10),(200,200),color=(0,0,0),width=.24)
            page.insert_text((20,40),f'page {i}')
        layout={'width_mm':200.,'height_mm':100.,'crop_margins_mm':dict(left=0,top=0,right=0,bottom=0),'placements':[dict(page_index=i,x_mm=i*100.,y_mm=0.,width_mm=100.,height_mm=100.) for i in range(2)]}
        return document.tobytes(),layout
    def test_source_hash_all_layout_pages_and_no_source_mutation(self):
        source,layout=self.vector_fixture();before=hashlib.sha256(source).hexdigest();original=copy.deepcopy(layout);scene=export_scene(source,layout)
        self.assertEqual(scene['source_sha256'],before);self.assertEqual(hashlib.sha256(source).hexdigest(),before)
        self.assertEqual(set(scene['pages']),{'0','1'});self.assertEqual(scene['placements'],layout['placements']);self.assertEqual((scene['width_mm'],scene['height_mm']),(200,100));self.assertEqual(layout,original)
        scene['placements'][0]['x_mm']=123;self.assertNotEqual(scene['placements'],layout['placements'])
    def test_invalid_layout_never_silently_crops_or_duplicates_content(self):
        source,base=self.vector_fixture();cases=[]
        bad=copy.deepcopy(base);bad['placements'].append(copy.deepcopy(bad['placements'][0]));cases.append((bad,'duplicate'))
        bad=copy.deepcopy(base);bad['placements'][1]['x_mm']=1;cases.append((bad,'overlapping'))
        bad=copy.deepcopy(base);bad['placements'][0]['x_mm']=base['width_mm'];cases.append((bad,'outside scene'))
        bad=copy.deepcopy(base);bad['placements'][0]['source_x_mm']=999;cases.append((bad,'crop outside page'))
        bad=copy.deepcopy(base);bad['placements'][0]['y_mm']=float('nan');cases.append((bad,'nonfinite'))
        for bad,reason in cases:
            with self.subTest(reason=reason),self.assertRaisesRegex(SceneUnsupported,reason):export_scene(source,bad)
    def test_image_is_retained_as_original_pixel_asset(self):
        import pymupdf
        document=pymupdf.open();page=document.new_page(width=100,height=100);pix=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,4,4),False);pix.clear_with(80);page.insert_image(pymupdf.Rect(10,10,40,40),stream=pix.tobytes('png'))
        assets={};exported=export_svg(page.get_svg_image(text_as_path=True),assets)
        self.assertTrue(any(a['width']==4 for a in assets.values()))
        def images(node):
            return [node] if node['type']=='image' else [image for child in node.get('children',[]) for image in images(child)]
        self.assertEqual(len(images(exported['tree'])),1)

class CommandLine(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup);self.root=Path(self.temporary.name)
        self.pdf=self.root/'original.pdf';self.layout=self.root/'layout.json'
        source,layout=Semantics.vector_fixture();self.pdf.write_bytes(source);self.layout.write_text(json.dumps(layout));self.original_pdf=source;self.original_layout=self.layout.read_bytes()
        self.module=Path(inspect.getfile(export_scene)).resolve()
        self.environment={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
    def module_cli(self,output):
        return subprocess.run([sys.executable,str(self.module),'--pdf',str(self.pdf),'--layout',str(self.layout),'--output',str(output)],capture_output=True,text=True,env=self.environment)
    def assert_inputs_preserved(self):
        self.assertEqual(self.pdf.read_bytes(),self.original_pdf);self.assertEqual(self.layout.read_bytes(),self.original_layout)
    def test_module_cli_refuses_aliases_and_existing_output(self):
        cases=[self.pdf,self.layout,self.root/'sub'/ '..'/'original.pdf']
        for target,name in [(self.pdf,'pdf-link'),(self.layout,'layout-link')]:
            link=self.root/name;link.symlink_to(target);cases.append(link)
            hard=self.root/(name+'-hard');os.link(target,hard);cases.append(hard)
        existing=self.root/'existing.json';existing.write_bytes(b'prior result');cases.append(existing)
        for output in cases:
            with self.subTest(output=output):
                result=self.module_cli(output);self.assertNotEqual(result.returncode,0);self.assertIn('output',result.stderr);self.assert_inputs_preserved()
        self.assertEqual(existing.read_bytes(),b'prior result')
    def test_module_cli_new_output_then_repeat_preserves_result(self):
        output=self.root/'new.json';first=self.module_cli(output)
        self.assertEqual(first.returncode,0,first.stderr);result=output.read_bytes();scene=json.loads(result);self.assertEqual(scene['source_sha256'],hashlib.sha256(self.original_pdf).hexdigest());self.assertEqual(set(scene['pages']),{'0','1'})
        repeated=self.module_cli(output);self.assertNotEqual(repeated.returncode,0);self.assertEqual(output.read_bytes(),result);self.assert_inputs_preserved()
    def test_output_created_after_preflight_is_not_truncated(self):
        output=self.root/'raced.json';real_open=Path.open
        def raced_open(path,mode='r',*args,**kwargs):
            if path==output and mode=='x':
                with real_open(path,'wb') as prior:prior.write(b'concurrent result')
            return real_open(path,mode,*args,**kwargs)
        with mock.patch.object(Path,'open',raced_open):
            with self.assertRaises(FileExistsError):write_scene_output({'version':1},output,[self.pdf,self.layout])
        self.assertEqual(output.read_bytes(),b'concurrent result');self.assert_inputs_preserved()
    def stage_prototype(self,confidence='high'):
        # Portable orchestration test with explicit dependency stubs. Real A/B
        # evidence separately exercises actual existing analyzer functions.
        stage=self.root/'checkout';app=stage/'app';scripts=stage/'scripts';app.mkdir(parents=True);scripts.mkdir()
        (app/'__init__.py').write_text('');shutil.copyfile(self.module,app/'projection_scene.py')
        candidate=self.module.parent.parent/'scripts'/'prototype_projection_scene.py'
        if not candidate.is_file():candidate=self.module.with_name('prototype_projection_scene.py')
        self.assertTrue(candidate.is_file());shutil.copyfile(candidate,scripts/'prototype_projection_scene.py')
        (app/'projection_child.py').write_text('''import pymupdf

def inspect(source):
    with pymupdf.open(stream=source,filetype='pdf') as document:
        pages=[dict(index=i,width_mm=p.rect.width*25.4/72,height_mm=p.rect.height*25.4/72) for i,p in enumerate(document)]
    return dict(pages=pages,page_count=len(pages),embedded_layout=None,page_filter=dict(included_page_indexes=list(range(len(pages))),excluded_pages=[]))
''')
        (app/'projection_layout.py').write_text('''import socket

def analyze_source(source,mode,embedded,indexes):
    assert mode=='current' and embedded is None and indexes==[0,1]
    try:socket.getaddrinfo('example.invalid',443)
    except RuntimeError as error:assert 'prohibits network' in str(error)
    else:raise AssertionError('network guard absent')
    return dict(confidence=CONFIDENCE,rows=1,columns=2,order='row',page_slots=[0,1],source='test-only-stub')

def build_layout(pages,rows,columns,order,crop,slots,source,indexes,rects):
    assert rows==1 and columns==2 and order=='row' and slots==indexes==[0,1] and rects is None
    width=pages[0]['width_mm'];height=pages[0]['height_mm']
    return dict(width_mm=width*2,height_mm=height,crop_margins_mm=dict(left=0,right=0,top=0,bottom=0),source=source,placements=[dict(page_index=i,x_mm=i*width,y_mm=0,width_mm=width,height_mm=height) for i in indexes])
'''.replace('CONFIDENCE',repr(confidence)))
        return scripts/'prototype_projection_scene.py'
    def test_prototype_delegates_pipeline_blocks_network_and_preserves_source(self):
        script=self.stage_prototype();output=self.root/'prototype.json'
        first=subprocess.run([sys.executable,str(script),'--pdf',str(self.pdf),'--output',str(output)],capture_output=True,text=True,env=self.environment)
        self.assertEqual(first.returncode,0,first.stderr);stats=json.loads(first.stdout);self.assertEqual(stats['network'],'blocked');self.assertEqual(stats['included_pages'],2);self.assertTrue(stats['source_unchanged']);self.assertIn('inspect_seconds',stats);self.assertIn('analyze_seconds',stats);self.assertIn('scene_seconds',stats);self.assert_inputs_preserved()
        previous=output.read_bytes();again=subprocess.run([sys.executable,str(script),'--pdf',str(self.pdf),'--output',str(output)],capture_output=True,text=True,env=self.environment)
        self.assertNotEqual(again.returncode,0);self.assertEqual(output.read_bytes(),previous)
        alias=subprocess.run([sys.executable,str(script),'--pdf',str(self.pdf),'--output',str(self.pdf)],capture_output=True,text=True,env=self.environment);self.assertNotEqual(alias.returncode,0);self.assertIn('aliases input',alias.stderr);self.assert_inputs_preserved()
    def test_prototype_low_confidence_has_no_output(self):
        script=self.stage_prototype(confidence='low');output=self.root/'unsupported.json';result=subprocess.run([sys.executable,str(script),'--pdf',str(self.pdf),'--output',str(output)],capture_output=True,text=True,env=self.environment)
        self.assertNotEqual(result.returncode,0);self.assertIn('accepted layout',result.stderr);self.assertFalse(output.exists());self.assert_inputs_preserved()

if __name__=='__main__':unittest.main()
