"""One-click PDF repagination through the existing PDF->PLT->PDF pipeline."""

from .pdf_layout_optimizer import optimize_pdf_layout
from .pdf_renderer import render_pdf
from .pdf_to_plt import MAX_PDF_PAGES, convert_pdf_to_plt, load_fitz, open_pdf_document
from .plt_parser import parse_plt


SUPPORTED_PAPER_SIZES = {'A0', 'A1', 'A2', 'A3', 'A4'}
SINGLE_PAGE_PAPER_SIZE = 'SINGLE'


def pdf_page_count(source):
    if not source:
        raise ValueError('PDF 文件为空')
    fitz = load_fitz()
    document = open_pdf_document(source, fitz)
    try:
        page_count = int(document.page_count)
        if page_count == 0:
            raise ValueError('PDF 没有可用页面')
        if page_count > MAX_PDF_PAGES:
            raise ValueError(f'PDF 页数过多，最多支持 {MAX_PDF_PAGES} 页')
        return page_count
    finally:
        document.close()


def automatic_layout_suggestion(source, source_page_count=None):
    page_count = source_page_count or pdf_page_count(source)
    if page_count == 1:
        return {
            'confidence': 'high',
            'rows': 1,
            'columns': 1,
            'order': 'row',
            'page_slots': [0],
            'crop_margins_mm': {'top': 0, 'right': 0, 'bottom': 0, 'left': 0},
            'output_rotation': 0,
            'source': 'single_page',
        }
    suggestion = optimize_pdf_layout(source)
    if suggestion.get('confidence') not in {'high', 'medium'}:
        reason = suggestion.get('reason') or '接缝证据不足'
        raise ValueError(
            f'无法可靠识别原 PDF 的页面拼版：{reason}。'
            '请改用“PDF 转 PLT”工作台手动调整后再转换。'
        )
    return suggestion


def source_conversion_options(suggestion):
    crop = suggestion.get('crop_margins_mm') or {}
    page_slots = list(suggestion.get('page_slots') or [])
    enabled_pages = [int(index) for index in page_slots if index is not None]
    return {
        'units_per_inch': 1016,
        'rows': int(suggestion['rows']),
        'columns': int(suggestion['columns']),
        'order': suggestion.get('order', 'row'),
        'page_slots': page_slots,
        'enabled_pages': enabled_pages,
        'crop_left_mm': float(crop.get('left', 0)),
        'crop_right_mm': float(crop.get('right', 0)),
        'crop_top_mm': float(crop.get('top', 0)),
        'crop_bottom_mm': float(crop.get('bottom', 0)),
        'output_rotation': int(suggestion.get('output_rotation', 0)),
        'line_width_mm': 0.265,
    }


def target_conversion_options(paper_size):
    normalized_paper_size = str(paper_size or '').upper()
    single_page = normalized_paper_size == SINGLE_PAGE_PAPER_SIZE
    if not single_page and normalized_paper_size not in SUPPORTED_PAPER_SIZES:
        raise ValueError('目标纸张只支持整张单页或 A0、A1、A2、A3、A4')
    return {
        'units_per_inch': 1016,
        # A0 is used only by the shared renderer's tile math. SINGLE always
        # sizes the actual PDF page from the complete drawing, not from A0.
        'paper_size': 'A0' if single_page else normalized_paper_size,
        'orientation': 'auto',
        'margin_mm': 10,
        'line_width_mm': 0.265,
        'single_page_output': single_page,
        'enforce_single_page_limit': single_page,
        'show_page_number': not single_page,
    }


def convert_pdf_to_pdf(source, options=None):
    options = options or {}
    target_options = target_conversion_options(options.get('paper_size'))
    source_page_count = int(options.get('source_page_count') or pdf_page_count(source))
    if source_page_count < 1 or source_page_count > MAX_PDF_PAGES:
        raise ValueError(f'PDF 页数必须在 1 到 {MAX_PDF_PAGES} 页之间')
    suggestion = automatic_layout_suggestion(source, source_page_count)
    plt, source_layout = convert_pdf_to_plt(
        source,
        source_conversion_options(suggestion),
    )
    drawing = parse_plt(plt, 1016)
    pdf, target_layout = render_pdf(drawing, target_options)
    result = {
        'paper_size': str(options.get('paper_size') or '').upper(),
        'source_page_count': source_page_count,
        'output_page_count': int(target_layout.get('page_count', 0)),
        'source_layout': source_layout,
        'target_layout': target_layout,
        'layout_source': suggestion.get('source'),
        'layout_confidence': suggestion.get('confidence'),
    }
    if target_layout.get('page_width_pt') and target_layout.get('page_height_pt'):
        result['output_width_mm'] = round(target_layout['page_width_pt'] * 25.4 / 72, 2)
        result['output_height_mm'] = round(target_layout['page_height_pt'] * 25.4 / 72, 2)
    return pdf, result
