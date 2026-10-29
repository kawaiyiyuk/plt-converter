"""Offline experimental PDF -> native Canvas scene v1; no HTTP/store side effects.

Strict SVG subset. Unsupported content raises; never rasterizes a page or omits
unknown paint. All geometry remains in original page points and layout mm.
"""
from __future__ import annotations
import argparse
import base64
import io
import struct
import zipfile
import copy
import hashlib
import json
import math
import re
from pathlib import Path
import xml.etree.ElementTree as ET

IDENTITY = [1., 0., 0., 1., 0., 0.]
PT_PER_MM = 72 / 25.4
NUMBER = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'
TOKENS = re.compile(r'[A-Za-z]|' + NUMBER)
SVG_NS='http://www.w3.org/2000/svg'
INK_NS='http://www.inkscape.org/namespaces/inkscape'
XLINK_NS='http://www.w3.org/1999/xlink'

class SceneUnsupported(ValueError):
    """Input would lose fidelity with the supported scene contract."""

def _load_pymupdf():
    """Load both supported package names without hiding broken native imports."""
    try:
        import pymupdf
    except ModuleNotFoundError as error:
        if error.name != 'pymupdf':
            raise
        import fitz as pymupdf
    return pymupdf

_INVERT_BYTES=bytes(range(255,-1,-1))

def _jpeg_has_adobe_marker(data):
    """Scan JPEG marker segments before SOS for the Adobe APP14 transform."""
    pos=2
    while pos+4<=len(data):
        if data[pos]!=0xff:return False
        marker=data[pos+1]
        if marker in (0xd9,0xda):return False
        if marker==0x01 or 0xd0<=marker<=0xd8:pos+=2;continue
        length=struct.unpack('>H',data[pos+2:pos+4])[0]
        if marker==0xee and data[pos+4:pos+9]==b'Adobe':return True
        pos+=2+length
    return False

def finite(value, context='number'):
    try: out=float(value)
    except (TypeError,ValueError) as e: raise SceneUnsupported(f'{context}: invalid number {value!r}') from e
    if not math.isfinite(out): raise SceneUnsupported(f'{context}: nonfinite number')
    return out

def numbers(value, context):
    result=[];end=0
    for m in re.finditer(NUMBER,value):
        if value[end:m.start()].strip(' ,\t\n\r'): raise SceneUnsupported(f'{context}: invalid numeric separator')
        result.append(finite(m.group(),context));end=m.end()
    if value[end:].strip(' ,\t\n\r'): raise SceneUnsupported(f'{context}: invalid numeric value')
    return result

def multiply(a,b):
    return [a[0]*b[0]+a[2]*b[1],a[1]*b[0]+a[3]*b[1],a[0]*b[2]+a[2]*b[3],a[1]*b[2]+a[3]*b[3],a[0]*b[4]+a[2]*b[5]+a[4],a[1]*b[4]+a[3]*b[5]+a[5]]

def transform(value):
    if not value:return IDENTITY.copy()
    out=IDENTITY.copy();end=0
    for m in re.finditer(r'([A-Za-z]+)\s*\(([^)]*)\)',value):
        if value[end:m.start()].strip(' ,\t\n\r'):raise SceneUnsupported('transform: invalid syntax')
        name=m[1];v=numbers(m[2],'transform');end=m.end()
        if name=='matrix' and len(v)==6: current=v
        elif name=='translate' and len(v) in [1,2]:current=[1,0,0,1,v[0],v[1] if len(v)==2 else 0]
        elif name=='scale' and len(v) in [1,2]:current=[v[0],0,0,v[1] if len(v)==2 else v[0],0,0]
        elif name=='rotate' and len(v) in [1,3]:
            c,s=math.cos(math.radians(v[0])),math.sin(math.radians(v[0]));current=[c,s,-s,c,0,0]
            if len(v)==3:current=multiply(multiply([1,0,0,1,v[1],v[2]],current),[1,0,0,1,-v[1],-v[2]])
        elif name in ['skewX','skewY'] and len(v)==1:
            t=math.tan(math.radians(v[0]));current=[1,0,t,1,0,0] if name=='skewX' else [1,t,0,1,0,0]
        else:raise SceneUnsupported(f'transform: unsupported {name}/{len(v)}')
        out=multiply(out,current)
    if value[end:].strip(' ,\t\n\r') or not end:raise SceneUnsupported('transform: invalid syntax')
    if not all(math.isfinite(x) for x in out):raise SceneUnsupported('transform: nonfinite matrix')
    return out

