"""Export transforms -> real PLT projection child/PDF/tile -> fabric origin."""
import hashlib
import unittest
import pymupdf
from app.services.pattern_layout_api import export_project
import test_projection_plt as fixtures


class LayoutOriginTests(unittest.TestCase):
    setUp=fixtures.ProjectionPltTests.setUp
    identity=fixtures.ProjectionPltTests.identity
    upload=fixtures.ProjectionPltTests.upload
    tile=fixtures.ProjectionPltTests.tile

    def test_export_projection_roundtrip_with_negative_positions_mirror_rotation_and_margins(self):
        source=b'IN;PU0,0;PD400,0,400,800,0,800,0,0;PU;'
        fragments=[{'path_id':'shape-1','segment_index':i,'t0':0,'t1':1} for i in range(4)]
        for x,y,rotation,mirror in [(20,30,0,False),(-10,15,0,True),(15,-10,90,False),(-15,-20,180,True)]:
            with self.subTest(x=x,y=y,rotation=rotation,mirror=mirror):
                doc={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),'candidates':[{'id':'p','fragments':fragments}],
                     'placements':[{'piece_id':'p','x':x,'y':y,'rotation_deg':rotation,'mirror_x':mirror}]}
                raw=export_project(source,doc)
                from app.services.plt_parser import parse_plt
                parsed=parse_plt(raw,projection=True)
                points=[p for shape in parsed['shapes'] for p in shape.get('points',[])]
                expected_x=min(p['x']/40 for p in points);expected_y=min(-p['y']/40 for p in points)
                response=self.upload(raw,'layout.plt');self.assertEqual(response.status_code,201,response.json)
                bounds=response.json['source_bounds_mm']
                self.assertAlmostEqual(bounds['x'],expected_x,places=6)
                self.assertAlmostEqual(bounds['y'],expected_y,places=6)
                identifier=response.json['document_id']
                summary=self.client.get(fixtures.BASE+'/'+identifier,headers={'Authorization':'Bearer fixture'}).json
                self.assertEqual(summary['source_bounds_mm'],bounds)
                # Actual projection PDF vector points plus the published crop
                # offset reconstruct every exported stroke in fabric coordinates.
                with pymupdf.open(self.ledger.source_path(identifier)) as pdf:
                    page=pdf[0]; actual=[]
                    for drawing in page.get_drawings():
                        for segment in drawing['items']:
                            if segment[0]=='l':
                                for p in segment[1:3]: actual.append((p.x*25.4/72+bounds['x'],p.y*25.4/72+bounds['y']))
                            elif segment[0]=='qu':
                                quad=segment[1]
                                for p in (quad.ul,quad.ur,quad.ll,quad.lr): actual.append((p.x*25.4/72+bounds['x'],p.y*25.4/72+bounds['y']))
                            elif segment[0]=='re':
                                rectangle=segment[1]
                                for p in (rectangle.tl,rectangle.tr,rectangle.bl,rectangle.br): actual.append((p.x*25.4/72+bounds['x'],p.y*25.4/72+bounds['y']))
                    for p in points:
                        self.assertTrue(any(abs(a-p['x']/40)<.001 and abs(b+p['y']/40)<.001 for a,b in actual), (actual,p))
                tile=self.tile(identifier,width_mm=bounds['w'],height_mm=bounds['h'],width_px=round(bounds['w']*10),height_px=round(bounds['h']*10))
                self.assertEqual(tile.status_code,200,tile.json if tile.is_json else None)
                pix=pymupdf.Pixmap(tile.data)
                self.assertLess(sum(pix.pixel(pix.width//2,0)),720)
