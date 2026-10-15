"""PDF operations, imported only after projection_bootstrap has applied limits."""
import json
import math
from pathlib import Path


def inspect(source):
    from .services.pdf_to_plt import (MAX_PDF_PAGES, load_fitz, open_pdf_document,
                                     read_pdf_layout_metadata_from_document, validate_pdf_complexity)
    from .projection import PT_PER_MM
    fitz = load_fitz()
    with open_pdf_document(source, fitz) as document:
        if document.is_encrypted or document.needs_pass or document.metadata.get('encryption'):
            raise ValueError('不支持加密 PDF，请先解除加密')
        if not 1 <= document.page_count <= MAX_PDF_PAGES:
            raise ValueError(f'PDF 页数必须在 1 至 {MAX_PDF_PAGES} 页之间，不会截断页面')
        validate_pdf_complexity(document)
        pages = []
        for index in range(document.page_count):
            rect = document[index].rect
            dimensions = [rect.width / PT_PER_MM, rect.height / PT_PER_MM]
            if any(not math.isfinite(v) or v <= 0 for v in dimensions):
                raise ValueError(f'第 {index+1} 页 PDF 页面尺寸无效')
            pages.append({'index':index,'width_mm':dimensions[0],'height_mm':dimensions[1]})
        try:
            embedded = read_pdf_layout_metadata_from_document(document)
        except (ValueError, TypeError, OverflowError):
            embedded = None
    return {'pages':pages,'page_count':len(pages),'width_mm':pages[0]['width_mm'],
            'height_mm':pages[0]['height_mm'],'requires_metadata_choice':bool(embedded),
            'embedded_layout':embedded}


def execute(spec):
    output = Path(spec['output'])
    try:
        source = Path(spec['source']).read_bytes()
        if spec['operation'] == 'parse':
            result = inspect(source)
        elif spec['operation'] == 'plt':
            from .services.plt_parser import parse_plt
            from .services.pdf_renderer import render_projection_pdf
            # Leave headroom under 0.05mm for the PDF writer's 0.001pt rounding.
            document = parse_plt(source, strict=True, max_chord_error_mm=0.049)
            pdf = render_projection_pdf(document)
            maximum = spec['parameters']['derived_max_bytes']
            if len(pdf) > maximum:
                raise ValueError(f'PLT 投影派生 PDF 超过 {maximum // (1024 * 1024)}MiB 文件预算')
            # The bounded lease owns this staging file until atomic promotion.
            derived = output.parent / 'derived.pdf'
            with derived.open('xb') as file:
                file.write(pdf)
                file.flush()
                __import__('os').fsync(file.fileno())
            result = inspect(pdf)
            result.update(source_format='plt', rendering_warnings=document.get('warnings', []))
        elif spec['operation'] == 'tile':
            from .projection import render_tile
            p = spec['parameters']
            result = render_tile(source, p['values'], p['page_index'], p.get('layout'))
        elif spec['operation'] == 'analysis':
            from .projection_layout import analyze_source
            p = spec['parameters']
            result = analyze_source(source, p['mode'], p.get('embedded'))
        else:
            raise ValueError('unknown projection operation')
        output.write_bytes(result if isinstance(result, bytes) else json.dumps(result, allow_nan=False).encode())
    except (ValueError, RuntimeError) as error:
        # ValueError is a deliberate, user-facing validation message; RuntimeError
        # (including MuPDF internals) must never be shown to the client verbatim.
        (output.parent / 'error.json').write_text(json.dumps(
            {'error': str(error)[:8192],
             'public': isinstance(error, ValueError) and not isinstance(error, (UnicodeError, json.JSONDecodeError))}))