def parse_path(value):
    """SVG path -> absolute M/L/C/Q/Z, retaining curves without flattening."""
    tokens=[];end=0
    for m in TOKENS.finditer(value):
        if value[end:m.start()].strip(' ,\t\n\r'):raise SceneUnsupported('path: invalid syntax')
        t=m.group();tokens.append(t if t.isalpha() else finite(t,'path'));end=m.end()
    if value[end:].strip(' ,\t\n\r'):raise SceneUnsupported('path: invalid suffix')
    result=[];i=0;command=None;previous=None;x=y=sx=sy=0.;last_cubic=last_quad=None
    arities={'M':2,'L':2,'H':1,'V':1,'C':6,'S':4,'Q':4,'T':2}
    while i<len(tokens):
        if isinstance(tokens[i],str):command=tokens[i];i+=1
        elif command is None:raise SceneUnsupported('path: missing command')
        upper=command.upper();relative=command.islower()
        if upper=='Z':
            result.append(['Z']);x,y=sx,sy;previous='Z';last_cubic=last_quad=None;command=None;continue
        if upper not in arities:raise SceneUnsupported(f'path: unsupported command {command}')
        count=arities[upper]
        if i+count>len(tokens) or any(isinstance(t,str) for t in tokens[i:i+count]):raise SceneUnsupported('path: missing parameters')
        v=tokens[i:i+count];i+=count;ox,oy=x,y
        def xy(a,b):return [a+ox,b+oy] if relative else [a,b]
        if upper in ['M','L','T']:
            nx,ny=xy(*v)
            if upper=='T':
                q=[2*x-last_quad[0],2*y-last_quad[1]] if previous in ['Q','T'] else [x,y]
                result.append(['Q',*q,nx,ny]);last_quad=q
            else:
                result.append([upper,nx,ny])
                if upper=='M':sx,sy=nx,ny;command='l' if relative else 'L'
            x,y=nx,ny
        elif upper=='H':x=v[0]+ox if relative else v[0];result.append(['L',x,y])
        elif upper=='V':y=v[0]+oy if relative else v[0];result.append(['L',x,y])
        elif upper=='C':
            p1,p2,p3=xy(*v[0:2]),xy(*v[2:4]),xy(*v[4:6]);result.append(['C',*p1,*p2,*p3]);last_cubic=p2;x,y=p3
        elif upper=='S':
            p1=[2*x-last_cubic[0],2*y-last_cubic[1]] if previous in ['C','S'] else [x,y]
            p2,p3=xy(*v[0:2]),xy(*v[2:4]);result.append(['C',*p1,*p2,*p3]);last_cubic=p2;x,y=p3
        elif upper=='Q':
            p1,p2=xy(*v[0:2]),xy(*v[2:4]);result.append(['Q',*p1,*p2]);last_quad=p1;x,y=p2
        if upper not in ['C','S']:last_cubic=None
        if upper not in ['Q','T']:last_quad=None
        previous=upper
    if result and result[0][0]!='M':raise SceneUnsupported('path: must start with moveto')
    return result

STYLE_KEYS={'fill','fill-opacity','fill-rule','stroke','stroke-opacity','stroke-width','stroke-linecap','stroke-linejoin','stroke-miterlimit','stroke-dasharray','stroke-dashoffset','clip-rule'}
DEFAULT_STYLE={'fill':'#000000','fill-opacity':'1','fill-rule':'nonzero','stroke':'none','stroke-opacity':'1','stroke-width':'1','stroke-linecap':'butt','stroke-linejoin':'miter','stroke-miterlimit':'4','stroke-dasharray':'none','stroke-dashoffset':'0','clip-rule':'nonzero'}
def color(value):
    if value=='none':return None
    if re.fullmatch(r'#[0-9a-fA-F]{3}',value):return '#'+''.join(c*2 for c in value[1:]).lower()
    if re.fullmatch(r'#[0-9a-fA-F]{6}',value):return value.lower()
    if value in ['black','white']:return {'black':'#000000','white':'#ffffff'}[value]
    raise SceneUnsupported(f'paint: unsupported color {value!r}')
def opacity(value):
    v=finite(value,'opacity')
    if not 0<=v<=1:raise SceneUnsupported('opacity: outside 0..1')
    return v

def style_fields(style):
    for key,allowed in [('fill-rule',['nonzero','evenodd']),('clip-rule',['nonzero','evenodd']),('stroke-linecap',['butt','round','square']),('stroke-linejoin',['miter','round','bevel'])]:
        if style[key] not in allowed:raise SceneUnsupported(f'{key}: unsupported {style[key]!r}')
    width=finite(style['stroke-width'],'stroke-width');miter=finite(style['stroke-miterlimit'],'stroke-miterlimit')
    if width<0 or miter<1:raise SceneUnsupported('stroke: invalid width or miter limit')
    dash=[] if style['stroke-dasharray']=='none' else numbers(style['stroke-dasharray'],'stroke-dasharray')
    if any(v<0 for v in dash) or (dash and not any(dash)):raise SceneUnsupported('stroke-dasharray: invalid pattern')
    return dict(fill=color(style['fill']),fillOpacity=opacity(style['fill-opacity']),fillRule=style['fill-rule'],stroke=color(style['stroke']),strokeOpacity=opacity(style['stroke-opacity']),strokeWidth=width,lineCap=style['stroke-linecap'],lineJoin=style['stroke-linejoin'],miterLimit=miter,dash=dash,dashOffset=finite(style['stroke-dashoffset'],'stroke-dashoffset'))

def tag_of(element):
    if element.tag.startswith('{'):
        namespace,tag=element.tag[1:].split('}',1)
        if namespace!=SVG_NS:raise SceneUnsupported(f'element: unsupported namespace {namespace}')
        return tag
    return element.tag

def attrs_of(element):
    attrs={}
    for key,value in element.attrib.items():
        if key.startswith('{'):
            ns,key=key[1:].split('}',1)
            if ns==INK_NS and key in ['groupmode','label']:continue
            if ns!=XLINK_NS or key!='href':raise SceneUnsupported(f'attribute: unsupported namespace {ns}/{key}')
        if key in attrs:raise SceneUnsupported(f'attribute: duplicate {key}')
        attrs[key]=value
    return attrs

