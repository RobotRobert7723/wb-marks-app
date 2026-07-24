from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from wb_marks_app.services.labels import (
    GS,
    LabelRecord,
    parse_mark_code,
    validate_mark_code_for_chestny_znak_light_industry,
    validate_wb_barcode_for_code128,
)


PAGE_WIDTH_MM = 58.0
PAGE_HEIGHT_MM = 40.0
SAFE_MARGIN_MM = 3.0
MIN_CHZ_DATAMATRIX_MM = 11.0
MIN_DATAMATRIX_MODULE_MM = 0.25
MIN_CODE128_X_MM = 0.25
MIN_CODE128_BAR_HEIGHT_MM = 7.0
DATAMATRIX_QUIET_ZONE_MODULES = 1
CODE128_QUIET_ZONE_MODULES = 10
_ASSET_DIR = Path(__file__).resolve().parents[1] / "static" / "label_assets"
_CARE_ICONS_ASSET = _ASSET_DIR / "care_icons_58x40.png"
_EAC_ASSET = _ASSET_DIR / "eac_58x40.png"
_HONEST_SIGN_ASSET = _ASSET_DIR / "honest_sign_58x40.png"
_LOGGER = logging.getLogger(__name__)

PDF_TEMPLATE_ALIASES = {
    "srad": "58x40_full",
    "combined": "58x40_full",
    "58x40_full": "58x40_full",
    "wb_chz_58x40": "58x40_full",
    "simple": "58x40_simple",
    "58x40_simple": "58x40_simple",
    "simple_brand": "58x40_simple_brand",
    "simplebrand": "58x40_simple_brand",
    "simple brand": "58x40_simple_brand",
    "simple-brand": "58x40_simple_brand",
    "58x40_simple_brand": "58x40_simple_brand",
    "medium": "58x40_medium",
    "58x40_medium": "58x40_medium",
    "wb": "58x40_wb",
    "58x40_wb": "58x40_wb",
    "chz": "58x40_chz",
    "58x40_chz": "58x40_chz",
    "note": "58x40_note",
    "58x40_note": "58x40_note",
}


class LabelPdfError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _BarcodeRequest:
    key: str
    options: dict[str, Any]


_FONTS_REGISTERED = False
_FONT_REGULAR = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"


def render_labels_pdf(labels: list[LabelRecord], *, template: str = "srad") -> bytes:
    pdf_template = normalize_pdf_template(template)
    if not labels:
        raise LabelPdfError("No labels to render")

    _register_fonts()
    _validate_chestny_znak_labels(labels, pdf_template)
    try:
        barcode_requests = _collect_barcode_requests(labels, pdf_template)
    except ValueError as exc:
        raise LabelPdfError(str(exc)) from exc
    raw_barcodes = _render_barcodes(barcode_requests)

    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(_pt(PAGE_WIDTH_MM), _pt(PAGE_HEIGHT_MM)), pageCompression=1)
    canvas.setTitle("WB labels 58x40")
    for set_number, label in enumerate(labels, start=1):
        for page_template in _page_templates_for(pdf_template):
            _draw_label(canvas, label, page_template, raw_barcodes, set_number=set_number)
            canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def normalize_pdf_template(value: str) -> str:
    key = (value or "srad").strip().lower()
    try:
        return PDF_TEMPLATE_ALIASES[key]
    except KeyError as exc:
        raise LabelPdfError(f"Unknown label PDF template: {value}") from exc


def _collect_barcode_requests(labels: list[LabelRecord], template: str) -> list[_BarcodeRequest]:
    requests: dict[str, _BarcodeRequest] = {}
    templates_with_wb = {"58x40_full", "58x40_medium", "58x40_simple", "58x40_simple_brand", "58x40_wb"}
    templates_with_chz = {"58x40_full", "58x40_medium", "58x40_simple", "58x40_simple_brand", "58x40_chz"}
    for label in labels:
        if template in templates_with_wb and label.wb_barcode:
            value = validate_wb_barcode_for_code128(label.wb_barcode)
            key = _barcode_key("code128", value)
            requests[key] = _BarcodeRequest(
                key=key,
                options={
                    "bcid": "code128",
                    "text": value,
                    "scaleX": 2,
                    "scaleY": 2,
                    "height": 9,
                    "padding": 0,
                    "includetext": False,
                },
            )
        if template in templates_with_chz and label.mark_code:
            parsed = validate_mark_code_for_chestny_znak_light_industry(label.mark_code)
            key = _barcode_key("datamatrix", parsed.raw)
            requests[key] = _BarcodeRequest(
                key=key,
                options={
                    "bcid": "datamatrix",
                    "text": "^FNC1" + parsed.raw,
                    "parsefnc": True,
                    "scale": 2,
                    "padding": 0,
                },
            )
    return list(requests.values())


