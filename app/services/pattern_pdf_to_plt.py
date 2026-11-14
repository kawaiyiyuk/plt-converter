"""Convert a reliably tiled pattern PDF into a seam-welded HPGL/PLT file."""

from .pdf_seam_weld import remove_grid_frames, weld_seam_paths
from .pdf_to_pdf import (
    automatic_layout_suggestion,
    pdf_page_count,
    source_conversion_options,
)
from .pdf_to_plt import (
    DEFAULT_PDF_TO_PLT_LINE_WIDTH_MM,
    convert_pdf_to_plt,
    load_fitz,
    open_pdf_document,
    read_pdf_layout_metadata,
    serialize_hpgl,
    validate_generated_plt,
)
from .pdf_whole_sheet import (
    WholeSheetTooComplex,
    has_big_piece,
    read_whole_sheet,
    whole_sheet_layout_result,
)


UNITS_PER_MM = 1016 / 25.4
CROP_DELTA_CANDIDATES = (0, -0.05, 0.05, -0.1, 0.1, -0.15, 0.15)
LAYOUT_SOURCE_NAMES = {
    'embedded_metadata': 'embedded',
    'single_page': 'single_page',
    'vector_seams': 'vector_seams',
}
SINGLE_CELL_SEAM_REPAIR = {
    'crop_delta_mm': 0,
    'frames_removed': 0,
    'welded_joints': 0,
}


class PatternPdfRejected(ValueError):
    """A pattern PDF that cannot be converted automatically."""


def image_coverage_ratio(page, fitz):
    """Return how much of the page area is covered by placed raster images."""
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height
    if page_area <= 0:
        return 0.0
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    covered = 0.0
    for info in infos:
        bbox = info.get('bbox')
        if not bbox:
            continue
        try:
            visible = fitz.Rect(bbox) & page_rect
        except Exception:
            continue
        if not visible.is_empty:
            covered += visible.width * visible.height
    return min(1.0, covered / page_area)


def _reject_scanned_single_page(source):
    fitz = load_fitz()
    document = open_pdf_document(source, fitz)
    try:
        page = document.load_page(0)
        drawings = page.get_drawings()
        if not drawings:
            raise PatternPdfRejected('这份 PDF 是扫描件或图片，无法自动转成 PLT')
        vector_item_count = sum(len(drawing.get('items') or []) for drawing in drawings)
        if image_coverage_ratio(page, fitz) >= 0.6 and vector_item_count < 200:
            raise PatternPdfRejected('这份 PDF 是扫描件或图片，无法自动转成 PLT')
    finally:
        document.close()


def _layout_rejection_message(error):
    message = str(error).strip()
    reason = message.split('请改用', 1)[0].strip()
    if '拼版：' in reason:
        reason = reason.split('拼版：', 1)[1]
    reason = reason.rstrip('。').strip()
    return f'无法可靠识别这份 PDF 的拼版：{reason}'


def _crop_options(options, delta):
    result = dict(options)
    for side in ('left', 'right', 'top', 'bottom'):
        value = float(result.get(f'crop_{side}_mm', 0)) + delta
        if value < 0:
            return None
        result[f'crop_{side}_mm'] = value
    return result


def _assert_vector_output(layout):
    if layout.get('raster_fallback'):
        raise PatternPdfRejected('这份 PDF 含扫描页或图片页，无法自动转成 PLT')


def _convert_with(source, options, path_postprocessor):
    plt, layout = convert_pdf_to_plt(
        source,
        options,
        path_postprocessor=path_postprocessor,
    )
    _assert_vector_output(layout)
    return plt, layout


def _weld_only_postprocessor(units_per_mm, stats):
    def postprocess(paths, grid):
        welded, merged = weld_seam_paths(paths, grid, units_per_mm, match_mm=0.1)
        stats['welded_joints'] = merged
        return welded

    return postprocess


def _repair_postprocessor(units_per_mm, match_mm, stats):
    def postprocess(paths, grid):
        kept, frames_removed = remove_grid_frames(paths, grid, units_per_mm)
        welded, merged = weld_seam_paths(kept, grid, units_per_mm, match_mm=match_mm)
        stats['frames_removed'] = frames_removed
        stats['welded_joints'] = merged
        return welded

    return postprocess