class SvgScene:
    def __init__(self,svg,assets=None,sampling=None):
        try:self.root=ET.fromstring(svg)
        except ET.ParseError as e:raise SceneUnsupported(f'SVG: parse failure {e}') from e
        self.ids={};self.paths={};self.path_cache={};self.assets=assets if assets is not None else {};self.sampling=sampling or {};self.image_sources={}
        for element in self.root.iter():
            tag=tag_of(element);attrs=attrs_of(element)
            if tag not in ['svg','defs','clipPath','g','path','use','image','mask','rect']:raise SceneUnsupported(f'element: unsupported {tag}')
            allowed={
                'svg':{'version','width','height','viewBox'},
                'defs':{'id'},'clipPath':{'id','clipPathUnits'},
                'g':{'id','transform','opacity','clip-path','mask','style'},
                'mask':{'id','maskUnits','maskContentUnits','x','y','width','height','mask-type'},
                'rect':{'id','x','y','width','height','transform','opacity','clip-path'},
                'image':{'id','href','width','height','x','y','transform','opacity','clip-path','preserveAspectRatio'},
                'path':{'id','d','transform','opacity','clip-path'},
                'use':{'id','href','transform','x','y','data-text','opacity','clip-path'},
            }[tag] | (STYLE_KEYS if tag in ['g','path','use','clipPath','rect'] else set())
            unknown=set(attrs)-allowed
            if unknown:raise SceneUnsupported(f'{tag}: unsupported attributes {sorted(unknown)}')
            if element.text and element.text.strip():raise SceneUnsupported(f'{tag}: unexpected text')
            if element.tail and element.tail.strip():raise SceneUnsupported(f'{tag}: unexpected tail')
            if attrs.get('id'):
                if attrs['id'] in self.ids:raise SceneUnsupported(f'reference: duplicate id {attrs["id"]}')
                self.ids[attrs['id']]=element
            if tag in ['path','use','image','rect'] and len(element):raise SceneUnsupported(f'{tag}: unexpected children')
    def path_id(self,element):
        value=attrs_of(element).get('d')
        if value is None:raise SceneUnsupported('path: missing d')
        if value not in self.path_cache:
            key=f'path_{len(self.paths)}';self.paths[key]=parse_path(value);self.path_cache[value]=key
        return self.path_cache[value]
    def reference(self,href):
        if not href or not href.startswith('#') or href[1:] not in self.ids:raise SceneUnsupported(f'reference: unresolved or external {href!r}')
        return self.ids[href[1:]]
    def clip(self,value):
        m=re.fullmatch(r'url\(#([^\s)]+)\)',value)
        if not m:raise SceneUnsupported(f'clip-path: unsupported {value!r}')
        element=self.reference('#'+m[1]);a=attrs_of(element)
        if tag_of(element)!='clipPath' or a.get('clipPathUnits','userSpaceOnUse')!='userSpaceOnUse':raise SceneUnsupported('clip-path: unsupported reference/units')
        # v1 union semantics are deliberately limited to a single path; more
        # paths with different winding could not be represented by one clip().
        children=list(element)
        if len(children)!=1 or tag_of(children[0])!='path':raise SceneUnsupported('clipPath: requires exactly one path')
        child=children[0];ca=attrs_of(child)
        if set(ca)-({'id','d','transform','clip-rule'}):raise SceneUnsupported('clipPath/path: unsupported attributes')
        rule=ca.get('clip-rule',a.get('clip-rule','nonzero'))
        if rule not in ['nonzero','evenodd']:raise SceneUnsupported('clipPath: unsupported winding')
        return [dict(path=self.path_id(child),transform=transform(ca.get('transform')),fillRule=rule)]
    def image(self,a,build_levels=True,sampling_override=None):
        pymupdf=_load_pymupdf()
        href=a.get('href','');match=re.fullmatch(r'data:image/(png|jpe?g);base64,([A-Za-z0-9+/=\s]+)',href)
        if not match:raise SceneUnsupported('image: only source-embedded PNG or JPEG accepted')
        png=match[1].lower()=='png'
        try:
            data=base64.b64decode(re.sub(r'\s','',match[2]),validate=True)
            if png:
                if data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR':raise ValueError('PNG header')
                width,height=struct.unpack('>II',data[16:24]);pix=pymupdf.Pixmap(data)
                if (pix.width,pix.height)!=(width,height):raise ValueError('PNG dimensions')
            else:
                if data[:2]!=b'\xff\xd8':raise ValueError('JPEG header')
                pix=pymupdf.Pixmap(data)
                # Adobe APP14 CMYK JPEGs store inverted ink samples; undo before RGB.
                if pix.n-pix.alpha==4 and _jpeg_has_adobe_marker(data):pix=pymupdf.Pixmap(pymupdf.csCMYK,pix.width,pix.height,bytes(pix.samples).translate(_INVERT_BYTES),False)
                # MuPDF emits JPEG source bytes verbatim; JPEG has no alpha, so
                # CMYK and other non-RGB/grayscale spaces must be converted.
                if not pix.alpha and (pix.colorspace is None or pix.colorspace.n not in (1,3)):pix=pymupdf.Pixmap(pymupdf.csRGB,pix)
                data=pix.tobytes('png');width,height=pix.width,pix.height
        except Exception as e:raise SceneUnsupported('image: invalid embedded PNG' if png else 'image: invalid embedded JPEG') from e
        key=hashlib.sha256(data).hexdigest()
        w,h=finite(a.get('width')),finite(a.get('height'))
        if w<=0 or h<=0:raise SceneUnsupported('image: invalid dimensions')
        if a.get('preserveAspectRatio','xMidYMid meet') not in ['none','xMidYMid meet']:raise SceneUnsupported('image: unsupported aspect ratio')
        # MuPDF emits matching intrinsic/display aspect ratios (or none).
        if a.get('preserveAspectRatio')!='none' and abs(w/h-width/height)>1e-6:raise SceneUnsupported('image: mismatched aspect ratio')
        origin=multiply(transform(a.get('transform')),[1,0,0,1,finite(a.get('x','0')),finite(a.get('y','0'))])
        interpolate=self.sampling.get(key,False) if sampling_override is None else sampling_override
        def asset(blob,aw,ah):
            aid=hashlib.sha256(blob).hexdigest()
            self.assets[aid]=dict(id=aid,sha256=aid,path=f'assets/{aid}.png',width=aw,height=ah,bytes=len(blob),data='data:image/png;base64,'+base64.b64encode(blob).decode('ascii'))
            return aid
        if width<=2048 and height<=2048:
            original=dict(type='image',assetId=asset(data,width,height),width=w,height=h,opacity=1,transform=IDENTITY.copy(),interpolate=interpolate)
            result=dict(type='group',transform=origin,opacity=opacity(a.get('opacity','1')),children=[original])
            if build_levels:self.attach_levels(result,pix,w,h,interpolate)
            self.image_sources[id(result)]=(result,pix,w,h)
            return result
        # Lossless source-pixel partition, not downsampling. Two-pixel gutters
        # retain the high-quality sampling kernel at rotated internal edges.
        children=[];core=2044
        for y in range(0,height,core):
            for x in range(0,width,core):
                cw,ch=min(core,width-x),min(core,height-y)
                gx,gy=max(0,x-2),max(0,y-2);right,bottom=min(width,x+cw+2),min(height,y+ch+2)
                tile=pymupdf.Pixmap(pix,width,height,pymupdf.IRect(gx,gy,right,bottom));blob=tile.tobytes('png')
                children.append(dict(type='image',assetId=asset(blob,tile.width,tile.height),width=cw*w/width,height=ch*h/height,sourceRect=[x-gx,y-gy,cw,ch],opacity=1,interpolate=interpolate,transform=[1,0,0,1,x*w/width,y*h/height]))
        result=dict(type='group',transform=origin,opacity=opacity(a.get('opacity','1')),children=children)
        if build_levels:self.attach_levels(result,pix,w,h,interpolate)
        self.image_sources[id(result)]=(result,pix,w,h)
        return result
    def attach_levels(self,result,pix,w,h,interpolate):
        pymupdf=_load_pymupdf()
        working=pymupdf.Pixmap(pix,pix.alpha);levels=[];factor=1
        while working.width>1 or working.height>1:
            factor*=2;working=pymupdf.Pixmap(pix,math.ceil(pix.width/factor),math.ceil(pix.height/factor),None)
            data=working.tobytes('png')
            level=self.image(dict(href='data:image/png;base64,'+base64.b64encode(data).decode('ascii'),width=str(w),height=str(h),preserveAspectRatio='none'),False,True)
            level=self.bound_image_level(level,w,h)
            levels.append(dict(sampleScale=factor,tree=level))
        if levels:
            result['imageSource']=[pix.width,pix.height,w,h];result['imageLevels']=levels
    def bound_image_level(self,tree,w,h):
        path=self.path_id(ET.Element('path',{'d':f'M0 0H{w}V{h}H0Z'}))
        return dict(type='group',transform=IDENTITY.copy(),opacity=1,children=[tree],clip=[dict(path=path,transform=IDENTITY.copy(),fillRule='nonzero',exact=True)])
    def one_image_source(self,node,parent=None):
        parent=IDENTITY if parent is None else parent
        frame=multiply(parent,node['transform'])
        if id(node) in self.image_sources and self.image_sources[id(node)][0] is node:return (*self.image_sources[id(node)][1:],frame)
        if node['type']!='group' or node['opacity']!=1 or any(k in node for k in ['clip','mask','blendMode']) or len(node['children'])!=1:return None
        return self.one_image_source(node['children'][0],frame)
    def combined_mask_levels(self,group):
        # Image soft masks must be filtered jointly in premultiplied space.
        # Filtering unassociated RGB and alpha independently creates halos.
        pymupdf=_load_pymupdf()
        if group['mask']['type']!='luminance' or len(group['children'])!=1:return
        rgb=self.one_image_source(group['children'][0]);mask=self.one_image_source(group['mask']['tree'])
        if rgb is None or mask is None:return
        pix,w,h,frame=rgb;mp,mw,mh,mframe=mask
        if pix.alpha or mp.alpha or pix.n!=3 or mp.n!=1 or (pix.width,pix.height)!=(mp.width,mp.height) or (w,h)!=(mw,mh) or any(abs(a-b)>1e-9 for a,b in zip(frame,mframe)):return
        colors=pix.samples;alpha=mp.samples;rgba=bytearray(pix.width*pix.height*4)
        for i,a in enumerate(alpha):
            rgba[4*i]=round(colors[3*i]*a/255);rgba[4*i+1]=round(colors[3*i+1]*a/255);rgba[4*i+2]=round(colors[3*i+2]*a/255);rgba[4*i+3]=a
        original=pymupdf.Pixmap(pymupdf.csRGB,pix.width,pix.height,bytes(rgba),True);working=original;levels=[];factor=1
        while working.width>1 or working.height>1:
            factor*=2;working=pymupdf.Pixmap(original,math.ceil(pix.width/factor),math.ceil(pix.height/factor),None);pixels=working.samples;count=working.width*working.height;rgbbytes=bytearray(count*3);abytes=bytearray(count)
            for i in range(count):
                a=pixels[4*i+3];abytes[i]=a
                if a:
                    rgbbytes[3*i]=min(255,round(pixels[4*i]*255/a));rgbbytes[3*i+1]=min(255,round(pixels[4*i+1]*255/a));rgbbytes[3*i+2]=min(255,round(pixels[4*i+2]*255/a))
            colorpix=pymupdf.Pixmap(pymupdf.csRGB,working.width,working.height,bytes(rgbbytes),False);maskpix=pymupdf.Pixmap(pymupdf.csGRAY,working.width,working.height,bytes(abytes),False)
            def node(p):return self.image(dict(href='data:image/png;base64,'+base64.b64encode(p.tobytes('png')).decode('ascii'),width=str(w),height=str(h),preserveAspectRatio='none'),False,True)
            level=dict(type='group',transform=frame,opacity=1,children=[self.bound_image_level(dict(type='group',transform=IDENTITY.copy(),opacity=1,children=[node(colorpix)],mask=dict(type='luminance',tree=node(maskpix))),w,h)])
            levels.append(dict(sampleScale=factor,tree=level))
        if levels:
            def strip(n):
                n.pop('imageLevels',None);n.pop('imageSource',None)
                for child in n.get('children',[]):strip(child)
                if n.get('mask'):strip(n['mask']['tree'])
            for child in group['children']:strip(child)
            strip(group['mask']['tree'])
            group['imageSource']=[pix.width,pix.height,w,h];group['imageTransform']=frame;group['imageLevels']=levels
    def mask(self,value,stack):
        m=re.fullmatch(r'url\(#([^\s)]+)\)',value)
        if not m:raise SceneUnsupported('mask: invalid reference')
        target=self.reference('#'+m[1]);a=attrs_of(target)
        if tag_of(target)!='mask' or a.get('maskContentUnits','userSpaceOnUse')!='userSpaceOnUse':raise SceneUnsupported('mask: unsupported units')
        if id(target) in stack:raise SceneUnsupported('mask: cyclic reference')
        if set(a)-{'id','maskContentUnits','mask-type'}:raise SceneUnsupported('mask: unsupported bounds or units')
        tree=dict(type='group',transform=IDENTITY.copy(),opacity=1,children=[self.node(child,DEFAULT_STYLE,(*stack,id(target))) for child in target])
        kind=a.get('mask-type','luminance')
        if kind not in ['alpha','luminance']:raise SceneUnsupported('mask: unsupported type')
        return dict(type=kind,tree=tree)
    def node(self,element,inherited,stack=()):
        tag=tag_of(element);a=attrs_of(element)
        if tag=='defs':return None
        if tag in ['clipPath','mask']:return None
        style={**inherited,**{k:v for k,v in a.items() if k in STYLE_KEYS}}
        matrix=transform(a.get('transform'));op=opacity(a.get('opacity','1'));clip=self.clip(a['clip-path']) if 'clip-path' in a else None
        if tag=='image':
            item=self.image(a)
            if clip:
                frame=transform(a.get('transform'));item['transform']=[1,0,0,1,finite(a.get('x','0')),finite(a.get('y','0'))]
                return dict(type='group',transform=frame,opacity=1,clip=clip,children=[item])
            return item
        if tag=='rect':
            def length(key,default,bound):
                value=a.get(key,default)
                return finite(value[:-1])*bound/100 if value.endswith('%') else finite(value)
            attrs=attrs_of(self.root)
            x=length('x','0',finite(attrs['width']));y=length('y','0',finite(attrs['height']));w=length('width','0',finite(attrs['width']));h=length('height','0',finite(attrs['height']))
            if w<0 or h<0:raise SceneUnsupported('rect: negative dimensions')
            element=ET.Element('path',{**{k:v for k,v in a.items() if k not in ['x','y','width','height']},'d':f'M{x} {y}h{w}v{h}h{-w}Z'})
            tag='path'
        if tag=='path':
            item=dict(type='path',path=self.path_id(element),transform=matrix,**style_fields(style))
            if op!=1 or clip:
                group=dict(type='group',transform=IDENTITY.copy(),opacity=op,children=[item])
                if clip:
                    # A path's clip follows its transform. Move transform to the
                    # wrapper to keep both geometry and clip in one local frame.
                    group['transform']=matrix;item['transform']=IDENTITY.copy();group['clip']=clip
                return group
            return item
        if tag=='use':
            target=self.reference(a.get('href'))
            if id(target) in stack:raise SceneUnsupported('use: cyclic reference')
            if tag_of(target) not in ['path','g','use','image','rect']:raise SceneUnsupported('use: unsupported reference type')
            matrix=multiply(matrix,[1,0,0,1,finite(a.get('x','0')),finite(a.get('y','0'))])
            item=self.node(target,style,(*stack,id(target)));children=[item]
        elif tag in ['svg','g']:
            children=[self.node(child,style,stack) for child in element];children=[x for x in children if x is not None]
        else:raise SceneUnsupported(f'node: unsupported {tag}')
        group=dict(type='group',transform=matrix,opacity=op,children=children)
        if clip:group['clip']=clip
        if 'mask' in a:
            group['mask']=self.mask(a['mask'],stack)
            self.combined_mask_levels(group)
        if 'style' in a:
            match=re.fullmatch(r'mix-blend-mode:([a-z-]+);?',a['style'])
            if not match or match[1] not in BLEND_MODES:raise SceneUnsupported('group: unsupported style')
            group['blendMode']=match[1]
        return group
    def export(self):
        if tag_of(self.root)!='svg':raise SceneUnsupported('root: expected svg')
        attrs=attrs_of(self.root);bounds=numbers(attrs.get('viewBox',''),'viewBox')
        if len(bounds)!=4 or bounds[0]!=0 or bounds[1]!=0 or bounds[2]<=0 or bounds[3]<=0:raise SceneUnsupported('root: unsupported viewBox')
        width,height=finite(attrs.get('width')),finite(attrs.get('height'))
        if abs(width-bounds[2])>1e-5 or abs(height-bounds[3])>1e-5:raise SceneUnsupported('root: physical/viewBox mismatch')
        tree=self.node(self.root,DEFAULT_STYLE)
        return dict(width_pt=width,height_pt=height,paths=self.paths,tree=tree)

