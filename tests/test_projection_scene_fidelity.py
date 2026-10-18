"""Real MuPDF source operators, original PNG pixels and deterministic bundle tests."""
import base64,hashlib,json,math,tempfile,unittest,zipfile
from pathlib import Path
import pymupdf
try:from app.projection_scene import export_scene,export_scene_bundle,export_svg,SceneUnsupported
except ModuleNotFoundError:from projection_scene import export_scene,export_scene_bundle,export_svg,SceneUnsupported

def layout():return dict(width_mm=100*25.4/72,height_mm=100*25.4/72,crop_margins_mm=dict(left=0,top=0,right=0,bottom=0),placements=[dict(page_index=0,x_mm=0,y_mm=0,width_mm=100*25.4/72,height_mm=100*25.4/72)])
def nodes(n):
 yield n
 for child in n.get('children',[]):yield from nodes(child)
 if n.get('mask'):yield from nodes(n['mask']['tree'])
def image_pdf(width=4,height=4,interpolate=False):
 d=pymupdf.open();p=d.new_page(width=100,height=100);pix=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,width,height),True)
 pix.clear_with(100)
 if width==4:
  for y in range(height):
   for x in range(width):pix.set_pixel(x,y,(min(255,x*85),0,0,x*85))
 xref=p.insert_image(pymupdf.Rect(10,10,90,90),stream=pix.tobytes('png'))
 if interpolate:
  d.xref_set_key(xref,'Interpolate','true');smask=d.xref_get_key(xref,'SMask')[1]
  if smask.endswith(' R'):d.xref_set_key(int(smask.split()[0]),'Interpolate','true')
 return d.tobytes(),pix

def raw_pdf(resources,content):
 d=pymupdf.open();p=d.new_page(width=100,height=100);res=d.get_new_xref();d.update_object(res,resources);d.xref_set_key(p.xref,'Resources',f'{res} 0 R');c=d.get_new_xref();d.update_object(c,'<<>>');d.update_stream(c,content.encode());d.xref_set_key(p.xref,'Contents',f'{c} 0 R');return d.tobytes()

def hairline_pdf(kind='repeat'):
 resources='<<>>'
 if kind=='repeat':content='0 G 0 w 10 50 m 90 50 l S 1 w 10 50 m 90 50 l S 0 w 10 50 m 90 50 l S'
 elif kind=='dash':content='0 G [3 2] 1 d 0 w 10 50 m 90 50 l S 1 w 10 50 m 90 50 l S'
 elif kind=='clip':content='10 10 80 80 re W n 0 G 0 w 0 50 m 100 50 l S 1 w 0 70 m 100 70 l S'
 elif kind=='transform':content='q 2 0 0 3 0 0 cm 0 w 0 G 10 10 m 40 20 l S Q 1 w 10 10 m 40 20 l S'
 else:
  resources='<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>'
  if kind=='textclip':content='BT /F1 60 Tf 7 Tr 1 0 0 1 20 20 Tm (A) Tj ET 0 w 0 G 0 50 m 100 50 l S'
  else:content=f'BT /F1 60 Tf {1 if kind!="filltext" else 0} Tr {0 if kind!="normaltext" else 1} w 1 0 0 1 20 20 Tm (A) Tj ET'
 return raw_pdf(resources,content)

def hairline_mask_pdf():
 d=pymupdf.open(stream=softmask_pdf('Luminosity'),filetype='pdf')
 for xref in range(1,d.xref_length()):
  if d.xref_is_stream(xref):
   stream=d.xref_stream(xref)
   if b'0.2 g' in stream:d.update_stream(xref,b'0 g 0 0 100 100 re f 1 G 0 w 10 50 m 90 50 l S')
   elif b'/GS1 gs' in stream:d.update_stream(xref,b'/GS1 gs 0 G 1 w 10 50 m 90 50 l S')
 return d.tobytes()

