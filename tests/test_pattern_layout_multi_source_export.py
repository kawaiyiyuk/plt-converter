import hashlib
import unittest

from app.services.pattern_layout_api import (
    PatternLayoutExportError,
    export_multi_source_project,
    recognize_source,
)
from app.services.pattern_layout_fingerprint import (
    export_runtime_version,
    recognition_code_version,
)
from app.services.plt_parser import parse_plt


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _source_entry(sid, raw, warnings=None, meta=None):
    entry = {'id': sid, 'sha256': _sha256(raw)}
    if warnings is not None:
        entry['acknowledged_source_warnings'] = list(warnings)
    if meta:
        entry.update(meta)
    return entry


def _compact_candidate(sid, candidate, new_id=None):
    return {
        'id': new_id or f'{sid}::{candidate["id"]}',
        'source_id': sid,
        'text_ids': list(candidate.get('text_ids', [])),
        'fragments': [
            {
                'path_id': f['path_id'],
                'segment_index': f['segment_index'],
                't0': f['t0'],
                't1': f['t1'],
            }
            for f in candidate.get('fragments', [])
        ],
    }


def _find_rect_piece(rec):
    return next(p for p in rec['pieces'] if p.get('fragments'))


class MultiSourceExportTests(unittest.TestCase):
    def test_merges_two_sources_with_colliding_ids_lt2_rotate_mirror_copy_and_labels(self):
        # 两份 PLT 都只有一个 shape-1 与同一编号 P-1 / text-1
        src_a = b'IN;SP1;PU0,0;PD2000,0,2000,1000,0,1000,0,0;PU400,400;LBFRONT\x03PU;'
        src_b = b'IN;SP2;PU0,0;LT2,4,1;PD1600,0,1600,1200,0,1200,0,0;LT;PU200,200;LBSLEEVE\x03PU;'
        rec_a = recognize_source(src_a)
        rec_b = recognize_source(src_b)
        piece_a = _find_rect_piece(rec_a)
        piece_b = _find_rect_piece(rec_b)

        doc = {
            'schema_version': 1,
            'sources': [
                _source_entry('src-1', src_a, warnings=rec_a['source']['warnings']),
                _source_entry('src-2', src_b, warnings=rec_b['source']['warnings']),
            ],
            'candidates': [
                _compact_candidate('src-1', piece_a, 'src-1::P-1'),
                _compact_candidate('src-2', piece_b, 'src-2::P-1'),
            ],
            'placements': [
                # 源 A 第 1 份：放到 x=10mm, y=20mm（带原生文字 FRONT）
                {'id': 'pl-a1', 'piece_id': 'src-1::P-1', 'anchor': {'x': 0, 'y': 0}, 'x': 10, 'y': 20, 'rotation_deg': 0, 'mirror_x': False},
                # 源 B：放到 x=100mm, y=10mm（含 LT2 虚线与原生文字 SLEEVE）
                {'id': 'pl-b1', 'piece_id': 'src-2::P-1', 'anchor': {'x': 0, 'y': 0}, 'x': 100, 'y': 10, 'rotation_deg': 0, 'mirror_x': False},
                # 源 A 第 2 份副本（去掉 text_ids 的候选副本测试旋转 + 镜像）
            ],
            'fabric': {'width_mm': 1500, 'length_mm': 2000},
        }
        # 追加无文字的旋转镜像副本
        cand_a_notext = _compact_candidate('src-1', piece_a, 'src-1::P-1-rot')
        cand_a_notext['text_ids'] = []
        doc['candidates'].append(cand_a_notext)
        doc['placements'].append({
            'id': 'pl-a2',
            'piece_id': 'src-1::P-1-rot',
            'anchor': {'x': 0, 'y': 0},
            'x': 80,
            'y': 80,
            'rotation_deg': 90,
            'mirror_x': True,
        })

        exported = export_multi_source_project({'src-1': src_a, 'src-2': src_b}, doc)
        parsed = parse_plt(exported, projection=True)
        labels = [s for s in parsed['shapes'] if s['type'] == 'text']
        self.assertEqual([l['text'] for l in labels], ['FRONT', 'SLEEVE'])
        # FRONT 原图在 (400,400) units 即 (10,15) mm（因为 max_y=1000，y_mm=(1000-400)/40=15）
        # 放置到 (10,20) mm -> 世界坐标 (20,35) mm -> HPGL (800, -1400)
        self.assertAlmostEqual(labels[0]['point']['x'], 800.0)
        self.assertAlmostEqual(labels[0]['point']['y'], -1400.0)
        # SLEEVE 原图 (200,200) units, max_y=1200 -> 局部 (5, 25) mm, 放置到 (100,10) -> (105,35) mm -> (4200, -1400)
        self.assertAlmostEqual(labels[1]['point']['x'], 4200.0)
        self.assertAlmostEqual(labels[1]['point']['y'], -1400.0)
        # 检查旋转镜像副本的线段确实写入并且 LT2 虚线产生了多段
        paths = [s for s in parsed['shapes'] if s['type'] == 'path']
        self.assertGreater(len(paths), 4)

    def test_multi_source_supports_on_fold_top_bottom_and_unfolded(self):
        # 源 A：50mm x 25mm 矩形；源 B：40mm x 20mm 矩形
        src_a = b'IN;PU0,0;PD2000,0,2000,1000,0,1000,0,0;PU;'
        src_b = b'IN;PU0,0;PD1600,0,1600,800,0,800,0,0;PU;'
        rec_a = recognize_source(src_a)
        rec_b = recognize_source(src_b)
        piece_a = _find_rect_piece(rec_a)
        piece_b = _find_rect_piece(rec_b)

        # 找到各片的上边界直边（y=0）用于折边
        top_a = [
            {'path_id': f['path_id'], 'segment_index': f['segment_index'], 't0': f['t0'], 't1': f['t1']}
            for f in piece_a['fragments']
            if abs(f['a'][1]) < 1e-6 and abs(f['b'][1]) < 1e-6
        ]
        top_b = [
            {'path_id': f['path_id'], 'segment_index': f['segment_index'], 't0': f['t0'], 't1': f['t1']}
            for f in piece_b['fragments']
            if abs(f['a'][1]) < 1e-6 and abs(f['b'][1]) < 1e-6
        ]
        self.assertTrue(top_a and top_b)

        # 1) folded + top + on_fold
        doc_top = {
            'schema_version': 2,
            'sources': [_source_entry('src-1', src_a), _source_entry('src-2', src_b)],
            'candidates': [_compact_candidate('src-1', piece_a), _compact_candidate('src-2', piece_b)],
            'placements': [
                {
                    'id': 'p-a',
                    'piece_id': 'src-1::' + piece_a['id'],
                    'anchor': {'x': 0, 'y': 0},
                    'x': 10,
                    'y': 0,
                    'rotation_deg': 0,
                    'mirror_x': False,
                    'cut_mode': 'on_fold',
                    'fold_edge': {'a': [0, 0], 'b': [50, 0], 'fragments': top_a},
                },
                {
                    'id': 'p-b',
                    'piece_id': 'src-2::' + piece_b['id'],
                    'anchor': {'x': 0, 'y': 0},
                    'x': 80,
                    'y': 0,
                    'rotation_deg': 0,
                    'mirror_x': False,
                    'cut_mode': 'on_fold',
                    'fold_edge': {'a': [0, 0], 'b': [40, 0], 'fragments': top_b},
                },
            ],
            'fabric': {'width_mm': 200, 'length_mm': 300, 'lay_mode': 'folded', 'fold_side': 'top'},
        }
        out_top = export_multi_source_project({'src-1': src_a, 'src-2': src_b}, doc_top)
        parsed_top = parse_plt(out_top, projection=True)
        # 折边边不导出，两片矩形各剩 3 条边（共 6 个线段，且无贴在折线 y=0 的水平边）
        top_paths = [s for s in parsed_top['shapes'] if s['type'] == 'path']
        top_segments = [(a, b) for s in top_paths for a, b in zip(s['points'], s['points'][1:])]
        self.assertEqual(len(top_segments), 6)
        self.assertFalse(any(abs(a['y']) < 1e-6 and abs(b['y']) < 1e-6 for a, b in top_segments))

        # 2) schema_version 3 + folded + bottom + on_fold
        doc_bottom = {
            **doc_top,
            'schema_version': 3,
            'placements': [
                {**doc_top['placements'][0], 'rotation_deg': 180, 'y': 100},
                {**doc_top['placements'][1], 'rotation_deg': 180, 'y': 100},
            ],
            'fabric': {'width_mm': 200, 'length_mm': 300, 'lay_mode': 'folded', 'fold_side': 'bottom'},
        }
        out_bottom = export_multi_source_project({'src-1': src_a, 'src-2': src_b}, doc_bottom)
        self.assertTrue(out_bottom)

        # 3) schema_version 3 + single + unfolded（展开成双倍对称轮廓）
        doc_unfold = {
            'schema_version': 3,
            'sources': [_source_entry('src-1', src_a), _source_entry('src-2', src_b)],
            'candidates': [_compact_candidate('src-1', piece_a), _compact_candidate('src-2', piece_b)],
            'placements': [
                {
                    'id': 'p-a',
                    'piece_id': 'src-1::' + piece_a['id'],
                    'anchor': {'x': 0, 'y': 0},
                    'x': 10,
                    'y': 50,
                    'rotation_deg': 0,
                    'mirror_x': False,
                    'cut_mode': 'unfolded',
                    'fold_edge': {'a': [0, 0], 'b': [50, 0], 'fragments': top_a},
                },
                {
                    'id': 'p-b',
                    'piece_id': 'src-2::' + piece_b['id'],
                    'anchor': {'x': 0, 'y': 0},
                    'x': 80,
                    'y': 50,
                    'rotation_deg': 0,
                    'mirror_x': False,
                    'cut_mode': 'normal',
                },
            ],
            'fabric': {'width_mm': 200, 'length_mm': 300, 'lay_mode': 'single', 'fold_side': 'bottom'},
        }
        out_unfold = export_multi_source_project({'src-1': src_a, 'src-2': src_b}, doc_unfold)
        parsed_unfold = parse_plt(out_unfold, projection=True)
        ys = [pt['y'] for s in parsed_unfold['shapes'] if s['type'] == 'path' for pt in s['points']]
        # p-a 原高 25mm 放于 y=50mm，沿 y=0 展开后应涵盖 y=25mm..75mm -> HPGL y=-3000..-1000
        self.assertAlmostEqual(min(ys), -3000.0)
        self.assertAlmostEqual(max(ys), -1000.0)

    def test_unused_source_skips_parsing_when_sha_valid(self):
        src_a = b'IN;PU0,0;PD800,0,800,800,0,800,0,0;PU;'
        # src_b 带有不可解析的语法，但如果未被任何 placement 引用，只在校验 SHA 后跳过解析
        src_b = b'INVALID_UNREFERENCED_PAYLOAD'
        rec_a = recognize_source(src_a)
        piece_a = _find_rect_piece(rec_a)
        doc = {
            'schema_version': 1,
            'sources': [_source_entry('src-1', src_a), _source_entry('src-2', src_b)],
            'candidates': [_compact_candidate('src-1', piece_a)],
            'placements': [{'id': 'p1', 'piece_id': 'src-1::' + piece_a['id'], 'anchor': {'x': 0, 'y': 0}, 'x': 5, 'y': 5, 'rotation_deg': 0, 'mirror_x': False}],
            'fabric': {'width_mm': 500, 'length_mm': 500},
        }
        out = export_multi_source_project({'src-1': src_a, 'src-2': src_b}, doc)
        self.assertTrue(out)

    def test_error_branches_match_single_source_export_codes(self):
        src_a = b'IN;PU0,0;PD800,0,800,800,0,800,0,0;PU100,100;LBHI\x03PU;'
        src_b = b'IN;PU0,0;PD400,0,400,400,0,400,0,0;PU;PG;'
        rec_a = recognize_source(src_a)
        rec_b = recognize_source(src_b)
        piece_a = _find_rect_piece(rec_a)
        piece_b = _find_rect_piece(rec_b)

        base_doc = {
            'schema_version': 1,
            'sources': [_source_entry('src-1', src_a), _source_entry('src-2', src_b, warnings=rec_b['source']['warnings'])],
            'candidates': [_compact_candidate('src-1', piece_a), _compact_candidate('src-2', piece_b)],
            'placements': [
                {'id': 'p1', 'piece_id': 'src-1::' + piece_a['id'], 'anchor': {'x': 0, 'y': 0}, 'x': 0, 'y': 0, 'rotation_deg': 0, 'mirror_x': False},
                {'id': 'p2', 'piece_id': 'src-2::' + piece_b['id'], 'anchor': {'x': 0, 'y': 0}, 'x': 30, 'y': 0, 'rotation_deg': 0, 'mirror_x': False},
            ],
            'fabric': {'width_mm': 500, 'length_mm': 500},
        }

        # 缺失源
        with self.assertRaises(PatternLayoutExportError) as ctx:
            export_multi_source_project({'src-1': src_a}, base_doc)
        self.assertEqual(ctx.exception.code, 'invalid_source_identity')

        # SHA 不匹配
        bad_sha_doc = {**base_doc, 'sources': [_source_entry('src-1', b'other'), base_doc['sources'][1]]}
        with self.assertRaises(PatternLayoutExportError) as ctx:
            export_multi_source_project({'src-1': src_a, 'src-2': src_b}, bad_sha_doc)
        self.assertEqual(ctx.exception.code, 'invalid_source_identity')

        # 重复 source id
        dup_src_doc = {**base_doc, 'sources': [base_doc['sources'][0], base_doc['sources'][0]]}
        with self.assertRaises(PatternLayoutExportError) as ctx:
            export_multi_source_project({'src-1': src_a, 'src-2': src_b}, dup_src_doc)
        self.assertEqual(ctx.exception.code, 'invalid_source_identity')

        # 候选缺少 source_id
        missing_sid_cand = dict(base_doc['candidates'][0])
        del missing_sid_cand['source_id']
        with self.assertRaises(PatternLayoutExportError) as ctx:
            export_multi_source_project({'src-1': src_a, 'src-2': src_b}, {**base_doc, 'candidates': [missing_sid_cand, base_doc['candidates'][1]]})
        self.assertEqual(ctx.exception.code, 'invalid_source_fragment')

        # 无选中片段
        with self.assertRaises(PatternLayoutExportError) as ctx:
            export_multi_source_project({'src-1': src_a, 'src-2': src_b}, {**base_doc, 'placements': []})
        self.assertEqual(ctx.exception.code, 'no_selected_fragments')

        # 未确认 warning
        unack_doc = {**base_doc, 'sources': [base_doc['sources'][0], _source_entry('src-2', src_b)]}
        with self.assertRaises(PatternLayoutExportError) as ctx:
            export_multi_source_project({'src-1': src_a, 'src-2': src_b}, unack_doc)
        self.assertEqual(ctx.exception.code, 'source_warning_confirmation_required')

        # 带文字裁片旋转
        rot_doc = {**base_doc, 'placements': [{**base_doc['placements'][0], 'rotation_deg': 90}, base_doc['placements'][1]]}
        with self.assertRaises(PatternLayoutExportError) as ctx:
            export_multi_source_project({'src-1': src_a, 'src-2': src_b}, rot_doc)
        self.assertEqual(ctx.exception.code, 'label_pose_not_faithful')

    def test_fingerprints_unchanged(self):
        self.assertRegex(recognition_code_version(), r'^[a-f0-9]{64}$')
        self.assertEqual(
            export_runtime_version(),
            '0f383501478e60b221fac3d5ac113cba7662b410d34f9c59c20927988646868f',
        )


if __name__ == '__main__':
    unittest.main()