BLEND_MODES={'normal','multiply','screen','overlay','darken','lighten','color-dodge','color-burn','hard-light','soft-light','difference','exclusion','hue','saturation','color','luminosity'}

def export_svg(svg,assets=None,sampling=None):return SvgScene(svg,assets,sampling).export()

def path_signature(commands,frame):
    transformed=[]
    for command in commands:
        out=[command[0]]
        for x,y in zip(command[1::2],command[2::2]):out.extend([round(frame[0]*x+frame[2]*y+frame[4],3),round(frame[1]*x+frame[3]*y+frame[5],3)])
        transformed.append(out)
    return json.dumps(transformed,separators=(',',':'))

def source_path_signature(path,ctm):
    pymupdf=_load_pymupdf()
    m=pymupdf.mupdf
    Walker=getattr(source_path_signature,'_walker_type',None)
    if Walker is None:
        class Walker(m.FzPathWalker2):
            def __init__(self):
                super().__init__();self.commands=[]
                for name in ['moveto','lineto','curveto','closepath']:getattr(self,'use_virtual_'+name)()
            def moveto(self,arg,x,y):self.commands.append(['M',x,y])
            def lineto(self,arg,x,y):self.commands.append(['L',x,y])
            def curveto(self,arg,*values):self.commands.append(['C',*values])
            def closepath(self,arg):self.commands.append(['Z'])
        source_path_signature._walker_type=Walker
    walker=Walker();m.fz_walk_path(m.FzPath(m.ll_fz_keep_path(path)),walker,walker.m_internal)
    return path_signature(walker.commands,[ctm.a,ctm.b,ctm.c,ctm.d,ctm.e,ctm.f])

