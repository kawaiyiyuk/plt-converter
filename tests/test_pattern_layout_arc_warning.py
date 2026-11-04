"""Curve approximation is an opt-in, acknowledged pattern-layout risk."""
import copy
import unittest
from app.services.plt_parser import parse_plt
from app.services.pattern_layout_api import recognize_source, export_project, PatternLayoutExportError

RECT='IN;PU0,0;PD10000,0,10000,10000,0,10000,0,0;PU;'


class PatternArcWarningTests(unittest.TestCase):
    def test_drawn_aa_ar_ci_warn_and_require_explicit_export_confirmation(self):
        for curve in ('PU4000,2000;PD;AA2000,2000,90;PU;',
                      'PU4000,2000;PD;AR-2000,0,90;PU;',
                      'PU4000,4000;CI1000;'):
            with self.subTest(curve=curve):
                raw=(RECT+curve).encode();result=recognize_source(raw)
                warnings=result['source']['warnings']
                self.assertTrue(any('导出使用折线近似' in warning and '0.05mm' in warning and '不代表投影总误差' in warning for warning in warnings))
                piece=next(p for p in result['pieces'] if p['fragments'])
                document={'schema_version':1,'source_sha256':result['source']['sha256'],'candidates':[piece],
                          'placements':[{'piece_id':piece['id'],'x':0,'y':0}]}
                with self.assertRaises(PatternLayoutExportError) as rejected:export_project(raw,document)
                self.assertEqual(rejected.exception.code,'source_warning_confirmation_required')
                document['acknowledged_source_warnings']=warnings
                output=export_project(raw,document)
                self.assertTrue(output);self.assertNotIn(b'AA',output);self.assertNotIn(b'AR',output);self.assertNotIn(b'CI',output)

    def test_no_drawn_curve_does_not_report_approximation(self):
        for tail in ('','PU4000,2000;AA2000,2000,90;',
                     'PU4000,2000;AR-2000,0,90;',
                     'SP0;PU4000,2000;PD;AA2000,2000,90;CI1000;',
                     'PU4000,2000;PD;AA2000,2000,0;PU;',
                     'PU2000,2000;PD;AA2000,2000,90;PU;',
                     'PU500,500;CI0;',
                     'PU500,500;LBAA AR CI\x03'):
            with self.subTest(tail=tail):
                result=recognize_source((RECT+tail).encode())
                self.assertFalse(any('导出使用折线近似' in warning for warning in result['source']['warnings']))

    def test_linear_pattern_export_does_not_require_an_arc_confirmation(self):
        raw=RECT.encode();result=recognize_source(raw);piece=result['pieces'][0]
        document={'schema_version':1,'source_sha256':result['source']['sha256'],'candidates':[piece],
                  'placements':[{'piece_id':piece['id'],'x':0,'y':0}]}
        self.assertTrue(export_project(raw,document))

    def test_legacy_parser_projection_defaults_and_linear_geometry_are_unchanged(self):
        raw=(RECT+'PU4000,2000;PD;AA2000,2000,90;PU;CI1000;').encode()
        legacy=parse_plt(raw,projection=True,max_chord_error_mm=.05)
        explicit_disabled=parse_plt(raw,projection=True,max_chord_error_mm=.05,report_curve_approximation=False)
        enabled=parse_plt(raw,projection=True,max_chord_error_mm=.05,report_curve_approximation=True)
        self.assertEqual(legacy,explicit_disabled)
        enabled_without_warning=copy.deepcopy(enabled)
        enabled_without_warning['warnings']=[w for w in enabled_without_warning['warnings'] if '导出使用折线近似' not in w]
        self.assertEqual(enabled_without_warning,legacy)
        self.assertEqual(parse_plt(RECT.encode(),projection=True,max_chord_error_mm=.05),
                         parse_plt(RECT.encode(),projection=True,max_chord_error_mm=.05,report_curve_approximation=True))
