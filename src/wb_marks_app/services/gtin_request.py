from __future__ import annotations

from io import BytesIO
from importlib import resources
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


DATA_SHEET_NAME = "1. данные_о_продукте"
DATA_START_ROW = 7
DATA_COLUMNS = 22
SHEET_PATH = "xl/worksheets/sheet1.xml"
SHARED_STRINGS_PATH = "xl/sharedStrings.xml"
WORKBOOK_PATH = "xl/workbook.xml"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
X14AC_NS = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
X14_NS = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM_NS = "http://schemas.microsoft.com/office/excel/2006/main"
NS = {"m": MAIN_NS}

ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", REL_NS)
ET.register_namespace("mc", MC_NS)
ET.register_namespace("x14ac", X14AC_NS)
ET.register_namespace("x14", X14_NS)
ET.register_namespace("xm", XM_NS)


def build_gtin_request_workbook(product_card: Any, gpc_code: str, company_gcp_label: str = "") -> bytes:
    template_ref = resources.files("wb_marks_app").joinpath("assets/gtin_request_template.xlsx")
    with resources.as_file(template_ref) as template_path:
        with ZipFile(template_path, "r") as template_zip:
            names = template_zip.namelist()
            _validate_template(names, template_zip)

            sheet_root = ET.fromstring(template_zip.read(SHEET_PATH))
            shared_root = ET.fromstring(template_zip.read(SHARED_STRINGS_PATH))
            shared_index = _shared_string_index(shared_root)

            rows = list(product_card.rows)
            row_capacity = _template_row_capacity(sheet_root)
            if len(rows) > row_capacity:
                raise ValueError(
                    f"В шаблоне Текшер доступно {row_capacity} строк для товаров, "
                    f"а в карточке WB {len(rows)} строк. Шаблон не изменён."
                )

            if _text(company_gcp_label):
                _set_cell_string(sheet_root, "B2", _text(company_gcp_label), shared_root, shared_index)

            data_rows = _product_data_rows(product_card, rows, gpc_code)
            for offset in range(row_capacity):
                excel_row = DATA_START_ROW + offset
                values = data_rows[offset] if offset < len(data_rows) else [None] * DATA_COLUMNS
                _write_row_values(sheet_root, excel_row, values, shared_root, shared_index)

            _update_shared_string_counts(shared_root)
            replacements = {
                SHEET_PATH: _xml_bytes(sheet_root),
                SHARED_STRINGS_PATH: _xml_bytes(shared_root),
            }
            return _copy_xlsx_with_replacements(template_zip, replacements)


def _validate_template(names: list[str], template_zip: ZipFile) -> None:
    required = {SHEET_PATH, SHARED_STRINGS_PATH, WORKBOOK_PATH}
    missing = sorted(required - set(names))
    if missing:
        raise ValueError(f"Шаблон Текшер повреждён: нет частей {', '.join(missing)}.")

    workbook_xml = template_zip.read(WORKBOOK_PATH).decode("utf-8", "replace")
    has_external_links = any(name.startswith("xl/externalLinks/") for name in names)
    has_printer_settings = any(name.startswith("xl/printerSettings/") for name in names)
    if not has_external_links or "externalReferences" not in workbook_xml:
        raise ValueError("Шаблон Текшер не содержит внешнюю ссылку Excel.")
    if not has_printer_settings:
        raise ValueError("Шаблон Текшер не содержит настройки печати.")


def _template_row_capacity(sheet_root: ET.Element) -> int:
    max_row = 0
    for row in sheet_root.findall(".//m:sheetData/m:row", NS):
        row_number = int(row.attrib.get("r") or "0")
        max_row = max(max_row, row_number)
    return max(0, max_row - DATA_START_ROW + 1)


def _product_data_rows(product_card: Any, rows: list[Any], gpc_code: str) -> list[list[str | None]]:
    brand = _text(product_card.wb_summary.brand)
    functional_name = _capitalize_text(product_card.wb_summary.seller_category or product_card.wb_summary.name)
    seller_article = _text(product_card.wb_summary.seller_article)
    color = _text(product_card.wb_summary.color).lower()

    data_rows: list[list[str | None]] = []
    for row in rows:
        size = _text(row.wb_size)
        row_color = _text(row.color).lower() or color
        data_rows.append(
            [
                brand,
                None,
                "Русский",
                functional_name,
                _variant_text(seller_article, row_color, size),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "Россия (Российская Федерация)",
                "Кыргызстан",
                None,
                _text(gpc_code),
                "1",
                "Штуки",
                "Потребительский продукт",
                "Фиксированное измерение",
            ]
        )
    return data_rows


def _write_row_values(
    sheet_root: ET.Element,
    row_number: int,
    values: list[str | None],
    shared_root: ET.Element,
    shared_index: dict[str, int],
) -> None:
    for col_index in range(1, DATA_COLUMNS + 1):
        cell_ref = f"{_column_name(col_index)}{row_number}"
        value = values[col_index - 1] if col_index <= len(values) else None
        if _text(value):
            _set_cell_string(sheet_root, cell_ref, _text(value), shared_root, shared_index)
        else:
            _clear_cell(sheet_root, cell_ref)