def apply_knockout_pairs(page,pairs,source_counts):
    # Match by geometry AND exact occurrence among all source fill/stroke
    # atomic paints. Repeated identical non-knockout paints keep their semantics.
    if not pairs:return
    requested=set(pairs);counts={};found=[]
    def find(group,parent):
        frame=multiply(parent,group['transform']);children=group.get('children',[]);i=0
        while i<len(children):
            first=children[i];second=children[i+1] if i+1<len(children) else None
            if first['type']=='path' and second and second['type']=='path' and first['fill'] and not first['stroke'] and second['stroke'] and not second['fill']:
                a=path_signature(page['paths'][first['path']],multiply(frame,first['transform']));b=path_signature(page['paths'][second['path']],multiply(frame,second['transform']))
                if a==b and a in source_counts:
                    ordinal=counts.get(a,0);counts[a]=ordinal+1
                    if (a,ordinal) in requested:found.append((group,i,a,ordinal))
                    i+=2;continue
            if first['type']=='group':find(first,frame)
            i+=1
        if group.get('mask'):find(group['mask']['tree'],frame)
    find(page['tree'],IDENTITY)
    if {(a,o) for _,_,a,o in found}!=requested or any(counts.get(a)!=source_counts[a] for a,_ in pairs):raise SceneUnsupported('PDF knockout group could not be mapped unambiguously without fidelity loss')
    for group,index,a,ordinal in reversed(found):
        group['children'][index:index+2]=[dict(type='group',transform=IDENTITY.copy(),opacity=1,knockout=True,children=group['children'][index:index+2])]