def _validate_chestny_znak_labels(labels: list[LabelRecord], template: str) -> None:
    templates_with_chz = {"58x40_full", "58x40_medium", "58x40_simple", "58x40_simple_brand", "58x40_chz"}
    if template not in templates_with_chz:
        return

    errors: list[str] = []
    for label in labels:
        if not label.mark_code:
            continue
        try:
            validate_mark_code_for_chestny_znak_light_industry(label.mark_code)
        except ValueError as exc:
            parsed = parse_mark_code(label.mark_code)
            errors.append(
                "label "
                f"{label.index}/{label.total}, "
                f"size={label.size or '-'}, "
                f"gtin={parsed.gtin or label.gtin or '-'}, "
                f"serial={parsed.serial or '-'}: {exc}"
            )

    if not errors:
        return
    message = "Chestny Znak code validation failed before label printing: " + "; ".join(errors[:20])
    if len(errors) > 20:
        message += f"; and {len(errors) - 20} more"
    _LOGGER.error(message)
    raise LabelPdfError(message)


def _page_templates_for(template: str) -> tuple[str, ...]:
    if template == "58x40_full":
        return ("58x40_full", "58x40_chz", "58x40_wb", "58x40_note")
    if template == "58x40_medium":
        return ("58x40_full", "58x40_chz", "58x40_wb")
    if template == "58x40_simple":
        return ("58x40_chz", "58x40_wb")
    if template == "58x40_simple_brand":
        return (
            "58x40_simple_brand_wb",
            "58x40_simple_brand_chz",
            "58x40_simple_brand_combo",
            "58x40_simple_brand_note",
        )
    return (template,)