def knockout_pdf(clip=False,complex=False):
 d=pymupdf.open();p=d.new_page(width=100,height=100)
 gs='<< /GS1 << /ca .4 /CA .7 >> >>';form=d.get_new_xref();d.update_object(form,'<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Group << /S /Transparency /CS /DeviceRGB /I true /K true >> /Resources << /ExtGState '+gs+' >> >>')
 paints='/GS1 gs 1 0 0 rg 0 0 1 RG 8 w 10 10 80 80 re f 10 10 80 80 re S'
 if complex:paints='/GS1 gs 1 0 0 rg 10 10 60 60 re f 0 0 1 rg 30 30 60 60 re f'
 d.update_stream(form,paints.encode());resources=d.get_new_xref();d.update_object(resources,f'<< /ExtGState {gs} /XObject << /FX {form} 0 R >> >>');d.xref_set_key(p.xref,'Resources',f'{resources} 0 R')
 content=d.get_new_xref();d.update_object(content,'<<>>');body='q '+('.8 0 0 .8 5 5 cm 0 0 100 100 re W n ' if clip else '')+'q '+paints+' Q /FX Do Q';d.update_stream(content,body.encode());d.xref_set_key(p.xref,'Contents',f'{content} 0 R');return d.tobytes()

def annotation_pdf(kind):
 d=pymupdf.open();p=d.new_page(width=100,height=100)
 if kind=='Rect':
  a=p.add_rect_annot(pymupdf.Rect(10,10,90,90));a.set_colors(stroke=(0,0,1),fill=(1,0,0));a.set_border(width=4)
 else:
  a=p.add_freetext_annot(pymupdf.Rect(10,10,90,90),'TEXT',fontsize=12,text_color=(0,0,1),fill_color=(1,1,0));a.set_border(width=2)
 a.set_opacity(.55);a.update();return d.tobytes()

def isolated_blend_pdf(isolated=True,blend=True,page_group=False,alpha=1):
 d=pymupdf.open();p=d.new_page(width=100,height=100)
 if page_group:d.xref_set_key(p.xref,'Group','<< /S /Transparency /CS /DeviceRGB /I true >>')
 form=d.get_new_xref();d.update_object(form,'<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Group << /S /Transparency /CS /DeviceRGB /I '+str(isolated).lower()+' >> /Resources << /ExtGState << /M << /BM /Multiply >> >> >> >>')
 d.update_stream(form,(('/M gs ' if blend else '')+'1 0 0 rg 10 10 80 80 re f').encode())
 res=d.get_new_xref();d.update_object(res,f'<< /ExtGState << /A << /ca {alpha} /CA {alpha} >> >> /XObject << /F {form} 0 R >> >>');d.xref_set_key(p.xref,'Resources',f'{res} 0 R')
 content=d.get_new_xref();d.update_object(content,'<<>>');d.update_stream(content,b'0 0 1 rg 0 0 100 100 re f /A gs /F Do');d.xref_set_key(p.xref,'Contents',f'{content} 0 R')
 return d.tobytes()

def annotation_blend_pdf(kind='Stamp',isolated=True,blend=True,empty=False):
 d=pymupdf.open();p=d.new_page(width=100,height=100)
 if not empty:p.draw_rect(p.rect,color=None,fill=(0,0,1))
 if kind=='Stamp':a=p.add_stamp_annot(p.rect)
 elif kind=='Widget':
  widget=pymupdf.Widget();widget.field_name='scene-appearance';widget.field_type=pymupdf.PDF_WIDGET_TYPE_TEXT;widget.field_value='A';widget.rect=p.rect;a=p.add_widget(widget)
 else:a=p.add_freetext_annot(p.rect,'A')
 form=d.get_new_xref();d.update_object(form,'<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Group << /S /Transparency /CS /DeviceRGB /I '+str(isolated).lower()+' >> /Resources << /ExtGState << /M << /BM /Multiply >> >> >> >>');d.update_stream(form,(('/M gs ' if blend else '')+'1 0 0 rg 10 10 80 80 re f').encode())
 d.xref_set_key(a.xref,'AP',f'<< /N {form} 0 R >>');d.xref_set_key(a.xref,'Rect','[0 0 100 100]')
 return d.tobytes()

def colored_mask_pdf():
 d=pymupdf.open(stream=softmask_pdf('Luminosity'),filetype='pdf')
 for xref in range(1,d.xref_length()):
  if d.xref_is_stream(xref) and b'0.2 g' in d.xref_stream(xref):d.update_stream(xref,b'1 0 0 rg 0 0 100 100 re f')
 return d.tobytes()