def _set_cell_string(
    sheet_root: ET.Element,
    cell_ref: str,
    value: str,
    shared_root: ET.Element,
    shared_index: dict[str, int],
) -> None:
    cell = _ensure_cell(sheet_root, cell_ref)
    _clear_cell_children(cell)
    cell.attrib["t"] = "s"
    v = ET.SubElement(cell, f"{{{MAIN_NS}}}v")
    v.text = str(_shared_string_id(shared_root, shared_index, value))


def _clear_cell(sheet_root: ET.Element, cell_ref: str) -> None:
    cell = _find_cell(sheet_root, cell_ref)
    if cell is None:
        return
    _clear_cell_children(cell)
    cell.attrib.pop("t", None)


def _clear_cell_children(cell: ET.Element) -> None:
    for child in list(cell):
        if child.tag in {
            f"{{{MAIN_NS}}}v",
            f"{{{MAIN_NS}}}is",
            f"{{{MAIN_NS}}}f",
        }:
            cell.remove(child)


def _ensure_cell(sheet_root: ET.Element, cell_ref: str) -> ET.Element:
    cell = _find_cell(sheet_root, cell_ref)
    if cell is not None:
        return cell

    row_number = _row_number(cell_ref)
    row = _find_row(sheet_root, row_number)
    if row is None:
        sheet_data = sheet_root.find("m:sheetData", NS)
        if sheet_data is None:
            raise ValueError("Шаблон Текшер повреждён: нет sheetData.")
        row = ET.Element(f"{{{MAIN_NS}}}row", {"r": str(row_number)})
        _insert_row(sheet_data, row)

    cell = ET.Element(f"{{{MAIN_NS}}}c", {"r": cell_ref})
    _insert_cell(row, cell)
    return cell


def _find_cell(sheet_root: ET.Element, cell_ref: str) -> ET.Element | None:
    return sheet_root.find(f".//m:c[@r='{cell_ref}']", NS)


def _find_row(sheet_root: ET.Element, row_number: int) -> ET.Element | None:
    return sheet_root.find(f".//m:sheetData/m:row[@r='{row_number}']", NS)


def _insert_row(sheet_data: ET.Element, row: ET.Element) -> None:
    row_number = int(row.attrib["r"])
    for index, existing in enumerate(list(sheet_data)):
        existing_number = int(existing.attrib.get("r") or "0")
        if existing_number > row_number:
            sheet_data.insert(index, row)
            return
    sheet_data.append(row)


def _insert_cell(row: ET.Element, cell: ET.Element) -> None:
    cell_column = _column_index(cell.attrib["r"])
    for index, existing in enumerate(list(row)):
        existing_ref = existing.attrib.get("r") or ""
        if existing_ref and _column_index(existing_ref) > cell_column:
            row.insert(index, cell)
            return
    row.append(cell)


def _shared_string_index(shared_root: ET.Element) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, si in enumerate(shared_root.findall("m:si", NS)):
        text = _shared_string_text(si)
        result.setdefault(text, index)
    return result


def _shared_string_id(shared_root: ET.Element, shared_index: dict[str, int], value: str) -> int:
    if value in shared_index:
        return shared_index[value]
    index = len(shared_root.findall("m:si", NS))
    si = ET.SubElement(shared_root, f"{{{MAIN_NS}}}si")
    t = ET.SubElement(si, f"{{{MAIN_NS}}}t")
    if value != value.strip():
        t.attrib["{http://www.w3.org/XML/1998/namespace}space"] = "preserve"
    t.text = value
    shared_index[value] = index
    return index


def _shared_string_text(si: ET.Element) -> str:
    texts = []
    for text_node in si.findall(".//m:t", NS):
        texts.append(text_node.text or "")
    return "".join(texts)


def _update_shared_string_counts(shared_root: ET.Element) -> None:
    count = len(shared_root.findall("m:si", NS))
    shared_root.attrib["count"] = str(count)
    shared_root.attrib["uniqueCount"] = str(count)


def _copy_xlsx_with_replacements(template_zip: ZipFile, replacements: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as result_zip:
        for item in template_zip.infolist():
            data = replacements.get(item.filename)
            if data is None:
                data = template_zip.read(item.filename)
            result_zip.writestr(_copy_zip_info(item), data)
    return output.getvalue()


def _copy_zip_info(item: ZipInfo) -> ZipInfo:
    copied = ZipInfo(filename=item.filename, date_time=item.date_time)
    copied.compress_type = item.compress_type
    copied.comment = item.comment
    copied.extra = item.extra
    copied.internal_attr = item.internal_attr
    copied.external_attr = item.external_attr
    return copied


def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - 64)
    return index


def _row_number(cell_ref: str) -> int:
    digits = "".join(ch for ch in cell_ref if ch.isdigit())
    return int(digits)


def _variant_text(seller_article: str, color: str, size: str) -> str:
    parts = []
    if seller_article:
        parts.append(f"Арт.{seller_article}")
    if color:
        parts.append(f"цвет: {color}")
    if size:
        parts.append(f"р. {size}")
    return ", ".join(parts)


def _capitalize_text(value: str) -> str:
    text = _text(value)
    if not text:
        return ""
    lowered = text.lower()
    return lowered[:1].upper() + lowered[1:]


def _text(value: Any) -> str:
    return str(value or "").strip()
