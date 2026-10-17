"""Projection PDF contract, including COPY assembly over an immutable old base.

For an offline container with the candidate mounted read-only at /candidate:
  python /candidate/tests/test_projection_base_compat.py --base-root /app --candidate-root /candidate
No daemon, network, worker or Redis access is performed by this test.
"""
import argparse
import ast
import hashlib
import inspect
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


LEGACY_PROOF = None
LEGACY_FILES = ('app/billing.py', 'app/job_queue.py', 'app/routes.py', 'app/tasks.py',
                'app/services/__init__.py', 'app/services/converter.py',
                'app/services/number_tokens.py', 'app/services/pdf_layout_optimizer.py',
                'app/services/pdf_metadata.py', 'app/services/pdf_renderer.py',
                'app/services/pdf_to_pdf.py', 'app/services/pdf_to_plt.py',
                'app/services/plt_metadata.py', 'app/services/plt_parser.py', 'run.py', 'worker.py')
EXISTING_HELPER_OVERLAYS = ('plt_parser.py', 'pdf_renderer.py', 'number_tokens.py', 'pdf_metadata.py')


def numbered_pdf():
    import pymupdf as fitz
    with fitz.open() as document:
        for row, column, shift in [(1, 2, 0), (2, 1, 5), (1, 1, 0), (2, 2, 5)]:
            page = document.new_page(width=260, height=360)
            left, top, right, bottom = 20, 20 + shift, 240, 320 + shift
            for a, b in [((left, top), (right, top)), ((left, bottom), (right, bottom)),
                         ((left, top), (left, bottom)), ((right, top), (right, bottom))]:
                page.draw_line(a, b, color=(.2, .6, .8), dashes='[3 3]')
            page.draw_line((0, top + 120), (260, top + 120))
            page.draw_line((left + 90, 0), (left + 90, 360))
            page.insert_text((100, top + 280), f'{row} {column}', fontsize=12)
        return document.tobytes()


class ProjectionBaseCompatibilityTests(unittest.TestCase):
    def test_deployment_context_contains_every_docker_copy_source(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / 'scripts/deploy-projector.sh').read_text()
        files_line = next(line for line in script.splitlines() if line.startswith('files = '))
        allowed = set(ast.literal_eval(files_line.partition('=')[2].strip()))
        for line in (root / 'Dockerfile.projection').read_text().splitlines():
            if not line.startswith('COPY '):
                continue
            args = [word for word in shlex.split(line)[1:] if not word.startswith('--')]
            for name in args[:-1]:
                source = root / name
                sources = list(source.rglob('*')) if source.is_dir() else [source]
                for path in sources:
                    if path.is_file():
                        self.assertIn(str(path.relative_to(root)), allowed)

    def test_selection_and_manual_layout_keep_original_page_indexes(self):
        from app.projection_page_filter import normalize_selection
        from app.projection_child import inspect as inspect_pdf
        from app.projection_layout import build_layout
        self.assertEqual(normalize_selection('[3,1]', 4), [1, 3])
        for selection in ([], [True], [0, 0], [4], ['0']):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                normalize_selection(selection, 4)
        parsed = inspect_pdf(numbered_pdf())
        self.assertEqual(parsed['page_count'], 4)
        layout = build_layout(parsed['pages'], 1, 2, 'row', {}, [3, 1],
                              included_page_indexes=[1, 3])
        self.assertEqual([item['page_index'] for item in layout['placements']], [3, 1])

    def test_auto_numbered_and_fallback_analysis_and_tile(self):
        from app.projection_child import inspect as inspect_pdf
        from app.projection_layout import analyze_source, build_layout
        from app.projection import render_tile
        source = numbered_pdf()
        suggestion = analyze_source(source, 'current', None, [0, 1, 2, 3])
        self.assertEqual(suggestion['source'], 'numbered_vector_frames')
        self.assertEqual(suggestion['confidence'], 'medium')
        self.assertEqual(suggestion['perfect_seams'], 4)
        self.assertEqual(suggestion['page_slots'], [2, 0, 1, 3])
        pages = inspect_pdf(source)['pages']
        layout = build_layout(pages, suggestion['rows'], suggestion['columns'],
                              suggestion['order'], suggestion['crop_margins_mm'],
                              suggestion['page_slots'], included_page_indexes=[0, 1, 2, 3],
                              source_rects_mm=suggestion['source_rects_mm'])
        tile = render_tile(source, {'x_mm': 0, 'y_mm': 0,
                           'width_mm': layout['width_mm'], 'height_mm': layout['height_mm'],
                           'width_px': 440, 'height_px': 600}, layout=layout)
        self.assertTrue(tile.startswith(b'\x89PNG\r\n\x1a\n'))
        fallback = analyze_source(source, 'current', None, [2])
        self.assertEqual(fallback['page_slots'], [2])

    def test_private_modules_are_separate_and_have_selection_capabilities(self):
        from app.projection_pdf_runtime import engine, pdf
        from app.services import pdf_layout_optimizer as legacy
        self.assertIsNot(engine, legacy)
        self.assertTrue(engine.__name__.startswith('app._projection_pdf.'))
        self.assertEqual(pdf.__name__, 'app._projection_pdf.pdf_to_plt')
        self.assertEqual(engine.load_fitz.__module__, pdf.__name__)
        self.assertEqual(pdf.decode_pdf_layout_metadata.__module__, 'app._projection_pdf.pdf_metadata')
        for name in ('normalize_pdf_page_indexes', '_blank_seam_conflicts',
                     'effective_pdf_layout_max_blank_cells'):
            self.assertTrue(callable(getattr(engine, name)))
        self.assertIn('page_indexes', inspect.signature(engine.optimize_pdf_layout).parameters)
        self.assertIn('page_indexes', inspect.signature(engine._extract_vector_segments).parameters)
        packaged = Path(engine.__file__).parent.name == '_projection_pdf'
        if LEGACY_PROOF:
            self.assertTrue(packaged)
            self.assertEqual(list(inspect.signature(legacy.optimize_pdf_layout).parameters), ['source'])
            self.assertEqual(list(inspect.signature(legacy._extract_vector_segments).parameters),
                             ['document', 'fitz', 'ignore_internal_guides'])
            root = Path(legacy.__file__).parents[2]
            for filename, digest in LEGACY_PROOF.items():
                self.assertEqual(hashlib.sha256((root / filename).read_bytes()).hexdigest(), digest, filename)
        else:
            self.assertFalse(packaged)
            self.assertEqual(Path(engine.__file__), Path(legacy.__file__))

    def test_image_missing_or_incomplete_private_runtime_never_uses_legacy(self):
        import app.projection_pdf_runtime as runtime
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / 'app'
                package.mkdir()
                (package / '__init__.py').write_text('')
                shutil.copy2(runtime.__file__, package / 'projection_pdf_runtime.py')
                (package / 'services').mkdir()
                # Even a services path containing the correct current files is
                # forbidden as an image fallback; only checkout mode permits it.
                for filename in ('pdf_layout_optimizer.py', 'pdf_to_plt.py', 'pdf_metadata.py'):
                    shutil.copy2(Path(runtime.pdf.__file__).parent / filename, package / 'services' / filename)
                if incomplete:
                    (root / 'Dockerfile.projection').write_text('# checkout marker')
                    (package / '_projection_pdf').mkdir()
                result = subprocess.run([sys.executable, '-c', 'import app.projection_pdf_runtime'],
                                        cwd=root, capture_output=True, text=True,
                                        env={**__import__('os').environ, 'PYTHONPATH': str(root)})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('projection private PDF runtime is ' + ('incomplete' if incomplete else 'missing'),
                              result.stderr)