def _barcode_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _render_barcodes(requests: list[_BarcodeRequest]) -> dict[str, dict[str, Any]]:
    if not requests:
        return {}
    node = _find_node_executable()
    if not node:
        raise LabelPdfError("Node.js is required to render GS1 DataMatrix and Code128 with bundled bwip-js")
    bwip_path = Path(__file__).resolve().parents[1] / "static" / "vendor" / "bwip-js-min.js"
    script = r"""
const fs = require('fs');
const bwipjs = require(process.argv[1]);
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
for (const request of requests) {
  try {
    out[request.key] = { ok: true, raw: bwipjs.raw(request.options)[0] };
  } catch (error) {
    out[request.key] = { ok: false, error: String(error && error.message ? error.message : error) };
  }
}
process.stdout.write(JSON.stringify(out));
"""
    payload = [{"key": request.key, "options": request.options} for request in requests]
    result = subprocess.run(
        [node, "-e", script, str(bwip_path)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise LabelPdfError(f"bwip-js barcode rendering failed: {message}")
    decoded = json.loads(result.stdout or "{}")
    rendered: dict[str, dict[str, Any]] = {}
    for key, item in decoded.items():
        if not item.get("ok"):
            raise LabelPdfError(f"Barcode rendering failed for {key}: {item.get('error', 'unknown error')}")
        rendered[key] = item["raw"]
    return rendered


def _find_node_executable() -> str | None:
    env_value = os.environ.get("NODE_BINARY")
    if env_value and Path(env_value).exists():
        return env_value
    return shutil.which("node")


def _draw_label(
    canvas: Canvas,
    label: LabelRecord,
    template: str,
    raw_barcodes: dict[str, dict[str, Any]],
    *,
    set_number: int,
) -> None:
    canvas.setFillColorRGB(0, 0, 0)
    if template == "58x40_wb":
        _draw_wb_label(canvas, label, raw_barcodes, set_number)
    elif template == "58x40_chz":
        _draw_chz_label(canvas, label, raw_barcodes, set_number)
    elif template == "58x40_note":
        _draw_note_label(canvas, label, set_number)
    elif template == "58x40_simple_brand_wb":
        _draw_simple_brand_wb_label(canvas, label, raw_barcodes, set_number)
    elif template == "58x40_simple_brand_chz":
        _draw_chz_label(canvas, label, raw_barcodes, set_number)
    elif template == "58x40_simple_brand_combo":
        _draw_simple_brand_combo_label(canvas, label, raw_barcodes, set_number)
    elif template == "58x40_simple_brand_note":
        _draw_simple_brand_note_label(canvas, label)
    else:
        _draw_full_label(canvas, label, raw_barcodes, set_number)


def _draw_full_label(
    canvas: Canvas,
    label: LabelRecord,
    raw_barcodes: dict[str, dict[str, Any]],
    set_number: int,
) -> None:
    if label.wb_barcode:
        _draw_code128(canvas, _raw(raw_barcodes, "code128", label.wb_barcode), 3.0, 3.0, 47.5, 7.0)
        _draw_centered(canvas, label.wb_barcode, 25.8, 10.1, 7.0, max_width_mm=32.0)

    _draw_set_number(canvas, set_number, size_pt=11.0)
    _draw_eac_logo(canvas, 50.0, 6.4, 4.8)
    _draw_text_fit(canvas, label.item_name, 3.0, 12.1, 36.5, 6.3, font=_FONT_REGULAR, min_size=4.6)

    rows = [
        ("Состав", label.composition),
        ("Поставщик", label.supplier_name),
        ("Дата производства", label.production_date),
        ("Сделано в", label.country_of_origin),
        ("Бренд", label.brand),
        ("Размер", label.size),
        ("Цвет", label.color),
        ("Артикул", label.vendor_code),
    ]
    y = 14.35
    row_line_height_mm = 1.85
    for title, value in rows:
        if not value:
            continue
        used_lines = _draw_pair_wrapped(
            canvas,
            title,
            value,
            3.0,
            y,
            35.0,
            5.1,
            max_lines=1,
            line_height_mm=row_line_height_mm,
        )
        y += row_line_height_mm * used_lines
        if y > 30.3:
            break

    if label.mark_code:
        _draw_datamatrix(canvas, _raw_datamatrix(raw_barcodes, label.mark_code), 39.0, 18.4, 16.0)

    _draw_care_icons(canvas, 3.0, 30.7)
    if label.supplier_address:
        _draw_supplier_address(canvas, label.supplier_address)


def _draw_wb_label(
    canvas: Canvas,
    label: LabelRecord,
    raw_barcodes: dict[str, dict[str, Any]],
    set_number: int,
) -> None:
    _draw_set_number(canvas, set_number, size_pt=12.0)
    _draw_centered(canvas, label.item_name, 29.0, 5.1, 11.0, font=_FONT_REGULAR, max_width_mm=43.0)
    if label.wb_barcode:
        _draw_code128(canvas, _raw(raw_barcodes, "code128", label.wb_barcode), 3.0, 11.0, 52.0, 9.8)
        _draw_centered(canvas, label.wb_barcode, 29.0, 21.1, 11.0, max_width_mm=41.0)
    if label.size:
        _draw_centered(canvas, f"Размер: {label.size}", 29.0, 27.0, 12.5, font=_FONT_BOLD, max_width_mm=43.0)
    _draw_pair_wrapped(canvas, "Цвет", label.color, 3.0, 31.9, 40.0, 7.2, max_lines=1)
    _draw_pair_wrapped(canvas, "Артикул", label.vendor_code, 3.0, 34.7, 52.0, 6.2, max_lines=1)


def _draw_chz_label(
    canvas: Canvas,
    label: LabelRecord,
    raw_barcodes: dict[str, dict[str, Any]],
    set_number: int,
) -> None:
    _draw_centered(canvas, label.item_name, 29.0, 3.0, 9.4, font=_FONT_BOLD, max_width_mm=38.0)
    _draw_set_number(canvas, set_number, size_pt=11.0)
    _draw_cz_logo(canvas, 4.0, 5.5)
    if label.mark_code:
        _draw_datamatrix(canvas, _raw_datamatrix(raw_barcodes, label.mark_code), 4.0, 10.0, 23.5)
    if label.color:
        _draw_centered(canvas, "Цвет:", 43.0, 8.0, 9.4, font=_FONT_BOLD, max_width_mm=20.0)
        _draw_centered(canvas, label.color, 43.0, 10.7, 9.2, max_width_mm=20.0)
    if label.size:
        _draw_centered(canvas, "Размер:", 43.0, 14.0, 9.4, font=_FONT_BOLD, max_width_mm=20.0)
        _draw_centered(canvas, label.size, 43.0, 17.4, 13.0, font=_FONT_BOLD, max_width_mm=17.0)
    _draw_hanger_icon(canvas, 36.2, 24.0)
    _draw_eac_logo(canvas, 42.0, 24.1, 5.0)
    if label.mark_preview:
        _draw_text_fit(canvas, label.mark_preview, 4.0, 34.4, 51.0, 6.2, font=_FONT_REGULAR, min_size=4.0)


def _draw_simple_brand_wb_label(
    canvas: Canvas,
    label: LabelRecord,
    raw_barcodes: dict[str, dict[str, Any]],
    set_number: int,
) -> None:
    _draw_set_number(canvas, set_number, size_pt=11.0)
    _draw_centered(canvas, label.item_name, 29.0, 4.4, 8.8, font=_FONT_BOLD, max_width_mm=34.0, min_size=6.0)
    if label.vendor_code:
        _draw_centered(canvas, "Артикул:", 29.0, 8.8, 8.2, font=_FONT_BOLD, max_width_mm=28.0, min_size=6.0)
        _draw_centered(canvas, label.vendor_code, 29.0, 11.4, 7.6, font=_FONT_REGULAR, max_width_mm=37.0, min_size=5.0)
    if label.composition:
        _draw_centered(canvas, "Состав:", 29.0, 14.0, 7.8, font=_FONT_BOLD, max_width_mm=25.0, min_size=5.5)
        _draw_centered(canvas, label.composition, 29.0, 16.2, 7.4, font=_FONT_REGULAR, max_width_mm=34.0, min_size=4.8)
    if label.size:
        _draw_pair_line(canvas, "Размер: ", label.size, 8.2, 19.5, 17.0, 7.8)
    if label.color:
        _draw_pair_line(canvas, "Цвет: ", label.color, 35.0, 19.5, 20.0, 7.8)
    if label.wb_barcode:
        _draw_code128(canvas, _raw(raw_barcodes, "code128", label.wb_barcode), 3.0, 23.2, 52.0, 8.9)
        _draw_centered(canvas, label.wb_barcode, 29.0, 32.3, 8.8, max_width_mm=40.0, min_size=6.2)


def _draw_simple_brand_combo_label(
    canvas: Canvas,
    label: LabelRecord,
    raw_barcodes: dict[str, dict[str, Any]],
    set_number: int,
) -> None:
    _draw_set_number(canvas, set_number, size_pt=11.0)
    _draw_centered(canvas, label.item_name, 29.0, 2.0, 8.8, font=_FONT_REGULAR, max_width_mm=37.0, min_size=6.0)
    if label.wb_barcode:
        _draw_code128(canvas, _raw(raw_barcodes, "code128", label.wb_barcode), 3.0, 7.0, 52.0, 8.8)
        _draw_centered(canvas, label.wb_barcode, 29.0, 16.2, 8.4, max_width_mm=40.0, min_size=6.0)

    y = 21.0
    rows = [
        ("Размер", label.size),
        ("Цвет", label.color),
        ("Артикул", label.vendor_code),
        ("Состав", label.composition),
    ]
    for title, value in rows:
        if not value:
            continue
        used_lines = _draw_pair_wrapped(
            canvas,
            title,
            value,
            3.0,
            y,
            34.0,
            7.1,
            max_lines=2 if title == "Состав" else 1,
            line_height_mm=2.6,
        )
        y += max(used_lines, 1) * 2.8
        if y > 34.0:
            break

    if label.mark_code:
        _draw_cz_logo(canvas, 39.9, 18.1)
        _draw_datamatrix(canvas, _raw_datamatrix(raw_barcodes, label.mark_code), 40.0, 22.8, 13.2)
    if label.mark_preview:
        _draw_text_fit(canvas, label.mark_preview, 20.8, 36.4, 34.0, 5.1, font=_FONT_REGULAR, min_size=3.8)


def _draw_simple_brand_note_label(canvas: Canvas, label: LabelRecord) -> None:
    note_lines = [line.strip() for line in (label.note_text or "Худи 1шт.\nБрюки 1шт.").splitlines() if line.strip()]
    lines = ["Менеджеру ПВЗ проверить"]
    if note_lines:
        lines.append(f"комплектность: {note_lines[0]}")
        lines.extend(note_lines[1:])
    else:
        lines.append("комплектность.")
    total_height = len(lines) * 4.8
    y = min(max(SAFE_MARGIN_MM, (PAGE_HEIGHT_MM - total_height) / 2), 37.0 - total_height)
    for line in lines:
        _draw_centered(canvas, line, 29.0, y, 11.2, font=_FONT_REGULAR, max_width_mm=50.0, min_size=7.0)
        y += 4.8


def _draw_note_label(canvas: Canvas, label: LabelRecord, set_number: int) -> None:
    _draw_set_number(canvas, set_number, size_pt=11.0)
    lines = ["Менеджеру ПВЗ", "Проверить", "комплектность:"]
    if label.note_text:
        lines.extend(line for line in label.note_text.splitlines() if line.strip())
    if len(lines) > 4:
        lines = lines[:4]
    total_height = len(lines) * 6.2
    y = min(max(SAFE_MARGIN_MM, (PAGE_HEIGHT_MM - total_height) / 2), 37.0 - total_height)
    for line in lines:
        _draw_centered(canvas, line, 29.0, y, 14.0, font=_FONT_BOLD, max_width_mm=52.0, min_size=9.0)
        y += 6.2


def _raw(raw_barcodes: dict[str, dict[str, Any]], kind: str, value: str) -> dict[str, Any]:
    try:
        return raw_barcodes[_barcode_key(kind, value)]
    except KeyError as exc:
        raise LabelPdfError(f"Missing rendered barcode for {kind}") from exc


def _raw_datamatrix(raw_barcodes: dict[str, dict[str, Any]], value: str) -> dict[str, Any]:
    return _raw(raw_barcodes, "datamatrix", validate_mark_code_for_chestny_znak_light_industry(value).raw)


def _draw_datamatrix(canvas: Canvas, raw: dict[str, Any], x_mm: float, y_top_mm: float, size_mm: float) -> None:
    pixx = int(raw["pixx"])
    pixy = int(raw["pixy"])
    module_mm = size_mm / (max(pixx, pixy) + DATAMATRIX_QUIET_ZONE_MODULES * 2)
    data_size_mm = module_mm * max(pixx, pixy)
    if data_size_mm < MIN_CHZ_DATAMATRIX_MM:
        raise LabelPdfError(
            f"DataMatrix is too small: {data_size_mm:.2f}mm, minimum is {MIN_CHZ_DATAMATRIX_MM:.2f}mm"
        )
    if module_mm < MIN_DATAMATRIX_MODULE_MM:
        raise LabelPdfError(
            f"DataMatrix module is too small: {module_mm:.3f}mm, minimum is {MIN_DATAMATRIX_MODULE_MM:.3f}mm"
        )

    module_pt = _pt(module_mm)
    x0 = _pt(x_mm) + DATAMATRIX_QUIET_ZONE_MODULES * module_pt
    top = _page_top(y_top_mm) - DATAMATRIX_QUIET_ZONE_MODULES * module_pt
    pixs = raw["pixs"]
    canvas.setFillColorRGB(0, 0, 0)
    for row in range(pixy):
        y = top - (row + 1) * module_pt
        row_offset = row * pixx
        for col in range(pixx):
            if pixs[row_offset + col]:
                canvas.rect(x0 + col * module_pt, y, module_pt, module_pt, stroke=0, fill=1)


def _draw_code128(
    canvas: Canvas,
    raw: dict[str, Any],
    x_mm: float,
    y_top_mm: float,
    width_mm: float,
    height_mm: float,
) -> None:
    if height_mm < MIN_CODE128_BAR_HEIGHT_MM:
        raise LabelPdfError(
            f"Code128 bars are too short: {height_mm:.2f}mm, minimum is {MIN_CODE128_BAR_HEIGHT_MM:.2f}mm"
        )
    modules = sum(int(value) for value in raw["sbs"])
    module_mm = width_mm / (modules + CODE128_QUIET_ZONE_MODULES * 2)
    if module_mm < MIN_CODE128_X_MM:
        raise LabelPdfError(f"Code128 X-dimension is too small: {module_mm:.3f}mm, minimum is {MIN_CODE128_X_MM:.3f}mm")

    module_pt = _pt(module_mm)
    x = _pt(x_mm) + CODE128_QUIET_ZONE_MODULES * module_pt
    y = _page_top(y_top_mm) - _pt(height_mm)
    height = _pt(height_mm)
    canvas.setFillColorRGB(0, 0, 0)
    is_bar = True
    for width_units in raw["sbs"]:
        width = int(width_units) * module_pt
        if is_bar:
            canvas.rect(x, y, width, height, stroke=0, fill=1)
        x += width
        is_bar = not is_bar


def _draw_cz_logo(canvas: Canvas, x_mm: float, y_top_mm: float) -> None:
    width_mm = 8.3
    height_mm = _asset_height_for_width(_HONEST_SIGN_ASSET, width_mm, fallback_ratio=52 / 143)
    _draw_image_asset(canvas, _HONEST_SIGN_ASSET, x_mm, y_top_mm, width_mm, height_mm)


def _draw_care_icons(canvas: Canvas, x_mm: float, y_top_mm: float) -> None:
    _draw_image_asset(canvas, _CARE_ICONS_ASSET, x_mm, y_top_mm, 19.0, 3.04)


def _draw_eac_logo(canvas: Canvas, x_mm: float, y_top_mm: float, width_mm: float) -> None:
    _draw_image_asset(canvas, _EAC_ASSET, x_mm, y_top_mm, width_mm, width_mm * 47 / 57)


def _draw_image_asset(
    canvas: Canvas,
    path: Path,
    x_mm: float,
    y_top_mm: float,
    width_mm: float,
    height_mm: float,
) -> None:
    if not path.exists():
        raise LabelPdfError(f"Label image asset is missing: {path}")
    canvas.drawImage(
        ImageReader(str(path)),
        _pt(x_mm),
        _page_top(y_top_mm) - _pt(height_mm),
        width=_pt(width_mm),
        height=_pt(height_mm),
        mask="auto",
    )


def _asset_height_for_width(path: Path, width_mm: float, *, fallback_ratio: float) -> float:
    if not path.exists():
        return width_mm * fallback_ratio
    reader = ImageReader(str(path))
    width_px, height_px = reader.getSize()
    if width_px <= 0:
        return width_mm * fallback_ratio
    return width_mm * height_px / width_px


def _draw_hanger_icon(canvas: Canvas, x_mm: float, y_top_mm: float) -> None:
    x = _pt(x_mm)
    top = _page_top(y_top_mm)
    r = _pt(2.1)
    cy = top - r
    cx = x + r
    canvas.setLineWidth(_pt(0.16))
    canvas.circle(cx, cy, r, stroke=1, fill=0)
    canvas.circle(cx, cy, r * 0.72, stroke=1, fill=0)
    canvas.setFont(_FONT_BOLD, 8.0)
    canvas.drawCentredString(cx, cy - _pt(0.95), "H")


def _draw_set_number(canvas: Canvas, set_number: int, *, size_pt: float) -> None:
    _draw_text_fit(canvas, str(set_number), 52.0, 3.0, 3.0, size_pt, font=_FONT_REGULAR, min_size=8.0)


def _draw_supplier_address(canvas: Canvas, value: str) -> None:
    _draw_text_fit(canvas, "Адрес поставщика:", 3.0, 34.1, 52.0, 4.6, font=_FONT_BOLD, min_size=4.0)
    _draw_text_fit(canvas, value, 3.0, 35.8, 52.0, 4.3, font=_FONT_REGULAR, min_size=3.8)


def _draw_pair_wrapped(
    canvas: Canvas,
    title: str,
    value: str,
    x_mm: float,
    y_top_mm: float,
    width_mm: float,
    size_pt: float,
    *,
    max_lines: int,
    line_height_mm: float = 2.85,
) -> int:
    if not value:
        return 0
    prefix = f"{title}: "
    lines = _wrap_line(prefix + value, width_mm, _FONT_REGULAR, size_pt)
    if not lines:
        return 0
    lines = lines[:max_lines]
    for index, line in enumerate(lines):
        y = y_top_mm + index * line_height_mm
        if index == 0 and line.startswith(prefix):
            _draw_pair_line(canvas, prefix, line[len(prefix) :], x_mm, y, width_mm, size_pt)
        else:
            _draw_text_fit(canvas, line, x_mm, y, width_mm, size_pt, font=_FONT_REGULAR, min_size=4.0)
    return len(lines)


def _draw_pair_line(
    canvas: Canvas,
    prefix: str,
    value: str,
    x_mm: float,
    y_top_mm: float,
    width_mm: float,
    size_pt: float,
) -> None:
    size = _fit_size(prefix + value, width_mm, _FONT_REGULAR, size_pt, 4.0)
    baseline = _baseline(y_top_mm, size)
    x = _pt(x_mm)
    canvas.setFont(_FONT_BOLD, size)
    canvas.drawString(x, baseline, prefix)
    canvas.setFont(_FONT_REGULAR, size)
    canvas.drawString(x + pdfmetrics.stringWidth(prefix, _FONT_BOLD, size), baseline, value)


def _draw_text_fit(
    canvas: Canvas,
    text: str,
    x_mm: float,
    y_top_mm: float,
    width_mm: float,
    size_pt: float,
    *,
    font: str,
    min_size: float,
) -> float:
    text = str(text or "")
    size = _fit_size(text, width_mm, font, size_pt, min_size)
    canvas.setFont(font, size)
    canvas.drawString(_pt(x_mm), _baseline(y_top_mm, size), text)
    return size


def _draw_centered(
    canvas: Canvas,
    text: str,
    center_x_mm: float,
    y_top_mm: float,
    size_pt: float,
    *,
    font: str | None = None,
    max_width_mm: float,
    min_size: float = 5.0,
) -> None:
    text = str(text or "")
    font = font or _FONT_REGULAR
    size = _fit_size(text, max_width_mm, font, size_pt, min_size)
    width = pdfmetrics.stringWidth(text, font, size)
    canvas.setFont(font, size)
    canvas.drawString(_pt(center_x_mm) - width / 2, _baseline(y_top_mm, size), text)


def _wrap_line(text: str, width_mm: float, font: str, size_pt: float) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_fits(candidate, width_mm, font, size_pt):
            current = candidate
            continue
        if current:
            lines.append(current)
        if _text_fits(word, width_mm, font, size_pt):
            current = word
        else:
            split_words = _split_long_word(word, width_mm, font, size_pt)
            lines.extend(split_words[:-1])
            current = split_words[-1] if split_words else ""
    if current:
        lines.append(current)
    return lines


def _split_long_word(word: str, width_mm: float, font: str, size_pt: float) -> list[str]:
    pieces: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        if _text_fits(candidate, width_mm, font, size_pt):
            current = candidate
            continue
        if current:
            pieces.append(current)
        current = char
    if current:
        pieces.append(current)
    return pieces


def _fit_size(text: str, width_mm: float, font: str, size_pt: float, min_size: float) -> float:
    size = size_pt
    while size > min_size and not _text_fits(text, width_mm, font, size):
        size -= 0.25
    return max(size, min_size)


def _text_fits(text: str, width_mm: float, font: str, size_pt: float) -> bool:
    return pdfmetrics.stringWidth(str(text or ""), font, size_pt) <= _pt(width_mm)


def _register_fonts() -> None:
    global _FONTS_REGISTERED, _FONT_REGULAR, _FONT_BOLD
    if _FONTS_REGISTERED:
        return
    regular = _first_existing_path(
        os.environ.get("LABEL_FONT_REGULAR"),
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    bold = _first_existing_path(
        os.environ.get("LABEL_FONT_BOLD"),
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    )
    if regular and bold:
        pdfmetrics.registerFont(TTFont("WBLabelRegular", str(regular)))
        pdfmetrics.registerFont(TTFont("WBLabelBold", str(bold)))
        _FONT_REGULAR = "WBLabelRegular"
        _FONT_BOLD = "WBLabelBold"
    _FONTS_REGISTERED = True


def _first_existing_path(*values: str | None) -> Path | None:
    for value in values:
        if not value:
            continue
        path = Path(value)
        if path.exists():
            return path
    return None


def _pt(value_mm: float) -> float:
    return value_mm * mm


def _page_top(y_top_mm: float) -> float:
    return _pt(PAGE_HEIGHT_MM - y_top_mm)


def _baseline(y_top_mm: float, size_pt: float) -> float:
    return _page_top(y_top_mm) - size_pt