def apply_hairlines(page,pairs,source_counts):
    # SVG omits the width for PDF 0w, which would become its default 1pt.
    # Match source geometry AND occurrence among all strokes of that geometry,
    # so a genuine 1pt stroke over the exact same path stays a normal stroke.
    if not pairs:return
    requested=set(pairs);counts={};found=[]
    def visit(node,parent):
        frame=multiply(parent,node['transform'])
        # A PDF softmask is painted before the content to which it applies.
        if node.get('mask'):visit(node['mask']['tree'],frame)
        if node['type']=='path' and node['stroke']:
            signature=path_signature(page['paths'][node['path']],frame)
            if signature in source_counts:
                ordinal=counts.get(signature,0);counts[signature]=ordinal+1
                if (signature,ordinal) in requested:found.append((node,signature,ordinal))
        for child in node.get('children',[]):visit(child,frame)
    visit(page['tree'],IDENTITY)
    if {(signature,ordinal) for _,signature,ordinal in found}!=requested or any(counts.get(signature)!=source_counts[signature] for signature,_ in pairs):
        raise SceneUnsupported('PDF hairline strokes could not be mapped unambiguously without fidelity loss')
    for node,_,_ in found:node['hairline']=True

def inspect_scene_semantics(page):
    """Inspect executed source paint operators, before SVG's shade rasterizer.

    Unused PDF resources are not a reason to reject a page. This device runs the
    source display operations and deliberately never renders pixels.
    """
    pymupdf=_load_pymupdf()
    m=pymupdf.mupdf
    class Probe(m.FzDevice2):
        def __init__(self):
            super().__init__();self.unsupported=set();self.sampling={};self.failure=None;self.use_virtual_fill_shade();self.use_virtual_begin_tile();self.use_virtual_fill_image();self.use_virtual_fill_image_mask();self.use_virtual_clip_image_mask();self.use_virtual_begin_group();self.use_virtual_end_group();self.use_virtual_begin_mask();self.use_virtual_end_mask();self.use_virtual_fill_path();self.use_virtual_stroke_path();self.use_virtual_fill_text();self.use_virtual_stroke_text();self.use_virtual_clip_stroke_path();self.use_virtual_clip_stroke_text();self.groups=[];self.knockout_pairs=[];self.pair_counts={};self.previous_paint=None;self.hairlines=[];self.stroke_counts={};self.phase='contents'
        def record_image(self,image,cp):
            try:
                while image:
                    buffer=m.FzBuffer(m.ll_fz_new_buffer_from_image_as_png(image,cp));blob=bytes(pymupdf.JM_BinFromBuffer(buffer));key=hashlib.sha256(blob).hexdigest();value=bool(image.interpolate)
                    if key in self.sampling and self.sampling[key]!=value:self.unsupported.add('conflicting image interpolation')
                    self.sampling[key]=value;image=image.mask
            except Exception as error:self.failure=error
        def paint(self,kind,alpha,path=None,ctm=None,extra=False):
            signature=None
            if path is not None:signature=source_path_signature(path,ctm)
            ordinal=None
            if kind=='stroke' and self.previous_paint==('fill',signature):
                ordinal=self.pair_counts.get(signature,0);self.pair_counts[signature]=ordinal+1
            self.previous_paint=(kind,signature)
            for group in self.groups:
                if group['knockout']:
                    group['paints'].append((kind,signature,ordinal));group['transparent']|=alpha!=1 or extra
            return signature
        def fill_path(self,ctx,path,even_odd,ctm,cs,color,alpha,cp):self.paint('fill',alpha,path,ctm)
        def stroke_path(self,ctx,path,stroke,ctm,cs,color,alpha,cp):
            signature=self.paint('stroke',alpha,path,ctm);ordinal=self.stroke_counts.get(signature,0);self.stroke_counts[signature]=ordinal+1
            if stroke.linewidth==0:self.hairlines.append((signature,ordinal))
        def fill_text(self,ctx,text,ctm,cs,color,alpha,cp):self.paint('text',alpha)
        def stroke_text(self,ctx,text,stroke,ctm,cs,color,alpha,cp):
            self.paint('text',alpha)
            if stroke.linewidth==0:self.unsupported.add('PDF hairline stroked text')
        def clip_stroke_path(self,ctx,path,stroke,ctm,scissor):
            if stroke.linewidth==0:self.unsupported.add('PDF hairline stroke clipping')
        def clip_stroke_text(self,ctx,text,stroke,ctm,scissor):
            if stroke.linewidth==0:self.unsupported.add('PDF hairline text stroke clipping')
        def fill_image(self,ctx,image,ctm,alpha,cp):
            self.paint('image',alpha,extra=bool(image.mask or image.use_colorkey));self.record_image(image,cp)
        def fill_image_mask(self,ctx,image,ctm,colorspace,color,alpha,cp):
            self.paint('image',alpha);self.record_image(image,cp)
        def clip_image_mask(self,ctx,image,ctm,scissor):self.record_image(image,m.FzColorParams().internal())
        def begin_group(self,ctx,area,cs,isolated,knockout,blendmode,alpha):
            # MuPDF's SVG writer drops PDF /I. Conversely, Canvas must isolate
            # an SVG opacity group even if the PDF Form was nonisolated.
            # Either boundary changes a descendant blend's source backdrop.
            # Only the contents phase's outermost group is the page backdrop.
            # Annotation/widget appearances start their own top-level groups
            # after that group closes, even with empty page contents.
            if blendmode!=0 and any(group['backdrop_boundary'] for group in self.groups):
                self.unsupported.add('PDF transparency group with backdrop-dependent blend')
            for group in self.groups:
                if group['knockout']:group['complex']=True;group['transparent']|=alpha!=1 or blendmode!=0
            is_page_backdrop=self.phase=='contents' and not self.groups
            self.groups.append(dict(knockout=bool(knockout),backdrop_boundary=not is_page_backdrop and (bool(isolated) or alpha!=1),paints=[],transparent=False,complex=False))
        def end_group(self,ctx):
            if not self.groups:return
            group=self.groups.pop();paints=group['paints']
            if not group['knockout'] or not group['transparent']:return
            if not group['complex'] and len(paints)==1:return
            if not group['complex'] and len(paints)==2 and paints[0][0]=='fill' and paints[1][0]=='stroke' and paints[0][1]==paints[1][1]:self.knockout_pairs.append((paints[0][1],paints[1][2]));return
            self.unsupported.add('PDF complex knockout transparency group')
        def begin_mask(self,*args):
            for group in self.groups:
                if group['knockout']:group['complex']=True;group['transparent']=True
        def end_mask(self,ctx,transfer):
            if transfer:self.unsupported.add('PDF softmask transfer function')
        def fill_shade(self,*args):self.unsupported.add('PDF shading')
        def begin_tile(self,*args):self.unsupported.add('PDF tiling pattern');return 0
    probe=Probe()
    # These phases are the same content -> annotations -> widgets order used
    # by fz_run_page. Reuse one device and all paint-occurrence counters; only
    # the root-group exemption needs to know which phase owns a new group.
    for phase in ['contents','annots','widgets']:
        probe.phase=phase
        getattr(m,'fz_run_page_'+phase)(page.this,probe,m.FzMatrix(),m.FzCookie())
    if probe.failure:raise SceneUnsupported('source image inspection failed') from probe.failure
    if probe.unsupported:raise SceneUnsupported('source paint unsupported without raster degradation: '+', '.join(sorted(probe.unsupported)))
    return probe.sampling,probe.knockout_pairs,probe.pair_counts,probe.hairlines,probe.stroke_counts

