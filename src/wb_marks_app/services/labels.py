from __future__ import annotations

import csv
from dataclasses import asdict, dataclass


GS = "\x1d"


@dataclass(slots=True)
class ParsedMarkCode:
    raw: str
    gtin: str = ""
    serial: str = ""
    check_key: str = ""
    crypto_tail: str = ""
    valid: bool = False
    error: str = ""


@dataclass(slots=True)
class LabelRecord:
    template: str = "combined"
    item_name: str = ""
    vendor_code: str = ""
    size: str = ""
    color: str = ""
    composition: str = ""
    wb_barcode: str = ""
    mark_code: str = ""
    mark_preview: str = ""
    gtin: str = ""
    serial: str = ""
    check_key: str = ""
    crypto_tail: str = ""
    unit_count: str = "1"
    index: int = 1
    total: int = 1
    note_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_mark_code(value: str) -> str:
    code = (value or "").strip().lstrip("\ufeff")
    replacements = {
        "\\x1d": GS,
        "\\u001d": GS,
        "\\u001D": GS,
        "<GS>": GS,
        "{GS}": GS,
        "[GS]": GS,
        "\u241d": GS,
    }
    for needle, replacement in replacements.items():
        code = code.replace(needle, replacement)
    return code


def extract_mark_codes(text: str) -> list[str]:
    codes: list[str] = []
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        field = _first_csv_field(line)
        code = normalize_mark_code(field)
        if _looks_like_header(code):
            continue
        if code:
            codes.append(code)
    return codes


def parse_mark_code(value: str) -> ParsedMarkCode:
    code = normalize_mark_code(value)
    if not code:
        return ParsedMarkCode(raw=code, error="empty code")
    if not code.startswith("01") or len(code) < 18:
        return ParsedMarkCode(raw=code, error="expected GS1 code starting with AI 01")

    gtin = code[2:16]
    rest = code[16:]
    if rest.startswith("21"):
        rest = rest[2:]
    parts = rest.split(GS)
    serial = parts[0] if parts else ""
    check_key = ""
    crypto_tail = ""
    for part in parts[1:]:
        if part.startswith("91"):
            check_key = part[2:]
        elif part.startswith("92"):
            crypto_tail = part[2:]

    valid = bool(gtin and serial)
    return ParsedMarkCode(
        raw=code,
        gtin=gtin,
        serial=serial,
        check_key=check_key,
        crypto_tail=crypto_tail,
        valid=valid,
        error="" if valid else "could not parse AI 21 serial",
    )


def build_manual_labels(
    *,
    template: str,
    item_name: str = "",
    vendor_code: str = "",
    size: str = "",
    color: str = "",
    composition: str = "",
    wb_barcode: str = "",
    mark_codes_text: str = "",
    unit_count: str = "1",
    copies: int = 1,
    note_text: str = "",
) -> list[LabelRecord]:
    codes = extract_mark_codes(mark_codes_text)
    count = len(codes) if codes else max(copies, 1)
    labels: list[LabelRecord] = []
    for index in range(1, count + 1):
        code = codes[index - 1] if codes else ""
        labels.append(
            make_label_record(
                template=template,
                item_name=item_name,
                vendor_code=vendor_code,
                size=size,
                color=color,
                composition=composition,
                wb_barcode=wb_barcode,
                mark_code=code,
                unit_count=unit_count,
                index=index,
                total=count,
                note_text=note_text,
            )
        )
    return labels


def make_label_record(
    *,
    template: str = "combined",
    item_name: str = "",
    vendor_code: str = "",
    size: str = "",
    color: str = "",
    composition: str = "",
    wb_barcode: str = "",
    mark_code: str = "",
    unit_count: str = "1",
    index: int = 1,
    total: int = 1,
    note_text: str = "",
) -> LabelRecord:
    parsed = parse_mark_code(mark_code) if mark_code else ParsedMarkCode(raw="")
    return LabelRecord(
        template=_normalize_template(template),
        item_name=item_name.strip(),
        vendor_code=vendor_code.strip(),
        size=size.strip(),
        color=color.strip(),
        composition=composition.strip(),
        wb_barcode=wb_barcode.strip(),
        mark_code=parsed.raw,
        mark_preview=(parsed.raw.split(GS)[0] if parsed.raw else ""),
        gtin=parsed.gtin,
        serial=parsed.serial,
        check_key=parsed.check_key,
        crypto_tail=parsed.crypto_tail,
        unit_count=(unit_count or "1").strip() or "1",
        index=index,
        total=total,
        note_text=note_text.strip(),
    )


def labels_to_dicts(labels: list[LabelRecord]) -> list[dict]:
    return [label.to_dict() for label in labels]


def _first_csv_field(line: str) -> str:
    if not line.startswith('"'):
        return line
    try:
        row = next(csv.reader([line]))
    except csv.Error:
        return line.strip('"')
    return str(row[0]) if row else ""


def _looks_like_header(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"code", "codes", "mark_code", "marking_code", "код", "код чз", "чз"}


def _normalize_template(value: str) -> str:
    template = (value or "combined").strip().lower()
    return template if template in {"combined", "wb", "chz", "note"} else "combined"
