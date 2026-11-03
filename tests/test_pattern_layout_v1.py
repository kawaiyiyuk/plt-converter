import unittest
from app.services.plt_parser import parse_plt

class PatternLayoutTests(unittest.TestCase):
    def test_line_style_is_public_segment_data(self):
        data = parse_plt(b'IN;SP2;PU0,0;LT2,4,1;PD400,0;LT;PD400,400;PU;', projection=True)
        styles = [s for p in data['shapes'] for s in p.get('source_segment_styles', [])]
        self.assertEqual([s['lt_argument'] for s in styles], ['2,4,1', ''])

    def test_legacy_projection_still_warns_and_strict_rejects_lt(self):
        source=b'IN;PU0,0;LT2,4,1;PD400,0;PU;'
        parsed=parse_plt(source,projection=True)
        self.assertTrue(any('LT' in warning and '已跳过未支持' in warning for warning in parsed['warnings']))
        with self.assertRaisesRegex(ValueError,'LT'):
            parse_plt(source,strict=True)
        from app.services.pdf_renderer import render_projection_pdf
        import pymupdf
        pdf=render_projection_pdf(parsed)
        with pymupdf.open(stream=pdf,filetype='pdf') as document:
            drawings=document[0].get_drawings()
            self.assertEqual(len(drawings),1)
            self.assertFalse(drawings[0].get('dashes','').startswith('[ 2'))

    def test_real_rectangle_and_nested_size_remain_complete_alternatives(self):
        from app.services.pattern_layout_api import recognize_source
        source=b'IN;PU0,0;PD8000,0,8000,8000,0,8000,0,0;PU400,400;PD2400,400,2400,2400,400,2400,400,400;PU4000,400;PD6000,400,6000,2400,4000,2400,4000,400;PU800,800;PD2000,800,2000,2000,800,2000,800,800;PU1000,1000;PD1800,1800;PU;'
        result=recognize_source(source)
        independent=[p for p in result['pieces'] if p['id'].startswith('S-')]
        self.assertEqual(len(independent),4)
        self.assertTrue(all(p['content_complete_for_region'] and not p['semantic_piece_verified'] for p in independent))
        inner=next(p for p in independent if p['source_closed_path_ids']==['shape-4'])
        self.assertTrue(any(f['path_id']=='shape-5' for f in inner['fragments']))
        outer=next(p for p in independent if p['source_closed_path_ids']==['shape-1'])
        self.assertTrue(any(f['path_id']=='shape-5' for f in outer['fragments']))

    def test_coincident_real_contours_are_both_restorable(self):
        from app.services.pattern_layout_api import recognize_source
        source=b'IN;PU0,0;PD4000,0,4000,4000,0,4000,0,0;PU0,0;PD4000,0,4000,4000,0,4000,0,0;PU800,800;PD2400,2400;PU;'
        result=recognize_source(source)
        independent=[p for p in result['pieces'] if p['id'].startswith('S-')]
        self.assertEqual(len(independent),2)
        for piece in independent:
            self.assertTrue(any(f['path_id']=='shape-3' for f in piece['fragments']))
            self.assertEqual({f['path_id'] for f in piece['fragments']},{'shape-1','shape-2','shape-3'})

    def test_unknown_source_warnings_require_exact_explicit_acknowledgement(self):
        from app.services.pattern_layout_api import export_project, recognize_source
        import hashlib
        src=b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;PG;'
        result=recognize_source(src);piece=result['pieces'][0]
        doc={'schema_version':1,'source_sha256':hashlib.sha256(src).hexdigest(),'candidates':[piece],'placements':[{'piece_id':piece['id'],'x':0,'y':0}]}
        with self.assertRaisesRegex(ValueError,'待确认警告'): export_project(src,doc)
        doc['acknowledged_source_warnings']=result['source']['warnings']
        self.assertTrue(export_project(src,doc))

    def test_native_label_translation_is_verified_and_pose_limitation_is_explicit(self):
        from app.services.pattern_layout_api import recognize_source,export_project
        import hashlib
        source=b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU40,40;LBABC\x03'
        result=recognize_source(source);piece=result['pieces'][0]
        doc={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),'candidates':[piece],'placements':[{'piece_id':piece['id'],'x':20,'y':30}]}
        output=export_project(source,doc)
        labels=[p for p in parse_plt(output,projection=True)['shapes'] if p['type']=='text']
        self.assertEqual(labels[0]['text'],'ABC')
        self.assertAlmostEqual(labels[0]['point']['x'],840)
        doc['placements'][0]['rotation_deg']=90
        with self.assertRaisesRegex(ValueError,'设备文字'):export_project(source,doc)

    def test_runtime_identity_covers_native_and_python_numeric_runtime(self):
        from app.services.pattern_layout_api import runtime_version
        from unittest.mock import patch
        baseline=runtime_version()
        for name,value in [('shapely.geos_version_string','different-geos'),('numpy.__version__','different-numpy'),('platform.machine','different-architecture')]:
            context=patch(name,return_value=value) if name=='platform.machine' else patch(name,value)
            with context:self.assertNotEqual(runtime_version(),baseline)

    def test_long_continuous_dashed_path_keeps_linear_source_phase(self):
        from app.services.pattern_layout_api import export_project
        import hashlib,time,math
        count=4000
        source=('IN;PU0,0;LT2,4,1;PD'+','.join(str(v) for i in range(1,count+1) for v in (i*40,0))+';PU;').encode()
        parsed=parse_plt(source,projection=True)
        doc={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),
             'candidates':[{'id':'c','fragments':[{'path_id':'shape-1','segment_index':i,'t0':0,'t1':1} for i in range(count)]}],
             'placements':[{'piece_id':'c','x':0,'y':0}], 'acknowledged_source_warnings':parsed['warnings']}
        start=time.monotonic();out=export_project(source,doc);elapsed=time.monotonic()-start
        ink=parse_plt(out,projection=True)
        total=sum(math.dist((a['x'],a['y']),(b['x'],b['y'])) for shape in ink['shapes'] for a,b in zip(shape.get('points',[]),shape.get('points',[])[1:]))
        self.assertAlmostEqual(total,count*40/2)
        self.assertLess(elapsed,5.)

    def test_hpgl_decimal_never_emits_exponents_or_collapses_tiny_segment(self):
        from app.services.pattern_layout_api import export_project,hpgl_decimal
        import hashlib
        self.assertEqual(hpgl_decimal(-0.),'0')
        for value in (1e-300,1e-7,1e30,-1e30):
            text=hpgl_decimal(value)
            self.assertNotIn('e',text.lower());self.assertEqual(float(text),value)
        source=b'IN;PU0,0;PD0.0000001,0,40,40;PU;'
        doc={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),'candidates':[{'id':'c','fragments':[{'path_id':'shape-1','segment_index':0,'t0':0,'t1':1}]}],
             'placements':[{'piece_id':'c','x':0,'y':0,'anchor':{'x':0,'y':1}}]}
        result=export_project(source,doc)
        self.assertNotIn(b'e',result.lower())
        parsed=parse_plt(result,projection=True)
        points=next(s['points'] for s in parsed['shapes'] if s['type']=='path')
        self.assertGreater(points[1]['x']-points[0]['x'],0)
        self.assertAlmostEqual(points[1]['x']-points[0]['x'],1e-7,places=18)

    def test_endpoint_closed_self_intersecting_source_keeps_every_original_segment_and_inner_line(self):
        from app.services.pattern_layout_api import recognize_source
        source=b'IN;PU0,0;PD4000,4000,0,4000,4000,0,0,0;PU400,200;PD800,200;PU;'
        result=recognize_source(source)
        candidate=next(p for p in result['pieces'] if p['id']=='S-shape-1')
        refs={(f['segment_index'],f['t0'],f['t1']) for f in candidate['fragments'] if f['path_id']=='shape-1'}
        self.assertEqual(refs,{(i,0.,1.) for i in range(4)})
        self.assertTrue(any(f['path_id']=='shape-2' for f in candidate['fragments']))
        self.assertTrue(any(i['code']=='source_self_intersection_region_ambiguous' for i in candidate['issues']))
        self.assertFalse(candidate['semantic_piece_verified'])

    def test_pen_width_change_inside_continuous_source_path_is_preserved(self):
        from app.services.pattern_layout_api import export_project
        import hashlib
        source=b'IN;PU0,0;PW0.2;PD400,0;PW0.4;PD800,0;PU;'
        doc={'schema_version':1,'source_sha256':hashlib.sha256(source).hexdigest(),'candidates':[{'id':'c','fragments':[{'path_id':'shape-1','segment_index':i,'t0':0,'t1':1} for i in range(2)]}],
             'placements':[{'piece_id':'c','x':0,'y':0}]}
        out=export_project(source,doc)
        widths=[st['line_width_units'] for p in parse_plt(out,projection=True)['shapes'] for st in p.get('source_segment_styles',[])]
        self.assertEqual(widths,[.2,.4])

    def test_rectangle_alternative_preserves_original_and_real_rectangle_choice(self):
        from app.services.pattern_layout_api import recognize_source
        from app.services.pattern_layout_variants import add_rectangle_alternatives
        import copy
        source=b'IN;PU0,0;PD8000,0,8000,8000,0,8000,0,0;PU1000,1000;PD3000,1000,3000,3000,1000,3000,1000,1000;PU1400,1400;PD2000,2000;PU;'
        result=recognize_source(source);original=copy.deepcopy(result['pieces']);add_rectangle_alternatives(result)
        self.assertEqual(result['pieces'][:len(original)],original)
        variants=[p for p in result['pieces'] if p.get('parent_candidate_id')=='S-shape-1']
        self.assertTrue(variants)
        self.assertTrue(any(p['id']=='S-shape-2' for p in result['pieces']))
        parent=next(p for p in original if p['id']=='S-shape-1')
        for variant in variants:
            excluded=set(variant['excluded_rectangle']['source_path_ids'])
            self.assertEqual(variant['fragments'],[f for f in parent['fragments'] if f['path_id'] not in excluded])
            self.assertIn('可能移除本片真实标记',variant['variant_label_cn'])
            self.assertTrue(any(f['path_id']=='shape-3' for f in variant['fragments']))

    def test_export_only_source_change_does_not_invalidate_recognition_fingerprint(self):
        import tempfile,shutil
        from pathlib import Path
        from unittest.mock import patch
        from app.services import pattern_layout_fingerprint as fingerprint
        with tempfile.TemporaryDirectory() as folder:
            copied=Path(folder)/'services';shutil.copytree(fingerprint.ROOT,copied,ignore=shutil.ignore_patterns('__pycache__'))
            with patch.object(fingerprint,'ROOT',copied):
                before_rec=fingerprint.recognition_runtime_version();before_export=fingerprint.export_runtime_version()
                api=copied/'pattern_layout_api.py';text=api.read_text()
                api.write_text(text.replace('def export_project(source_bytes, document):','def export_project(source_bytes, document):\n    # export-only sensitivity probe'))
                self.assertEqual(fingerprint.recognition_runtime_version(),before_rec)
                self.assertNotEqual(fingerprint.export_runtime_version(),before_export)

    def test_mult_path_real_rectangle_never_gets_a_remove_own_outline_variant(self):
        from app.services.pattern_layout_api import recognize_source
        from app.services.pattern_layout_variants import add_rectangle_alternatives
        source=b'IN;PU0,0;PD4000,0;PU4000,0;PD4000,4000;PU4000,4000;PD0,4000;PU0,4000;PD0,0;PU;'
        result=recognize_source(source);original_count=len(result['pieces']);add_rectangle_alternatives(result)
        self.assertEqual(len(result['pieces']),original_count)

    def test_export_uses_only_exact_fragment_and_transforms(self):
        from app.services.pattern_layout_api import export_project
        import hashlib
        src=b'IN;SP3;PU0,0;PD400,0,400,400,0,400,0,0;PU;'
        doc={'schema_version':1,'source_sha256':hashlib.sha256(src).hexdigest(),
             'candidates':[{'id':'c','fragments':[{'path_id':'shape-1','segment_index':0,'t0':.25,'t1':.75}]}],
             'placements':[{'id':'a','piece_id':'c','anchor':{'x':0,'y':0},'x':20,'y':30,'rotation_deg':90,'mirror_x':False}]}
        out=export_project(src,doc)
        parsed=parse_plt(out,projection=True)
        paths=[s for s in parsed['shapes'] if s['type']=='path']
        self.assertEqual(len(paths),1)
        self.assertEqual(len(paths[0]['points']),2)
        self.assertEqual(paths[0]['pen'],3)
        self.assertAlmostEqual(abs(paths[0]['points'][1]['y']-paths[0]['points'][0]['y']),200)
        doc['source_sha256']='x'
        with self.assertRaises(ValueError): export_project(src,doc)

    def test_dashed_middle_fragment_and_mirror_keep_source_phase(self):
        from app.services.pattern_layout_api import export_project
        import hashlib
        src=b'IN;PU0,0;LT2,4,1;PD400,0;PU;'
        doc={'schema_version':1,'source_sha256':hashlib.sha256(src).hexdigest(),
             'candidates':[{'id':'c','fragments':[{'path_id':'shape-1','segment_index':0,'t0':.25,'t1':.75}]}],
             'placements':[{'piece_id':'c','x':0,'y':0}]}
        doc['acknowledged_source_warnings']=parse_plt(src,projection=True)['warnings']
        doc['placements'][0]['mirror_x']=True
        output=export_project(src,doc)
        parsed=parse_plt(output,projection=True)
        paths=[p for p in parsed['shapes'] if p['type']=='path']
        # Source 2.5..7.5mm cuts a 4mm period: only 4..6mm is ink.
        self.assertEqual(len(paths),1)
        self.assertAlmostEqual(paths[0]['points'][0]['x'],-160.)
        self.assertAlmostEqual(paths[0]['points'][1]['x'],-240.)
        self.assertEqual(paths[0]['source_segment_styles'][0]['lt_argument'],'2,4,1')

if __name__ == '__main__': unittest.main()