def export_scene(source,layout):
    """Serialize only pages placed by the already-decided existing layout."""
    pymupdf=_load_pymupdf()
    if not isinstance(source,bytes):raise TypeError('source must be original PDF bytes')
    for key in ['width_mm','height_mm','placements','crop_margins_mm']:
        if key not in layout:raise SceneUnsupported(f'layout: missing {key}')
    def layout_number(value,context):
        if isinstance(value,bool) or not isinstance(value,(int,float)):raise SceneUnsupported(f'layout: {context} must be a number')
        return finite(value,f'layout: {context}')
    if layout_number(layout['width_mm'],'width_mm')<=0 or layout_number(layout['height_mm'],'height_mm')<=0:raise SceneUnsupported('layout: invalid size')
    placements=copy.deepcopy(layout['placements']);crop=copy.deepcopy(layout['crop_margins_mm'])
    if not isinstance(placements,list) or not all(isinstance(p,dict) for p in placements):raise SceneUnsupported('layout: placements must be objects')
    if not isinstance(crop,dict):raise SceneUnsupported('layout: crop must be an object')
    if set(crop)!={'left','right','top','bottom'} or any(layout_number(x,'crop')<0 for x in crop.values()):raise SceneUnsupported('layout: invalid crop')
    if not placements:raise SceneUnsupported('layout: no placements')
    pages={};assets={};seen_indexes=set();seen_rects=[]
    # Roundoff allowance only: the current layout is computed in binary floats.
    tolerance_mm=1e-6
    with pymupdf.open(stream=source,filetype='pdf') as document:
        for placement in placements:
            if set(placement)-{'page_index','x_mm','y_mm','width_mm','height_mm','source_x_mm','source_y_mm'}:raise SceneUnsupported('layout: unknown placement fields')
            index=placement.get('page_index')
            if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(document):raise SceneUnsupported('layout: invalid page_index')
            if index in seen_indexes:raise SceneUnsupported('layout: duplicate page placement')
            seen_indexes.add(index)
            for name in ['x_mm','y_mm','width_mm','height_mm']:
                if name not in placement or layout_number(placement[name],name)<0:raise SceneUnsupported(f'layout: invalid {name}')
            if placement['width_mm']==0 or placement['height_mm']==0:raise SceneUnsupported('layout: empty placement')
            for name in ['source_x_mm','source_y_mm']:
                if name in placement and layout_number(placement[name],name)<0:raise SceneUnsupported(f'layout: invalid {name}')
            left,top,width,height=(layout_number(placement[k],k) for k in ['x_mm','y_mm','width_mm','height_mm'])
            if left+width>layout['width_mm']+tolerance_mm or top+height>layout['height_mm']+tolerance_mm:raise SceneUnsupported('layout: placement outside scene')
            rect=(left,top,left+width,top+height)
            if any(min(rect[2],r[2])-max(rect[0],r[0])>tolerance_mm and min(rect[3],r[3])-max(rect[1],r[1])>tolerance_mm for r in seen_rects):raise SceneUnsupported('layout: overlapping placements')
            seen_rects.append(rect)
            page=document[index]
            sx=finite(placement.get('source_x_mm',crop['left']));sy=finite(placement.get('source_y_mm',crop['top']))
            if sx+width>page.rect.width/PT_PER_MM+tolerance_mm or sy+height>page.rect.height/PT_PER_MM+tolerance_mm:raise SceneUnsupported(f'layout: page {index} source crop outside page')
            if str(index) not in pages:
                try:
                    sampling,knockouts,pair_counts,hairlines,stroke_counts=inspect_scene_semantics(page)
                    pages[str(index)]=export_svg(page.get_svg_image(text_as_path=True),assets,sampling)
                    apply_knockout_pairs(pages[str(index)],knockouts,pair_counts)
                    apply_hairlines(pages[str(index)],hairlines,stroke_counts)
                except SceneUnsupported as e:raise SceneUnsupported(f'page {index}: {e}') from e
    used_assets=set()
    def collect(node):
        if node['type']=='image':used_assets.add(node['assetId'])
        for child in node.get('children',[]):collect(child)
        if node.get('mask'):collect(node['mask']['tree'])
        for level in node.get('imageLevels',[]):collect(level['tree'])
    for page in pages.values():collect(page['tree'])
    assets={key:value for key,value in assets.items() if key in used_assets}
    return dict(version=1,units='pt',mm_per_pt=25.4/72,source_sha256=hashlib.sha256(source).hexdigest(),width_mm=layout['width_mm'],height_mm=layout['height_mm'],placements=placements,crop_margins_mm=crop,pages=pages,assets=assets)

