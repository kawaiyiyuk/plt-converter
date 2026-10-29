"""Projection scene JPEG source images become lossless PNG assets."""
import base64,re,unittest
import pymupdf
try:from app.projection_scene import export_scene,export_svg,SceneUnsupported
except ModuleNotFoundError:from projection_scene import export_scene,export_svg,SceneUnsupported

def layout():return dict(width_mm=100*25.4/72,height_mm=100*25.4/72,crop_margins_mm=dict(left=0,top=0,right=0,bottom=0),placements=[dict(page_index=0,x_mm=0,y_mm=0,width_mm=100*25.4/72,height_mm=100*25.4/72)])
def nodes(n):
 yield n
 for child in n.get('children',[]):yield from nodes(child)
 if n.get('mask'):yield from nodes(n['mask']['tree'])
def rgb_pixmap(width,height,shade):
 pix=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,width,height),False)
 for y in range(height):
  for x in range(width):pix.set_pixel(x,y,shade(x,y))
 return pix
def image_pdf(width,height,pixels,stream,mask=None):
 d=pymupdf.open();p=d.new_page(width=100,height=100)
 p.insert_image(pymupdf.Rect(10,10,90,90),stream=stream) if mask is None else p.insert_image(pymupdf.Rect(10,10,90,90),stream=stream,mask=mask)
 return d,p
def png_asset(scene,width,height,channels=None):
 for asset in scene['assets'].values():
  if (asset['width'],asset['height'])!=(width,height):continue
  blob=base64.b64decode(asset['data'].split(',',1)[1]);pix=pymupdf.Pixmap(blob)
  if channels is None or pix.n==channels:return asset,blob,pix
def svg(body):return '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="10" height="10" viewBox="0 0 10 10">'+body+'</svg>'
def adobe_marker(data):
 try:from app.projection_scene import _jpeg_has_adobe_marker
 except (ModuleNotFoundError,ImportError):from projection_scene import _jpeg_has_adobe_marker
 return _jpeg_has_adobe_marker(data)

class JPEGImages(unittest.TestCase):
 def test_rgb_jpeg_image_exports_as_png_asset(self):
  width,height=8,6;source=rgb_pixmap(width,height,lambda x,y:((x*37)%256,(y*53)%256,((x+y)*29)%256));jpeg=source.tobytes('jpeg')
  d,p=image_pdf(width,height,None,jpeg);self.assertIn('data:image/jpeg',p.get_svg_image())
  scene=export_scene(d.tobytes(),layout());asset,blob,png=png_asset(scene,width,height)
  self.assertTrue(asset['data'].startswith('data:image/png;base64,'));self.assertEqual(blob[:8],b'\x89PNG\r\n\x1a\n')
  original=pymupdf.Pixmap(jpeg);self.assertEqual((png.width,png.height),(original.width,original.height))
  for y in range(height):
   for x in range(width):
    for a,b in zip(png.pixel(x,y),original.pixel(x,y)):self.assertLessEqual(abs(a-b),2)

 def test_cmyk_jpeg_with_mask_exports_rgb(self):
  width,height=10,10;rgb=rgb_pixmap(width,height,lambda x,y:(200,80,40));cmyk=pymupdf.Pixmap(pymupdf.csCMYK,rgb);jpeg=cmyk.tobytes('jpeg')
  self.assertEqual((jpeg[2:4],jpeg[6:11]),(b'\xff\xee',b'Adobe'))
  mask=pymupdf.Pixmap(pymupdf.csGRAY,pymupdf.IRect(0,0,width,height),False);mask.clear_with(255);mask_png=mask.tobytes('png')
  d,p=image_pdf(width,height,None,jpeg,mask_png);svg_text=p.get_svg_image();self.assertIn('data:image/jpeg',svg_text)
  # PyMuPDF may re-encode synthetic CMYK images without APP14; real Adobe PDFs
  # keep the source JPEG, so substitute it into the same PDF/mask SVG.
  svg_text=re.sub(r'data:image/jpeg;base64,[A-Za-z0-9+/=\s]+','data:image/jpeg;base64,'+base64.b64encode(jpeg).decode('ascii'),svg_text,count=1)
  assets={};export_svg(svg_text,assets);scene={'assets':assets};asset,blob,color=png_asset(scene,width,height,3)
  self.assertIsNotNone(asset);self.assertEqual(color.n,3);self.assertEqual(blob[:8],b'\x89PNG\r\n\x1a\n')
  expected=pymupdf.Pixmap(pymupdf.csRGB,cmyk)
  for y in range(height):
   for x in range(width):
    for a,b in zip(color.pixel(x,y),expected.pixel(x,y)):self.assertLessEqual(abs(a-b),8)

 def test_adobe_marker_scan_ignores_entropy_data(self):
  rgb=rgb_pixmap(8,6,lambda x,y:((x*37)%256,(y*53)%256,((x+y)*29)%256));rgb_jpeg=rgb.tobytes('jpeg');self.assertFalse(adobe_marker(rgb_jpeg))
  cmyk_jpeg=pymupdf.Pixmap(pymupdf.csCMYK,rgb).tobytes('jpeg');self.assertTrue(adobe_marker(cmyk_jpeg))
  fake=rgb_jpeg[:-2]+b'\xff\xeeAdobe'+rgb_jpeg[-2:];self.assertIn(b'\xff\xeeAdobe',fake);self.assertFalse(adobe_marker(fake))

 def test_unsupported_image_mime_still_rejected(self):
  payload=base64.b64encode(b'GIF89a').decode('ascii')
  with self.assertRaises(SceneUnsupported) as caught:export_svg(svg('<image width="1" height="1" href="data:image/gif;base64,'+payload+'"/>'))
  self.assertIn('PNG or JPEG',str(caught.exception))