def run_assembled(base_root, candidate_root):
    """Apply only Dockerfile COPY operations over actual old-base Python sources."""
    with tempfile.TemporaryDirectory(prefix='projection-base-compat-') as directory:
        staged = Path(directory)
        shutil.copytree(base_root / 'app', staged / 'app')
        for name in ('run.py', 'worker.py', 'run_projection.py', 'projection_bootstrap.py'):
            if (base_root / name).exists():
                shutil.copy2(base_root / name, staged / name)
        legacy = {name: hashlib.sha256((staged / name).read_bytes()).hexdigest() for name in LEGACY_FILES}
        prior_pr = {name: (hashlib.sha256((candidate_root / name).read_bytes()).hexdigest()
                          if name.startswith('app/services/') and Path(name).name in EXISTING_HELPER_OVERLAYS
                          else digest) for name, digest in legacy.items()}
        for line in (candidate_root / 'Dockerfile.projection').read_text().splitlines():
            if not line.startswith('COPY '):
                continue
            words = shlex.split(line)
            if not words or words[0] != 'COPY':
                continue
            args = [word for word in words[1:] if not word.startswith('--')]
            destination = args[-1]
            if destination.startswith('/'):
                continue  # The volume ownership seed is unrelated to Python.
            target = staged / destination
            target.mkdir(parents=True, exist_ok=True)
            for name in args[:-1]:
                shutil.copy2(candidate_root / name, target / Path(name).name)
        # The four helper overlays already exist in the PR being repaired.
        # This fix may add private sources, but must change none of the legacy
        # sixteen files relative to that original COPY assembly.
        for name, digest in prior_pr.items():
            if hashlib.sha256((staged / name).read_bytes()).hexdigest() != digest:
                raise AssertionError(f'COPY overwrote legacy module: {name}')
        proof = staged / 'legacy-proof.json'
        proof.write_text(json.dumps(prior_pr, sort_keys=True))
        print(json.dumps({'legacy_base_sha': legacy, 'legacy_original_pr_sha': prior_pr,
                          'legacy_after_fix_sha': prior_pr, 'legacy_files_unchanged_since_original_pr': len(prior_pr),
                          'existing_base_overlay_changes': [name for name in legacy if legacy[name] != prior_pr[name]]},
                         sort_keys=True), flush=True)
        return subprocess.run([sys.executable, str(Path(__file__).resolve()),
                               '--legacy-proof', str(proof)], cwd=staged,
                              env={**__import__('os').environ, 'PYTHONPATH': str(staged)}).returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-root', type=Path)
    parser.add_argument('--candidate-root', type=Path)
    parser.add_argument('--legacy-proof', type=Path)
    args, rest = parser.parse_known_args()
    if args.base_root:
        if args.candidate_root is None:
            parser.error('--candidate-root is required with --base-root')
        raise SystemExit(run_assembled(args.base_root, args.candidate_root))
    if args.legacy_proof:
        LEGACY_PROOF = json.loads(args.legacy_proof.read_text())
    unittest.main(argv=[sys.argv[0], *rest], verbosity=2)