def _select_crop_delta(source, options):
    best_delta = 0
    best_merged = None
    for delta in CROP_DELTA_CANDIDATES:
        candidate_options = _crop_options(options, delta)
        if candidate_options is None:
            continue
        stats = {'frames_removed': 0, 'welded_joints': 0}
        postprocessor = _repair_postprocessor(UNITS_PER_MM, 0.02, stats)
        _convert_with(source, candidate_options, postprocessor)
        merged = stats['welded_joints']
        if best_merged is None or merged > best_merged:
            best_delta = delta
            best_merged = merged
    return best_delta


def _convert_whole_sheet(sheet, options):
    if not has_big_piece(sheet['shapes'], UNITS_PER_MM):
        raise PatternPdfRejected('这份 PDF 没有识别到完整的大块裁片，无法自动转换')
    line_width_mm = max(
        0.03,
        float(options.get('line_width_mm', DEFAULT_PDF_TO_PLT_LINE_WIDTH_MM)),
    )
    validate_generated_plt(sheet['shapes'], 1016)
    plt = serialize_hpgl(sheet['shapes'], 1016, line_width_mm)
    return plt, {
        'layout': whole_sheet_layout_result(sheet),
        'layout_source': 'whole_sheet',
        'layout_confidence': 'high',
        'skipped_page_indexes': [],
        'seam_repair': {
            'crop_delta_mm': 0,
            'frames_removed': sheet['fixed_paths_removed'],
            'welded_joints': 0,
        },
        'review_required': True,
    }


def convert_pattern_pdf_to_plt(source, options=None):
    """Convert one reliably tiled pattern PDF and return (plt_bytes, result)."""
    options = options or {}
    try:
        whole_sheet = read_whole_sheet(source)
    except WholeSheetTooComplex as error:
        raise PatternPdfRejected(str(error)) from error
    if whole_sheet is not None:
        return _convert_whole_sheet(whole_sheet, options)

    page_count = pdf_page_count(source)
    embedded = read_pdf_layout_metadata(source)
    if embedded and embedded.get('complete_layout'):
        metadata_mode = 'original'
    elif embedded:
        metadata_mode = 'current'
    else:
        metadata_mode = None

    if page_count == 1 and not embedded:
        _reject_scanned_single_page(source)

    try:
        suggestion = automatic_layout_suggestion(source, page_count, metadata_mode)
    except ValueError as error:
        raise PatternPdfRejected(_layout_rejection_message(error)) from error
    if suggestion.get('confidence') != 'high':
        raise PatternPdfRejected('这份 PDF 的拼版把握不够，无法自动转换')

    layout_source = LAYOUT_SOURCE_NAMES.get(
        suggestion.get('source'), suggestion.get('source')
    )
    conversion_options = {
        **source_conversion_options(suggestion),
        'metadata_mode': metadata_mode,
    }
    rows = int(conversion_options['rows'])
    columns = int(conversion_options['columns'])
    skipped_page_indexes = list(suggestion.get('skipped_page_indexes') or [])

    def build_result(layout, seam_repair):
        return {
            'layout': layout,
            'layout_source': layout_source,
            'layout_confidence': suggestion.get('confidence'),
            'skipped_page_indexes': skipped_page_indexes,
            'seam_repair': seam_repair,
            'review_required': True,
        }

    if rows * columns == 1:
        plt, layout = _convert_with(source, conversion_options, None)
        return plt, build_result(layout, dict(SINGLE_CELL_SEAM_REPAIR))

    if layout_source == 'embedded':
        stats = {'frames_removed': 0, 'welded_joints': 0}
        postprocessor = _weld_only_postprocessor(UNITS_PER_MM, stats)
        plt, layout = _convert_with(source, conversion_options, postprocessor)
        seam_repair = {
            'crop_delta_mm': 0,
            'frames_removed': 0,
            'welded_joints': stats['welded_joints'],
        }
        return plt, build_result(layout, seam_repair)

    best_delta = 0
    if layout_source == 'vector_seams':
        best_delta = _select_crop_delta(source, conversion_options)
        stats = {'frames_removed': 0, 'welded_joints': 0}
        postprocessor = _repair_postprocessor(UNITS_PER_MM, 0.1, stats)
        final_options = _crop_options(conversion_options, best_delta)
        plt, layout = _convert_with(source, final_options, postprocessor)
        seam_repair = {
            'crop_delta_mm': best_delta,
            'frames_removed': stats['frames_removed'],
            'welded_joints': stats['welded_joints'],
        }
        return plt, build_result(layout, seam_repair)

    raise PatternPdfRejected('这份 PDF 的拼版把握不够，无法自动转换')
