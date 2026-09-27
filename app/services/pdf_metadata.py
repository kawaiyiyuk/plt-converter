import base64
import json
import math
import os


ROUNDTRIP_METADATA_PREFIX = 'FENGRENJIYI_PLT_LAYOUT_V1:'
MAX_GRID_SIZE = 24


def encode_pdf_layout_metadata(metadata):
    normalized = normalize_pdf_layout_metadata(metadata)
    payload = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('ascii')
    encoded = base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')
    return ROUNDTRIP_METADATA_PREFIX + encoded


def decode_pdf_layout_metadata(subject):
    value = str(subject or '')
    if not value.startswith(ROUNDTRIP_METADATA_PREFIX):
        return None
    encoded = value[len(ROUNDTRIP_METADATA_PREFIX):]
    try:
        padding = '=' * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode((encoded + padding).encode('ascii'))
        decoded = json.loads(payload.decode('ascii'))
        return normalize_pdf_layout_metadata(decoded)
    except (ValueError, TypeError, OverflowError, UnicodeError, json.JSONDecodeError):
        return None


def normalize_pdf_layout_metadata(metadata):
    if not isinstance(metadata, dict) or _metadata_int(metadata.get('version', 0), 'version') != 1:
        raise ValueError('PDF 排版元数据版本无效')
    rows = _positive_grid_int(metadata.get('rows'), 'rows')
    columns = _positive_grid_int(metadata.get('columns'), 'columns')
    order = metadata.get('order')
    if order not in {'row', 'column'}:
        raise ValueError('PDF 排版元数据顺序无效')
    capacity = rows * columns
    raw_slots = metadata.get('page_slots')
    if not isinstance(raw_slots, list) or len(raw_slots) != capacity:
        raise ValueError('PDF 排版元数据槽位无效')
    page_slots = []
    page_indexes = []
    for value in raw_slots:
        if value is None:
            page_slots.append(None)
            continue
        index = _metadata_int(value, '页码')
        if index < 0:
            raise ValueError('PDF 排版元数据页码无效')
        page_slots.append(index)
        page_indexes.append(index)
    if len(set(page_indexes)) != len(page_indexes):
        raise ValueError('PDF 排版元数据页码重复')

    crop = metadata.get('crop_margins_mm')
    if not isinstance(crop, dict):
        raise ValueError('PDF 排版元数据裁边无效')
    crop_margins = {
        side: _finite_non_negative(crop.get(side), f'crop_{side}')
        for side in ('top', 'right', 'bottom', 'left')
    }
    complete_layout = metadata.get('complete_layout', False)
    if not isinstance(complete_layout, bool):
        raise ValueError('PDF 排版元数据完整状态无效')
    return {
        'version': 1,
        'rows': rows,
        'columns': columns,
        'order': order,
        'page_slots': page_slots,
        'crop_margins_mm': crop_margins,
        'drawing_width_mm': _finite_drawing_dimension(
            metadata.get('drawing_width_mm'), 'drawing_width_mm'
        ),
        'drawing_height_mm': _finite_drawing_dimension(
            metadata.get('drawing_height_mm'), 'drawing_height_mm'
        ),
        'complete_layout': complete_layout,
    }


def _metadata_int(value, name):
    if isinstance(value, bool) or (
        isinstance(value, float) and (not math.isfinite(value) or not value.is_integer())
    ):
        raise ValueError(f'PDF 排版元数据{name}无效')
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f'PDF 排版元数据{name}无效') from error


def _positive_grid_int(value, name):
    result = _metadata_int(value, name)
    if result <= 0 or result > MAX_GRID_SIZE:
        raise ValueError(f'{name} 超出范围')
    return result


def _finite_non_negative(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f'{name} 超出范围') from error
    if not math.isfinite(result) or result < 0 or result > 100:
        raise ValueError(f'{name} 超出范围')
    return result


def _finite_drawing_dimension(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f'{name} 超出范围') from error
    try:
        maximum = float(os.getenv('PLT_MAX_DIMENSION_MM', '10000'))
    except (TypeError, ValueError):
        maximum = 10000.0
    if not math.isfinite(maximum) or maximum < 100:
        maximum = 10000.0
    if not math.isfinite(result) or result < 0 or result > maximum:
        raise ValueError(f'{name} 超出范围')
    return result