class Fidelity(unittest.TestCase):
 def test_hairlines_match_source_stroke_occurrences_without_changing_normal_strokes(self):
  for kind,flags in [('repeat',[True,False,True]),('dash',[True,False]),('clip',[True,False]),('transform',[True,False])]:
   scene=export_scene(hairline_pdf(kind),layout());strokes=[n for n in nodes(scene['pages']['0']['tree']) if n['type']=='path' and n['stroke']]
   with self.subTest(kind=kind):
    self.assertEqual([n.get('hairline',False) for n in strokes],flags)
    self.assertTrue(all(n['strokeWidth']>0 for n in strokes))
    if kind=='dash':self.assertEqual([n['dash'] for n in strokes],[[3,2],[3,2]])
 def test_hairline_text_is_explicit_but_ordinary_text_and_existing_textclip_behavior_stay(self):
  with self.assertRaisesRegex(SceneUnsupported,'hairline.*text'):export_scene(hairline_pdf('stroketext'),layout())
  for kind in ['normaltext','filltext']:self.assertTrue(export_scene(hairline_pdf(kind),layout())['pages']['0']['paths'])
  # Text clipping already has an explicit unsupported mask-bounds result.
  with self.assertRaisesRegex(SceneUnsupported,'mask: unsupported bounds or units'):export_scene(hairline_pdf('textclip'),layout())
 def test_hairline_and_normal_stroke_same_geometry_in_mask_keep_source_paint_order(self):
  scene=export_scene(hairline_mask_pdf(),layout());all_nodes=list(nodes(scene['pages']['0']['tree']));mask=next(n['mask']['tree'] for n in all_nodes if n.get('mask'))
  mask_strokes=[n for n in nodes(mask) if n['type']=='path' and n['stroke']]
  self.assertEqual([n.get('hairline',False) for n in mask_strokes],[True])
  self.assertEqual([n.get('hairline',False) for n in all_nodes if n['type']=='path' and n['stroke']],[False,True])
 def test_hairline_ambiguous_svg_stroke_count_is_explicit_not_an_ordinal_guess(self):
  from unittest.mock import patch
  import importlib
  module=importlib.import_module(export_scene.__module__)
  original=module.export_svg
  def duplicate_stroke(*args,**kwargs):
   page=original(*args,**kwargs);stroke=next(n for n in nodes(page['tree']) if n['type']=='path' and n['stroke']);page['tree']['children'].append(dict(stroke));return page
  with patch.object(module,'export_svg',side_effect=duplicate_stroke),self.assertRaisesRegex(SceneUnsupported,'hairline strokes could not be mapped unambiguously'):
   export_scene(raw_pdf('<<>>','0 G 0 w 10 50 m 90 50 l S'),layout())
 def test_real_pdf_original_and_pyramid_gutters_retain_rgb_and_alpha_samples(self):
  import xml.etree.ElementTree as ET
  w,h=4100,5;pix=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,w,h),True)
  for y in range(h):
   for x in range(w):
    a=64+(x+y)%192;pix.set_pixel(x,y,((x*7)%a,(x*13+y)%a,(x+y*19)%a,a))
  d=pymupdf.open();p=d.new_page(width=100,height=100);p.insert_image(p.rect,stream=pix.tobytes('png'))
  # Compare to the real source PNG operands, including the separate alpha
  # mask; splitting must not change either channel or any gutter sample.
  originals={}
  for e in ET.fromstring(p.get_svg_image()).iter():
   if e.tag.endswith('}image'):
    source=pymupdf.Pixmap(base64.b64decode(e.attrib['{http://www.w3.org/1999/xlink}href'].split(',')[1]));originals[source.n]=source
  scene=export_scene(d.tobytes(),layout());checked=[]
  def check(group,expected):
   if group['type']!='group':return
   if group['children'] and all(n['type']=='image' and n.get('sourceRect') for n in group['children']):
    aid=group['children'][0]['assetId'];kind=pymupdf.Pixmap(base64.b64decode(scene['assets'][aid]['data'].split(',')[1])).n
    if expected.n!=kind:expected=originals[kind]
    logical_w=max(n['transform'][4]+n['width'] for n in group['children']);logical_h=max(n['transform'][5]+n['height'] for n in group['children'])
    for n in group['children']:
     sx,sy,cw,ch=n['sourceRect'];a=scene['assets'][n['assetId']];tile=pymupdf.Pixmap(base64.b64decode(a['data'].split(',')[1]));x=round(n['transform'][4]*expected.width/logical_w);y=round(n['transform'][5]*expected.height/logical_h)
     self.assertLessEqual(max(tile.width,tile.height),2048)
     self.assertEqual((sx,sy),(min(2,x),min(2,y)))
     for tx,ty in [(0,0),(tile.width-1,0),(0,tile.height-1),(tile.width-1,tile.height-1),(sx,sy),(sx+cw-1,sy+ch-1)]:self.assertEqual(tile.pixel(tx,ty),expected.pixel(x-sx+tx,y-sy+ty))
    checked.append(expected.n)
   for child in group['children']:check(child,expected)
   if group.get('mask'):check(group['mask']['tree'],expected)
  for group in nodes(scene['pages']['0']['tree']):
   if group.get('imageSource'):
    aid=next(n['assetId'] for n in nodes(group) if n['type']=='image');sample=pymupdf.Pixmap(base64.b64decode(scene['assets'][aid]['data'].split(',')[1]));source=originals[sample.n]
    check(group,source)
    for level in ([] if group.get('imageTransform') else group.get('imageLevels',[])):
     factor=level['sampleScale'];check(level['tree'],pymupdf.Pixmap(source,math.ceil(w/factor),math.ceil(h/factor)))
  self.assertEqual(sorted(checked),[1,3])
  # Opaque source levels have no joint RGB/softmask filtering. Check their
  # real emitted level PNG partitions against the independent full resize.
  d=pymupdf.open();p=d.new_page(width=100,height=100);rgb=pymupdf.Pixmap(pix,0);p.insert_image(p.rect,stream=rgb.tobytes('png'));scene=export_scene(d.tobytes(),layout());checked=[]
  for group in nodes(scene['pages']['0']['tree']):
   if group.get('imageSource'):
    check(group,rgb)
    for level in group.get('imageLevels',[]):
     factor=level['sampleScale'];check(level['tree'],pymupdf.Pixmap(rgb,math.ceil(w/factor),math.ceil(h/factor)))
  self.assertEqual(checked,[3,3])
 def test_isolated_form_blend_is_not_silently_exported_with_nonisolated_semantics(self):
  isolated=isolated_blend_pdf();nonisolated=isolated_blend_pdf(False)
  with pymupdf.open(stream=isolated,filetype='pdf') as d:self.assertEqual(d[0].get_pixmap().pixel(50,50),(255,0,0))
  with pymupdf.open(stream=nonisolated,filetype='pdf') as d:self.assertEqual(d[0].get_pixmap().pixel(50,50),(0,0,0))
  with self.assertRaisesRegex(SceneUnsupported,'backdrop-dependent blend'):export_scene(isolated,layout())
  self.assertTrue(export_scene(nonisolated,layout())['pages']['0']['paths'])
 def test_plain_isolated_forms_and_explicit_page_group_remain_supported(self):
  for page_group in [False,True]:
   self.assertTrue(export_scene(isolated_blend_pdf(blend=False,page_group=page_group),layout())['pages']['0']['paths'])
  # Root/page isolation is the page's transparent backdrop, not a nested Form.
  source=raw_pdf('<< /ExtGState << /M << /BM /Multiply >> >> >>','0 0 1 rg 0 0 100 100 re f /M gs 1 0 0 rg 10 10 80 80 re f')
  with pymupdf.open(stream=source,filetype='pdf') as d:
   d.xref_set_key(d[0].xref,'Group','<< /S /Transparency /CS /DeviceRGB /I true >>');source=d.tobytes()
  self.assertTrue(export_scene(source,layout())['pages']['0']['paths'])
 def test_form_isolation_opacity_and_child_blend_matrix_never_silently_changes_backdrop(self):
  for isolated in [False,True]:
   for alpha in [1,.5]:
    for blend in [False,True]:
     with self.subTest(isolated=isolated,alpha=alpha,blend=blend):
      source=isolated_blend_pdf(isolated=isolated,alpha=alpha,blend=blend)
      with pymupdf.open(stream=source,filetype='pdf') as d:reference=d[0].get_pixmap().pixel(50,50)
      expected=(0,0,255*(1-alpha)) if blend and not isolated else (255*alpha,0,255*(1-alpha))
      self.assertLessEqual(max(abs(a-b) for a,b in zip(reference,expected)),2)
      if blend and (isolated or alpha!=1):
       with self.assertRaisesRegex(SceneUnsupported,'backdrop-dependent blend'):export_scene(source,layout())
      else:self.assertTrue(export_scene(source,layout())['pages']['0']['paths'])
 def test_annotation_and_widget_appearance_roots_are_not_exempt_page_groups(self):
  for kind in ['Stamp','FreeText','Widget']:
   for empty in [False,True]:
    for isolated in [False,True]:
     for blend in [False,True]:
      with self.subTest(kind=kind,empty=empty,isolated=isolated,blend=blend):
       source=annotation_blend_pdf(kind,isolated,blend,empty)
       with pymupdf.open(stream=source,filetype='pdf') as d:
        self.assertEqual(d[0].get_pixmap().pixel(50,50),(0,0,0) if blend and not isolated and not empty else (255,0,0))
       if isolated and blend:
        with self.assertRaisesRegex(SceneUnsupported,'backdrop-dependent blend'):export_scene(source,layout())
       else:self.assertTrue(export_scene(source,layout())['pages']['0']['paths'])
 def test_image_pixels_mask_and_safe_deterministic_zip(self):
  source,_=image_pdf();scene=export_scene(source,layout());images=[n for n in nodes(scene['pages']['0']['tree']) if n['type']=='image'];self.assertEqual(len(images),2)
  self.assertGreaterEqual(len(scene['assets']),2);self.assertTrue(any(n.get('mask') for n in nodes(scene['pages']['0']['tree'])))
  self.assertEqual(sum(a['width']==4 and a['height']==4 for a in scene['assets'].values()),2)
  with tempfile.TemporaryDirectory() as td:
   a=Path(td)/'a.zip';b=Path(td)/'b.zip';info=export_scene_bundle(source,layout(),a,64*1024*1024);export_scene_bundle(source,layout(),b,64*1024*1024)
   self.assertEqual(a.read_bytes(),b.read_bytes());self.assertEqual(info['sha256'],hashlib.sha256(a.read_bytes()).hexdigest());self.assertEqual(info['bytes'],a.stat().st_size)
   with zipfile.ZipFile(a) as z:
    self.assertEqual(set(z.namelist()),{'scene.json',*(v['path'] for v in scene['assets'].values())});s=json.loads(z.read('scene.json'));self.assertTrue(all('data' not in v for v in s['assets'].values()));self.assertEqual(info['unpacked_bytes'],sum(v.file_size for v in z.infolist()))
    for asset in s['assets'].values():self.assertEqual(hashlib.sha256(z.read(asset['path'])).hexdigest(),asset['sha256'])
   previous=a.read_bytes()
   with self.assertRaises((FileExistsError,SceneUnsupported)):export_scene_bundle(source,layout(),a,64*1024*1024)
   self.assertEqual(a.read_bytes(),previous)
   too_small=Path(td)/'small.zip'
   with self.assertRaises(SceneUnsupported):export_scene_bundle(source,layout(),too_small,10)
   self.assertFalse(too_small.exists())
 def test_source_interpolation_flag_is_retained(self):
  for flag in [False,True]:
   source,_=image_pdf(interpolate=flag);scene=export_scene(source,layout());images=[n for n in nodes(scene['pages']['0']['tree']) if n['type']=='image'];self.assertTrue(all(n['interpolate']==flag for n in images))
 def test_large_source_png_is_losslessly_partitioned_with_sampling_gutters(self):
  import base64
  source,pix=image_pdf(width=3001,height=2053,interpolate=True);scene=export_scene(source,layout());images=[n for n in nodes(scene['pages']['0']['tree']) if n['type']=='image']
  self.assertGreater(len(images),2);self.assertTrue(all(a['width']<=2048 and a['height']<=2048 for a in scene['assets'].values()));self.assertTrue(all(n['interpolate'] for n in images))
  self.assertEqual(sum(n['sourceRect'][2]*n['sourceRect'][3] for n in images),2*3001*2053)
  # A two-pixel kernel gutter surrounds each internal boundary while the
  # nonoverlapping core remains <=2044px (and the actual PNG <=2048px).
  self.assertEqual(sorted(set(tuple(n['sourceRect']) for n in images)),[(0,0,2044,2044),(0,2,2044,9),(2,0,957,2044),(2,2,957,9)])
  for n in images:
   asset=scene['assets'][n['assetId']];tile=pymupdf.Pixmap(base64.b64decode(asset['data'].split(',')[1]));sx,sy,sw,sh=n['sourceRect'];self.assertEqual((tile.width,tile.height),(asset['width'],asset['height']));self.assertEqual(tile.pixel(sx,sy),(100,100,100) if tile.n==3 else (255,))
 def test_blend(self):
  source=raw_pdf('<< /ExtGState << /G1 << /BM /Multiply /ca .6 >> >> >>','1 0 0 rg 0 0 70 70 re f /G1 gs 0 0 1 rg 30 30 70 70 re f');scene=export_scene(source,layout());self.assertTrue(any(n.get('blendMode')=='multiply' for n in nodes(scene['pages']['0']['tree'])))
 def test_executed_shading_is_detected_before_svg_raster_degradation(self):
  resources='<< /Shading << /S1 << /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 100 0] /Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> /Extend [true true] >> >> >>'
  with self.assertRaisesRegex(SceneUnsupported,'PDF shading'):export_scene(raw_pdf(resources,'/S1 sh'),layout())
  # A merely present unused resource must not reject an otherwise vector page.
  scene=export_scene(raw_pdf(resources,'0 0 10 10 re f'),layout());self.assertFalse(scene['assets'])
 def test_knockout_mapping_disambiguates_identical_nonknockout_paints(self):
  for clip in [False,True]:
   scene=export_scene(knockout_pdf(clip=clip),layout());tree=scene['pages']['0']['tree'];groups=[n for n in nodes(tree) if n.get('knockout')]
   self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]['children']),2)
   # Identical non-knockout fill/stroke remain direct siblings, unchanged.
   self.assertEqual(sum(n['type']=='path' for n in nodes(tree)),4)
 def test_complex_knockout_is_explicit_not_silently_flattened(self):
  with self.assertRaisesRegex(SceneUnsupported,'complex knockout'):export_scene(knockout_pdf(complex=True),layout())
 def test_rect_and_freetext_annotations_are_exported(self):
  for kind in ['Rect','FreeText']:
   source=annotation_pdf(kind);scene=export_scene(source,layout());self.assertTrue(scene['pages']['0']['paths'])
 def test_no_external_image_or_unknown_mask_style(self):
  for body in ['<image width="1" height="1" href="https://example.com/a.png"/>','<g style="filter:blur(1px)"/>']:
   with self.assertRaises(SceneUnsupported):export_svg('<svg width="100" height="100" viewBox="0 0 100 100">'+body+'</svg>')