def export_scene_bundle(source,layout,output_path,maximum_bytes):
    """Exclusive deterministic bundle, sourced only from this original PDF.

    maximum_bytes is the caller's existing derived-artifact quota, also bounding
    total unpacked bytes. No remote references, filenames or scripts are copied.
    """
    if isinstance(maximum_bytes,bool) or not isinstance(maximum_bytes,int) or maximum_bytes<=0:raise ValueError('maximum_bytes must be positive integer')
    output=check_output_path(output_path,[])
    scene=export_scene(source,layout);files={}
    for asset in scene['assets'].values():
        payload=base64.b64decode(asset.pop('data').split(',',1)[1],validate=True)
        files[asset['path']]=payload
    serialized=json.dumps(scene,ensure_ascii=False,separators=(',',':'),allow_nan=False,sort_keys=True).encode()
    files['scene.json']=serialized
    unpacked=sum(map(len,files.values()))
    if unpacked>maximum_bytes:raise SceneUnsupported('scene bundle exceeds derived byte budget')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name in sorted(files):
            entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED;entry.external_attr=0o100600<<16;entry.create_system=3
            archive.writestr(entry,files[name],compresslevel=6)
    payload=buffer.getvalue()
    if len(payload)>maximum_bytes:raise SceneUnsupported('scene bundle exceeds derived byte budget')
    try:
        with output.open('xb') as destination:destination.write(payload)
    except FileExistsError:raise
    except OSError:
        output.unlink(missing_ok=True);raise
    return dict(sha256=hashlib.sha256(payload).hexdigest(),bytes=len(payload),unpacked_bytes=unpacked,scene_sha256=hashlib.sha256(serialized).hexdigest(),scene_bytes=len(serialized),source_sha256=scene['source_sha256'],assets=scene['assets'],page_count=len(scene['pages']),width_mm=scene['width_mm'],height_mm=scene['height_mm'])

def check_output_path(output, inputs):
    """Reject aliases and all existing outputs before doing expensive work.

    The later exclusive create remains the authoritative race-safe check; a
    symlink or hardlink appearing after this preflight cannot overwrite input.
    """
    output=Path(output)
    for source in map(Path,inputs):
        aliases=output.resolve()==source.resolve()
        if not aliases and output.exists() and source.exists():
            aliases=output.samefile(source)
        if aliases:raise SceneUnsupported(f'output aliases input: {source}')
    if output.exists() or output.is_symlink():raise SceneUnsupported(f'output already exists: {output}')
    return output

def write_scene_output(scene, output, inputs):
    """Create one new result; never truncate or replace an existing file."""
    output=check_output_path(output,inputs)
    serialized=json.dumps(scene,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    # O_EXCL also rejects symlinks and hardlinks created after the preflight.
    with output.open('x',encoding='utf-8') as destination:destination.write(serialized)
    return len(serialized.encode('utf-8'))

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--pdf',required=True);parser.add_argument('--layout',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    try:
        output=check_output_path(args.output,[args.pdf,args.layout])
        source=Path(args.pdf).read_bytes();layout=json.loads(Path(args.layout).read_text());scene=export_scene(source,layout)
        size=write_scene_output(scene,output,[args.pdf,args.layout])
    except (OSError,ValueError) as error:parser.error(str(error))
    print(json.dumps(dict(output=str(output),pages=len(scene['pages']),placements=len(scene['placements']),bytes=size,source_sha256=scene['source_sha256'])))
if __name__=='__main__':main()
