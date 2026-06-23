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
    short_check_code: str = ""
    valid: bool = False
    error: str = ""


@dataclass(slots=True)
class LabelRecord:
    template: str = "srad"
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
    supplier_name: str = ""
    production_date: str = ""
    country_of_origin: str = ""
    brand: str = ""
    supplier_address: str = ""

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
    if code.startswith("]d2"):
        code = code[3:]
    return code.lstrip("\xe8")


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


def extract_gs1_mark_codes(text: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        for field in _csv_candidate_fields(line):
            parsed = parse_mark_code(field)
            if not parsed.valid or parsed.raw in seen:
                continue
            seen.add(parsed.raw)
            codes.append(parsed.raw)
    return codes


def parse_mark_code(value: str) -> ParsedMarkCode:
    code = _normalize_ai_parentheses(normalize_mark_code(value))
    if not code:
        return ParsedMarkCode(raw=code, error="empty code")
    if any(ord(char) < 32 and char != GS for char in code):
        return ParsedMarkCode(raw=code, error="mark code contains unsupported control characters")
    if not code.startswith("01") or len(code) < 18:
        return ParsedMarkCode(raw=code, error="expected GS1 code starting with AI 01")

    gtin = code[2:16]
    if not gtin.isdigit():
        return ParsedMarkCode(raw=code, gtin=gtin, error="AI 01 GTIN must contain 14 digits")
    if not gtin14_check_digit_is_valid(gtin):
        return ParsedMarkCode(raw=code, gtin=gtin, error="AI 01 GTIN check digit is invalid")

    rest = code[16:]
    if not rest.startswith("21"):
        return ParsedMarkCode(raw=code, gtin=gtin, error="expected AI 21 serial after AI 01 GTIN")
    rest = rest[2:]
    parts = rest.split(GS)
    serial = parts[0] if parts else ""
    if not serial:
        return ParsedMarkCode(raw=code, gtin=gtin, error="AI 21 serial is empty")

    check_key = ""
    crypto_tail = ""
    short_check_code = ""
    for part in parts[1:]:
        if not part:
            continue
        if part.startswith("91"):
            check_key = part[2:]
        elif part.startswith("92"):
            crypto_tail = part[2:]
        elif part.startswith("93"):
            short_check_code = part[2:]
        else:
            return ParsedMarkCode(
                raw=code,
                gtin=gtin,
                serial=serial,
                check_key=check_key,
                crypto_tail=crypto_tail,
                short_check_code=short_check_code,
                error=f"unsupported GS1 AI segment: {part[:2]}",
            )

    if check_key and len(check_key) != 4:
        return ParsedMarkCode(
            raw=code,
            gtin=gtin,
            serial=serial,
            check_key=check_key,
            crypto_tail=crypto_tail,
            short_check_code=short_check_code,
            error="AI 91 check key must contain 4 characters",
        )
    if crypto_tail and len(crypto_tail) not in {44, 88}:
        return ParsedMarkCode(
            raw=code,
            gtin=gtin,
            serial=serial,
            check_key=check_key,
            crypto_tail=crypto_tail,
            short_check_code=short_check_code,
            error="AI 92 crypto tail must contain 44 or 88 characters",
        )
    if short_check_code and len(short_check_code) != 4:
        return ParsedMarkCode(
            raw=code,
            gtin=gtin,
            serial=serial,
            short_check_code=short_check_code,
            error="AI 93 check code must contain 4 characters",
        )
    if bool(check_key) != bool(crypto_tail):
        return ParsedMarkCode(
            raw=code,
            gtin=gtin,
            serial=serial,
            check_key=check_key,
            crypto_tail=crypto_tail,
            short_check_code=short_check_code,
            error="AI 91 and AI 92 must be present together",
        )

    return ParsedMarkCode(
        raw=code,
        gtin=gtin,
        serial=serial,
        check_key=check_key,
        crypto_tail=crypto_tail,
        short_check_code=short_check_code,
        valid=True,
        error="",
    )


def validate_mark_code_for_datamatrix(value: str, *, require_verification: bool = True) -> ParsedMarkCode:
    parsed = parse_mark_code(value)
    if not parsed.valid:
        raise ValueError(parsed.error or "invalid GS1 DataMatrix payload")
    if require_verification and not (parsed.short_check_code or (parsed.check_key and parsed.crypto_tail)):
        raise ValueError("mark code must include AI 91/92 or AI 93 verification fields")
    return parsed


def validate_wb_barcode_for_code128(value: str) -> str:
    barcode = str(value or "").strip()
    if not barcode:
        raise ValueError("WB barcode is required")
    if len(barcode) > 64:
        raise ValueError("WB barcode is too long for Code128 label rendering")
    if any(ord(char) < 32 or ord(char) > 126 for char in barcode):
        raise ValueError("WB barcode must contain printable ASCII characters only")
    return barcode


def gtin14_check_digit_is_valid(value: str) -> bool:
    gtin = str(value or "")
    if len(gtin) != 14 or not gtin.isdigit():
        return False
    digits = [int(char) for char in gtin]
    total = 0
    for index, digit in enumerate(reversed(digits[:-1]), start=1):
        total += digit * (3 if index % 2 else 1)
    expected = (10 - (total % 10)) % 10
    return expected == digits[-1]


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
    supplier_name: str = "",
    production_date: str = "",
    country_of_origin: str = "",
    brand: str = "",
    supplier_address: str = "",
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
                supplier_name=supplier_name,
                production_date=production_date,
                country_of_origin=country_of_origin,
                brand=brand,
                supplier_address=supplier_address,
            )
        )
    return labels


def make_label_record(
    *,
    template: str = "srad",
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
    supplier_name: str = "",
    production_date: str = "",
    country_of_origin: str = "",
    brand: str = "",
    supplier_address: str = "",
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
        supplier_name=supplier_name.strip(),
        production_date=production_date.strip(),
        country_of_origin=country_of_origin.strip(),
        brand=brand.strip(),
        supplier_address=supplier_address.strip(),
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


def _csv_candidate_fields(line: str) -> list[str]:
    values: list[str] = [line]
    for delimiter in (",", ";", "\t"):
        if delimiter not in line and not line.startswith('"'):
            continue
        try:
            row = next(csv.reader([line], delimiter=delimiter))
        except csv.Error:
            continue
        values.extend(str(value) for value in row)

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_mark_code(value)
        if not normalized or normalized in seen or _looks_like_header(normalized):
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _looks_like_header(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"code", "codes", "mark_code", "marking_code", "код", "код чз", "чз"}


def _normalize_template(value: str) -> str:
    template = (value or "srad").strip().lower()
    aliases = {"combined": "srad", "58x40_full": "srad", "wb_chz_58x40": "srad"}
    template = aliases.get(template, template)
    return template if template in {"srad", "simple", "medium", "wb", "chz", "note"} else "srad"


def _normalize_ai_parentheses(value: str) -> str:
    code = value.strip()
    if not code.startswith("("):
        return code
    replacements = {
        "(01)": "01",
        "(21)": "21",
        "(91)": GS + "91",
        "(92)": GS + "92",
        "(93)": GS + "93",
    }
    for source, target in replacements.items():
        code = code.replace(source, target)
    return code