def softmask_pdf(mode):
 d=pymupdf.open();p=d.new_page(width=100,height=100)
 form=d.get_new_xref();d.update_object(form,'<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Group << /S /Transparency /CS /DeviceRGB /I true >> /Resources <<>> >>');d.update_stream(form,b'0.2 g 0 0 100 100 re f 1 g 20 20 60 60 re f')
 resources=d.get_new_xref();d.update_object(resources,f'<< /ExtGState << /GS1 << /SMask << /S /{mode} /G {form} 0 R >> >> >> >>');d.xref_set_key(p.xref,'Resources',f'{resources} 0 R')
 content=d.get_new_xref();d.update_object(content,'<<>>');d.update_stream(content,b'/GS1 gs 1 0 0 rg 0 0 100 100 re f');d.xref_set_key(p.xref,'Contents',f'{content} 0 R');return d.tobytes()

def canvas_fixtures(directory):
 """Reproduce frontend native-Canvas probe inputs with real MuPDF references."""
 directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
 sources={'image':image_pdf()[0],'blend':raw_pdf('<< /ExtGState << /G1 << /BM /Multiply /ca .6 >> >> >>','1 0 0 rg 0 0 70 70 re f /G1 gs 0 0 1 rg 30 30 70 70 re f'),'clipreal':raw_pdf('<<>>','10 10 m 90 10 l 50 90 l h W n 1 0 0 rg 0 0 100 100 re f'),'maskLuminosity':softmask_pdf('Luminosity'),'maskAlpha':softmask_pdf('Alpha'),'maskColor':colored_mask_pdf(),'knockoutDuplicate':knockout_pdf(),'knockoutClip':knockout_pdf(clip=True),'annotRect':annotation_pdf('Rect'),'annotFreeText':annotation_pdf('FreeText')}
 for name,source in sources.items():
  (directory/(name+'.pdf')).write_bytes(source);(directory/(name+'.json')).write_text(json.dumps(export_scene(source,layout())))
  with pymupdf.open(stream=source,filetype='pdf') as document:document[0].get_pixmap(matrix=pymupdf.Matrix(3,3)).save(str(directory/(name+'-reference.png')))
 print(json.dumps(dict(directory=str(directory),cases=list(sources))))

if __name__=='__main__':
 import sys
 if len(sys.argv)==3 and sys.argv[1]=='--canvas-fixtures':canvas_fixtures(sys.argv[2])
 else:unittest.main()
