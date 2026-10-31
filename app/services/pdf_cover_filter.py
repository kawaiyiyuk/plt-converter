"""Detect leading raster cover pages without relying on page dimensions.

Engineering heuristic, not PDF semantics: a cover is a leading page dominated
by a single image and almost no visible vector ink. The vector body after it
must be substantial, so pure scans are never auto-filtered.
"""

MAX_LEADING_PAGES = 3
MIN_IMAGE_COVERAGE_RATIO = 0.2
# Path items, not drawings: PyMuPDF merges same-styled paths into one drawing,
# so a real pattern tile can be a single drawing with hundreds of segments.
MIN_VECTOR_ITEMS = 3
MIN_BODY_PAGES = 3
# Bounds the extra get_drawings() work before complexity validation runs.
MAX_BODY_SCAN_PAGES = 12


def detect_leading_cover_pages(document, fitz):
    """Return a leading run of original page indexes that look like covers."""
    page_count = int(document.page_count)
    if page_count < 4:
        return []

    covers = []
    for index in range(min(MAX_LEADING_PAGES, page_count)):
        page = document.load_page(index)
        if not _is_raster_cover_page(page, fitz):
            break
        covers.append(index)

    if not covers or len(covers) >= page_count:
        return []

    body_pages = 0
    body_start = covers[-1] + 1
    for index in range(body_start, min(page_count, body_start + MAX_BODY_SCAN_PAGES)):
        page = document.load_page(index)
        if _visible_item_count(page) >= MIN_VECTOR_ITEMS:
            body_pages += 1
            if body_pages >= MIN_BODY_PAGES:
                return covers
    return []


def _is_raster_cover_page(page, fitz):
    page_rect = page.rect
    area = page_rect.get_area()
    if area <= 0:
        return False
    rotation_matrix = getattr(page, 'rotation_matrix', None)
    largest_image_area = 0.0
    for image in page.get_image_info():
        rect = fitz.Rect(image['bbox'])
        if rotation_matrix is not None:
            rect = rect * rotation_matrix
        largest_image_area = max(largest_image_area, (rect & page_rect).get_area())
    if largest_image_area < area * MIN_IMAGE_COVERAGE_RATIO:
        return False
    return _visible_item_count(page) < MIN_VECTOR_ITEMS


def _visible_item_count(page):
    """Count path items of drawings with visible stroke or non-white, non-transparent fill."""
    count = 0
    for drawing in page.get_drawings():
        stroke = drawing.get('color')
        fill = drawing.get('fill')
        has_stroke = (
            stroke is not None
            and any(float(channel) < 1 for channel in stroke)
            and float(drawing.get('stroke_opacity', 1)) > 0
        )
        has_fill = (
            fill is not None
            and any(float(channel) < 1 for channel in fill)
            and float(drawing.get('fill_opacity', 1)) > 0
        )
        if has_stroke or has_fill:
            count += len(drawing.get('items') or ())
    return count
