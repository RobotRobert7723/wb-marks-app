from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover - optional dependency during lightweight tests
    class _RequestsStub:
        class HTTPError(Exception):
            def __init__(self, *args, response=None, **kwargs) -> None:
                super().__init__(*args)
                self.response = response

        class Response:
            status_code: int
            text: str
            content: bytes

        class Session:
            def __init__(self, *args, **kwargs) -> None:
                raise ModuleNotFoundError(
                    "The 'requests' package is required for live Teksher API calls. "
                    "Install project dependencies with 'python -m pip install -e .'."
                )

    requests = _RequestsStub()  # type: ignore[assignment]

from wb_marks_app.exceptions import AppError, ManualStepRequired
from wb_marks_app.models import AppConfig, MarkingTask
from wb_marks_app.services.browser import BrowserSessionManager


Logger = Callable[[str], None]

TEKSHER_EXISTING_PRODUCT_MESSAGE = "В Текшер уже есть карточка с этим GTIN, данные НЕ СОХРАНЕНЫ"


class ExistingTeksherProductError(AppError):
    """Raised when Teksher already has a product card for a requested GTIN."""


class TeksherService:
    def __init__(
        self,
        browser: BrowserSessionManager,
        logger: Logger | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.browser = browser
        self.logger = logger or (lambda _: None)
        self.session = session or requests.Session()
        self.sleep = sleep or time.sleep
        self._dictionary_cache: dict[str, list[dict]] = {}

    def issue_marks(self, tasks: list[MarkingTask], config: AppConfig) -> list[MarkingTask]:
        if not tasks:
            return tasks

        if not self._has_valid_token(config):
            self._authenticate(config)

        if not config.teksher_api_token:
            self.browser.open_url(config.teksher_url)
            grouped = defaultdict(int)
            for task in tasks:
                grouped[task.gtin] += 1

            summary = ", ".join(f"{gtin} x{count}" for gtin, count in sorted(grouped.items()))
            self.logger(f"Teksher launch payload: {summary}")
            raise ManualStepRequired(
                "Teksher API token is not configured. "
                "The browser was opened with the saved session; finish the marking step there."
            )

        grouped_tasks = self._group_tasks(tasks)
        order_ids = self._create_mark_code_orders(grouped_tasks, config)

        for order_id, gtin in order_ids.items():
            self.logger(f"Teksher order created for GTIN {gtin}: {order_id}")
            self._wait_for_order_ready(order_id, config)
            marking_operation_id = self._create_utilisation(order_id, config)
            self.logger(f"Teksher marking operation created for GTIN {gtin}: {marking_operation_id}")
            codes = self._download_marking_codes(marking_operation_id, config)
            self._wait_for_operation_status(marking_operation_id, "ACCEPTED", config)
            self._apply_codes(grouped_tasks[gtin], codes)

        return tasks

    def save_operation_csv(self, operation_id: str, destination: Path, config: AppConfig) -> Path:
        if not self._has_valid_token(config):
            self._authenticate(config)
        destination.parent.mkdir(parents=True, exist_ok=True)
        csv_bytes = self._get_marking_csv(operation_id, config)
        destination.write_bytes(csv_bytes)
        return destination

    def ensure_authenticated(self, config: AppConfig) -> None:
        if not self._has_valid_token(config):
            self._authenticate(config)

    def validate_connection(self, config: AppConfig) -> None:
        self.ensure_authenticated(config)

    def ensure_product_drafts_for_mapping(
        self,
        product_card,
        rows_payload: list[dict],
        config: AppConfig,
    ) -> list[str]:
        rows = self._unique_gtin_rows(rows_payload)
        if not rows:
            return []

        self.ensure_authenticated(config)
        for row in rows:
            gtin = self._text(row.get("gtin"))
            if self._product_exists_by_gtin(gtin, config):
                raise ExistingTeksherProductError(TEKSHER_EXISTING_PRODUCT_MESSAGE)

        manufacturer_info = self._manufacturer_create_fields(config)
        draft_ids: list[str] = []
        for row in rows:
            payload = self._product_draft_payload(product_card, row, manufacturer_info, config)
            try:
                draft_ids.append(self._create_product_draft(payload, config))
            except AppError as exc:
                if self._is_existing_product_error(exc):
                    raise ExistingTeksherProductError(TEKSHER_EXISTING_PRODUCT_MESSAGE) from exc
                raise
        return draft_ids

    def product_exists_by_gtin(self, gtin: str, config: AppConfig) -> bool:
        self.ensure_authenticated(config)
        return self._product_exists_by_gtin(gtin, config)

    def product_mapping_rows_by_gtins(self, gtins: list[str], config: AppConfig) -> dict[str, dict]:
        self.ensure_authenticated(config)
        result: dict[str, dict] = {}
        for gtin in self._unique_values(gtins):
            product = self._product_by_gtin(gtin, config)
            if product is None:
                continue
            row = self._product_mapping_row(product, config)
            row_gtin = self._text(row.get("gtin")) or gtin
            row["gtin"] = row_gtin
            result[row_gtin] = row
        return result

    def create_mark_code_order(self, gtin: str, quantity: int, config: AppConfig) -> str:
        self.ensure_authenticated(config)
        payload = self._create_mark_code_orders({gtin: [object()] * quantity}, config)
        order_id = next(iter(payload.keys()), "")
        if not order_id:
            raise AppError(f"Teksher did not return an order id for GTIN {gtin}.")
        return order_id

    def wait_for_order_ready(self, order_id: str, config: AppConfig) -> dict:
        self._wait_for_order_ready(order_id, config)
        return self._get_operation(order_id, config)

    def create_marking_operation(self, order_id: str, config: AppConfig) -> str:
        self.ensure_authenticated(config)
        return self._create_utilisation(order_id, config)

    def wait_for_operation(self, operation_id: str, expected_status: str, config: AppConfig) -> dict:
        self._wait_for_operation_status(operation_id, expected_status, config)
        return self._get_operation(operation_id, config)

    def get_operation_details(self, operation_id: str, config: AppConfig) -> dict:
        self.ensure_authenticated(config)
        return self._get_operation(operation_id, config)

    def read_operation_codes(self, operation_id: str, config: AppConfig) -> list[str]:
        self.ensure_authenticated(config)
        csv_bytes = self._get_marking_csv(operation_id, config)
        return self._parse_codes(csv_bytes)

    def run_full_cycle(self, gtin: str, quantity: int, config: AppConfig, destination: Path) -> Path:
        if not self._has_valid_token(config):
            self._authenticate(config)
        headers = self._headers(config)
        payload = {
            "countryId": config.teksher_country_id,
            "extension": config.teksher_extension,
            "items": [
                {
                    "gtin": gtin,
                    "markingCodesAmount": quantity,
                    "dataSupplier": "AUTO",
                }
            ],
        }
        response = self.session.post(
            self._url(config, "/facade/order/api/v1/operations/multi"),
            headers=headers,
            json=payload,
            timeout=30,
        )
        self._raise_for_status(response)
        data = self._json(response)
        order_ids = data.get("data") or {}
        order_id = next(iter(order_ids.keys()), "")
        if not order_id:
            raise AppError(f"Teksher did not return an order id: {data}")

        self.logger(f"Teksher order created: {order_id} for GTIN {gtin}")
        self._wait_for_order_ready(order_id, config)
        marking_operation_id = self._create_utilisation(order_id, config)
        self.logger(f"Teksher marking operation created: {marking_operation_id}")
        self._wait_for_operation_status(marking_operation_id, "ACCEPTED", config)
        path = self.save_operation_csv(marking_operation_id, destination, config)
        return path

    def run_transgran_cycle(
        self,
        csv_path: Path,
        gtins: list[str],
        document_number: str,
        document_date: datetime,
        shipment_date: datetime,
        config: AppConfig,
    ) -> str:
        if not self._has_valid_token(config):
            self._authenticate(config)
        file_id = self._upload_transgran_marking_file(csv_path, config)
        operation_id = self._create_transgran_operation(
            file_id=file_id,
            gtins=gtins,
            document_number=document_number,
            document_date=document_date,
            shipment_date=shipment_date,
            config=config,
        )
        self._wait_for_notifications_unread(config)
        self._wait_for_operation_registration(operation_id, config)
        return operation_id

    def _group_tasks(self, tasks: list[MarkingTask]) -> dict[str, list[MarkingTask]]:
        grouped: dict[str, list[MarkingTask]] = defaultdict(list)
        for task in tasks:
            grouped[task.gtin].append(task)
        return grouped

    def _unique_gtin_rows(self, rows_payload: list[dict]) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        for row in rows_payload:
            if not isinstance(row, dict):
                continue
            gtin = self._text(row.get("gtin"))
            if not gtin or gtin in seen:
                continue
            seen.add(gtin)
            rows.append(row)
        return rows

    def _unique_values(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = self._text(value)
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _product_exists_by_gtin(self, gtin: str, config: AppConfig) -> bool:
        normalized_gtin = self._text(gtin)
        if not normalized_gtin:
            return False
        response = self.session.get(
            self._url(config, f"/facade/api/v1/products?gtin={quote(normalized_gtin)}"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        for record in self._extract_records(payload):
            for key in ("gtin", "GTIN", "gtinCode", "productGtin"):
                if self._text(record.get(key)) == normalized_gtin:
                    return True
        return False

    def _product_by_gtin(self, gtin: str, config: AppConfig) -> dict | None:
        normalized_gtin = self._text(gtin)
        if not normalized_gtin:
            return None
        response = self.session.get(
            self._url(config, f"/facade/api/v1/products?gtin={quote(normalized_gtin)}"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        product = None
        for record in self._extract_product_records(payload):
            if self._text(
                record.get("gtin") or record.get("GTIN") or record.get("gtinCode") or record.get("productGtin")
            ) == normalized_gtin:
                product = record
                break
        if product is None:
            return None

        product_id = self._product_id_from_response({"data": product})
        if product_id:
            try:
                detail = self._product_detail(product_id, config)
            except AppError:
                detail = None
            if detail is not None:
                if not self._text(detail.get("gtin")):
                    detail["gtin"] = normalized_gtin
                return detail
        return product

    def _product_detail(self, product_id: str, config: AppConfig) -> dict | None:
        response = self.session.get(
            self._url(config, f"/facade/api/v1/products/{quote(product_id)}"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        return self._product_payload(self._json(response))

    def _product_payload(self, payload: dict) -> dict | None:
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and self._looks_like_product(data):
            return data
        if isinstance(payload, dict) and self._looks_like_product(payload):
            return payload
        for record in self._extract_product_records(payload):
            return record
        return None

    def _product_mapping_row(self, product: dict, config: AppConfig) -> dict:
        attributes = self._product_attributes(product, config)
        return {
            "teksher_size": self._attribute_value_by_spec(
                attributes,
                {"35"},
                ("razmerodezhdy", "razmerizdeliya", "размеродежды", "размеризделия"),
            ),
            "product_type": self._attribute_value_by_spec(
                attributes,
                {"12"},
                ("vidtovara", "видтовара"),
            ),
            "gtin": self._text(
                product.get("gtin") or product.get("GTIN") or product.get("gtinCode") or product.get("productGtin")
            ),
            "tnved": self._nested_text(product, ("tnved", "tnvedCode"), ("code", "name", "title", "id")),
            "country": self._nested_text(
                product,
                ("manufacturedCountry", "country", "manufacturedCountryId"),
                ("name", "title", "code", "alpha2", "alpha3", "id"),
            ),
            "vendor_article": self._attribute_value_by_spec(
                attributes,
                {"13914"},
                (
                    "modelartikulproizvoditelya",
                    "artikulproizvoditelya",
                    "модельартикулпроизводителя",
                    "артикулпроизводителя",
                ),
            ),
            "color": self._attribute_value_by_spec(attributes, {"36"}, ("tsvet", "цвет")),
            "composition": self._attribute_value_by_spec(attributes, {"2483"}, ("sostav", "состав")),
            "target_gender": self._attribute_value_by_spec(
                attributes,
                {"14013"},
                ("tselevoipol", "целевойпол"),
            ),
            "trademark": self._text(product.get("trademark") or product.get("brand")),
        }

    def _product_attributes(self, product: dict, config: AppConfig) -> list[dict]:
        attributes = self._coerce_dict_list(
            product.get("attributes")
            or product.get("attributeValues")
            or product.get("productAttributes")
        )
        if attributes:
            return attributes
        product_id = self._product_id_from_response({"data": product})
        if not product_id:
            return []
        try:
            response = self.session.get(
                self._url(config, f"/facade/api/v1/products/{quote(product_id)}/attributes"),
                headers=self._headers(config),
                timeout=30,
            )
            self._raise_for_status(response)
        except AppError:
            return []
        payload = self._json(response)
        return self._coerce_dict_list(payload.get("data") if isinstance(payload, dict) else payload)

    def _attribute_value_by_spec(
        self,
        attributes: list[dict],
        codes: set[str],
        name_tokens: tuple[str, ...],
    ) -> str:
        for attribute in attributes:
            code = self._attribute_code(attribute)
            if code in codes:
                value = self._attribute_value(attribute)
                if value:
                    return value
        normalized_tokens = {self._normalize_match(token) for token in name_tokens}
        for attribute in attributes:
            name = self._normalize_match(
                self._first_record_text(
                    attribute,
                    "name",
                    "attributeName",
                    "attributeTypeName",
                    "title",
                    "label",
                    "codeName",
                )
            )
            if not name or not any(token in name for token in normalized_tokens):
                continue
            value = self._attribute_value(attribute)
            if value:
                return value
        return ""

    def _attribute_code(self, attribute: dict) -> str:
        for key in ("attributeTypeCode", "attribute_type_code", "typeCode", "attributeCode", "code", "id"):
            value = self._text(attribute.get(key))
            if value:
                return value
        attribute_type = attribute.get("attributeType")
        if isinstance(attribute_type, dict):
            for key in ("code", "id"):
                value = self._text(attribute_type.get(key))
                if value:
                    return value
        return ""

    def _attribute_value(self, attribute: dict) -> str:
        for key in (
            "value",
            "values",
            "attributeValue",
            "attributeValues",
            "optionValue",
            "valueText",
            "text",
            "dictionaryValue",
            "dictionaryValues",
        ):
            value = self._text(attribute.get(key))
            if value:
                return value
        return ""

    def _nested_text(self, product: dict, keys: tuple[str, ...], nested_keys: tuple[str, ...]) -> str:
        for key in keys:
            value = product.get(key)
            if isinstance(value, dict):
                for nested_key in nested_keys:
                    text = self._text(value.get(nested_key))
                    if text:
                        return text
            else:
                text = self._text(value)
                if text:
                    return text
        return ""

    def _extract_product_records(self, payload) -> list[dict]:
        return [record for record in self._extract_records(payload) if self._looks_like_product(record)]

    def _looks_like_product(self, record: dict) -> bool:
        return any(
            key in record
            for key in (
                "gtin",
                "GTIN",
                "gtinCode",
                "productGtin",
                "fullName",
                "status",
                "attributes",
                "attributeValues",
                "trademark",
                "manufacturedCountry",
                "tnved",
            )
        )

    def _coerce_dict_list(self, value) -> list[dict]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            records = self._extract_records(value)
            if records:
                return records
            return [value]
        return []

    def _create_product_draft(self, payload: dict, config: AppConfig) -> str:
        response = self.session.post(
            self._url(config, "/facade/api/v1/products/create"),
            headers=self._headers(config),
            json=payload,
            timeout=30,
        )
        self._raise_for_status(response)
        data = self._json_or_empty(response)
        if data is None:
            self.logger("Teksher product draft create returned an empty response body.")
            return ""
        draft_id = self._product_id_from_response(data)
        if not draft_id:
            self.logger(f"Teksher product draft create response did not include id: {data}")
        return draft_id

    def _product_draft_payload(
        self,
        product_card,
        row: dict,
        manufacturer_info: dict[str, str],
        config: AppConfig,
    ) -> dict:
        tnved = self._text(row.get("tnved")) or self._text(product_card.wb_summary.tnved)
        country = self._text(row.get("country")) or self._text(product_card.wb_summary.country)
        payload = {
            "gtin": self._text(row.get("gtin")),
            "fullName": self._product_full_name(product_card, row),
            "manufacturerFullName": manufacturer_info["manufacturerFullName"],
            "manufacturerInn": manufacturer_info["manufacturerInn"],
            "gcp": manufacturer_info["gcp"],
            "gln": manufacturer_info["gln"],
            "manufacturedCountryId": self._resolve_country_id(country, config),
            "tnved": self._resolve_tnved_id(tnved, config),
            "trademark": self._text(row.get("trademark")) or self._text(product_card.wb_summary.brand),
            "isImport": False,
        }
        return payload

    def _product_full_name(self, product_card, row: dict) -> str:
        values = [
            self._text(row.get("product_type")) or self._text(product_card.wb_summary.name),
            self._text(row.get("teksher_size")) or self._text(row.get("wb_size")),
            self._text(row.get("color")),
            self._text(row.get("composition")) or self._text(product_card.wb_summary.composition),
        ]
        full_name = " ".join(value for value in values if value)
        if full_name:
            return full_name
        return f"WB {self._text(product_card.wb_article)} {self._text(row.get('gtin'))}".strip()

    def _manufacturer_create_fields(self, config: AppConfig) -> dict[str, str]:
        response = self.session.get(
            self._url(config, "/facade/api/v1/participants/manufacturer_info"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        data = payload.get("data") if isinstance(payload, dict) else payload
        manufacturer = {
            "manufacturerFullName": self._participant_value(
                data,
                "manufacturerFullName",
                "fullName",
                "full_name",
                "name",
                "participantName",
                "organizationName",
                "companyName",
            ),
            "manufacturerInn": self._participant_value(
                data,
                "manufacturerInn",
                "inn",
                "taxNumber",
                "taxpayerId",
                "tin",
            ),
            "gcp": self._participant_value(data, "gcp", "gs1CompanyPrefix", "companyPrefix")
            or self._identifier_value(data, "gcp", "gs1companyprefix", "companyprefix"),
            "gln": self._participant_value(data, "gln", "globalLocationNumber")
            or self._identifier_value(data, "gln", "globallocationnumber"),
        }
        missing = [key for key, value in manufacturer.items() if not value]
        if missing:
            raise AppError(
                "Не удалось получить реквизиты производителя Текшер для создания карточки: "
                + ", ".join(missing)
                + "."
            )
        return manufacturer

    def _participant_value(self, payload, *keys: str) -> str:
        for requested_key in keys:
            normalized_key = self._key_token(requested_key)
            for record in self._walk_dicts(payload):
                for key, value in record.items():
                    if self._key_token(str(key)) == normalized_key:
                        text = self._text(value)
                        if text:
                            return text
        return ""

    def _identifier_value(self, payload, *identifier_names: str) -> str:
        names = {self._normalize_match(name) for name in identifier_names}
        for record in self._walk_dicts(payload):
            label = self._first_record_text(
                record,
                "type",
                "identifierType",
                "identifierName",
                "name",
                "code",
                "key",
            )
            if self._normalize_match(label) not in names:
                continue
            value = self._first_record_text(record, "value", "identifier", "number")
            if value:
                return value
        return ""

    def _resolve_tnved_id(self, value: str, config: AppConfig) -> int:
        return self._dictionary_id(
            value,
            self._dictionary_items("/facade/api/v1/tnveds", config),
            ("code", "tnved", "tnvedCode", "name", "title", "label", "value"),
            "ТНВЭД",
        )

    def _resolve_country_id(self, value: str, config: AppConfig) -> int:
        country = self._text(value)
        try:
            return self._dictionary_id(
                country,
                self._dictionary_items("/facade/api/v1/countries", config),
                ("name", "nameRu", "shortName", "title", "label", "code", "alpha2", "alpha3", "countryCode"),
                "страну производства",
            )
        except AppError:
            if self._normalize_match(country) in {
                "kg",
                "kyrgyzstan",
                "kyrgyzrepublic",
                "кыргызстан",
                "киргизия",
                "киргизскаяреспублика",
            }:
                return config.teksher_country_id
            raise

    def _dictionary_items(self, path: str, config: AppConfig) -> list[dict]:
        cache_key = f"{config.teksher_url.rstrip('/')}{path}"
        if cache_key in self._dictionary_cache:
            return self._dictionary_cache[cache_key]
        response = self.session.get(
            self._url(config, path),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        items = list(self._extract_records(payload))
        self._dictionary_cache[cache_key] = items
        return items

    def _dictionary_id(
        self,
        value: str,
        items: list[dict],
        value_keys: tuple[str, ...],
        label: str,
    ) -> int:
        text = self._text(value)
        if not text:
            raise AppError(f"Не удалось определить {label}: значение пустое.")
        text_norm = self._normalize_match(text)
        text_digits = self._digits(text)
        for item in items:
            item_id = self._extract_dict_id(item)
            if item_id is None:
                continue
            if self._text(item_id) == text:
                return int(item_id)
            for candidate in self._dictionary_value_texts(item, value_keys):
                candidate_norm = self._normalize_match(candidate)
                candidate_digits = self._digits(candidate)
                if candidate_norm == text_norm or (text_digits and candidate_digits == text_digits):
                    return int(item_id)
        if text.isdigit() and len(text) <= 6:
            return int(text)
        raise AppError(f"Не удалось найти {label} в справочнике Текшер: {text}.")

    def _dictionary_value_texts(self, item: dict, keys: tuple[str, ...]) -> list[str]:
        normalized_keys = {self._key_token(key) for key in keys}
        values: list[str] = []
        for key, value in item.items():
            if self._key_token(str(key)) in normalized_keys:
                text = self._text(value)
                if text:
                    values.append(text)
            if isinstance(value, dict):
                values.extend(self._dictionary_value_texts(value, keys))
        return values

    def _extract_dict_id(self, item: dict) -> int | None:
        for key in ("id", "ID", "valueId", "value_id"):
            value = item.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
        return None

    def _extract_records(self, payload) -> list[dict]:
        if isinstance(payload, list):
            records: list[dict] = []
            for item in payload:
                records.extend(self._extract_records(item))
            return records
        if not isinstance(payload, dict):
            return []

        records = []
        if self._extract_dict_id(payload) is not None or any(key in payload for key in ("gtin", "GTIN", "gtinCode")):
            records.append(payload)
        for key in ("data", "content", "items", "list", "results", "records", "rows", "products", "product", "body"):
            if key in payload:
                records.extend(self._extract_records(payload[key]))
        return records

    def _walk_dicts(self, payload):
        if isinstance(payload, dict):
            yield payload
            for value in payload.values():
                yield from self._walk_dicts(value)
        elif isinstance(payload, list):
            for item in payload:
                yield from self._walk_dicts(item)

    def _first_record_text(self, record: dict, *keys: str) -> str:
        normalized_keys = {self._key_token(key) for key in keys}
        for key, value in record.items():
            if self._key_token(str(key)) in normalized_keys:
                text = self._text(value)
                if text:
                    return text
        return ""

    def _product_id_from_response(self, payload: dict) -> str:
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            for key in ("id", "productId", "product_id"):
                if self._text(data.get(key)):
                    return self._text(data.get(key))
        for key in ("id", "productId", "product_id"):
            if isinstance(payload, dict) and self._text(payload.get(key)):
                return self._text(payload.get(key))
        if isinstance(data, (str, int)):
            return str(data)
        return ""

    def _is_existing_product_error(self, exc: Exception) -> bool:
        message = str(exc).casefold()
        return "409" in message or "conflict" in message or "already" in message or "уже" in message

    def _text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            return ", ".join(self._text(item) for item in value if self._text(item))
        if isinstance(value, dict):
            for key in ("value", "name", "title", "label", "code"):
                text = self._text(value.get(key))
                if text:
                    return text
            return ""
        return str(value).strip()

    def _normalize_match(self, value: str) -> str:
        return (
            self._text(value)
            .replace("ё", "е")
            .replace("Ё", "Е")
            .replace("_", "")
            .replace("-", "")
            .replace(".", "")
            .replace(",", "")
            .replace("/", "")
            .replace(" ", "")
            .casefold()
        )

    def _key_token(self, value: str) -> str:
        return self._normalize_match(value)

    def _digits(self, value: str) -> str:
        return "".join(ch for ch in self._text(value) if ch.isdigit())

    def _create_mark_code_orders(
        self,
        grouped_tasks: dict[str, list[MarkingTask]],
        config: AppConfig,
    ) -> dict[str, str]:
        items = []
        for gtin, gtin_tasks in sorted(grouped_tasks.items()):
            items.append(
                {
                    "gtin": gtin,
                    "markingCodesAmount": len(gtin_tasks),
                    "dataSupplier": "AUTO",
                }
            )

        payload = {
            "countryId": config.teksher_country_id,
            "extension": config.teksher_extension,
            "items": items,
        }
        response = self.session.post(
            self._url(config, "/facade/order/api/v1/operations/multi"),
            headers=self._headers(config),
            json=payload,
            timeout=30,
        )
        self._raise_for_status(response)
        data = self._json(response)
        order_ids = data.get("data") or {}
        if not order_ids:
            raise AppError(f"Teksher did not return order ids: {data}")
        return {str(order_id): str(gtin) for order_id, gtin in order_ids.items()}

    def _authenticate(self, config: AppConfig) -> None:
        if not config.teksher_username or not config.teksher_password:
            raise ManualStepRequired(
                "Teksher credentials are not configured. "
                "Provide teksher_username and teksher_password to fetch a fresh token automatically."
            )

        response = self.session.post(
            self._url(config, "/facade/oauth/login"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"username": config.teksher_username, "password": config.teksher_password},
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        data = payload.get("data") or {}
        access_token = str(data.get("access_token") or "")
        refresh_token = str(data.get("refresh_token") or "")
        if not access_token:
            raise AppError(f"Teksher login did not return an access token: {payload}")
        config.teksher_api_token = access_token
        config.teksher_refresh_token = refresh_token
        self.logger("Teksher token refreshed via oauth/login")

    def _wait_for_order_ready(self, order_id: str, config: AppConfig) -> None:
        timeout_seconds = max(config.step_timeout_seconds, 30)
        deadline = time.monotonic() + timeout_seconds
        last_status = "unknown"
        last_end_at = ""

        while time.monotonic() < deadline:
            try:
                details = self._get_operation(order_id, config)
                last_status = str(details.get("status") or "unknown")
                last_end_at = str(details.get("endAt") or "")
                ready = self._is_operation_ready(order_id, config)
            except (AppError, requests.RequestException) as exc:
                self.logger(f"Teksher order {order_id} polling transient error: {exc}")
                self.sleep(2.0)
                continue
            self.logger(
                f"Teksher order {order_id} status={last_status}, ready={ready}, endAt={last_end_at or '-'}"
            )
            if last_status == "ACCEPTED" and (ready or last_end_at):
                return
            if last_status in {"REJECTED", "FAILED"}:
                raise AppError(f"Teksher order {order_id} finished with status {last_status}")
            self.sleep(2.0)

        raise AppError(f"Teksher order {order_id} did not become ready within {timeout_seconds} seconds.")

    def _wait_for_operation_status(self, operation_id: str, expected_status: str, config: AppConfig) -> None:
        timeout_seconds = max(config.step_timeout_seconds, 30)
        deadline = time.monotonic() + timeout_seconds
        last_status = "unknown"

        while time.monotonic() < deadline:
            try:
                details = self._get_operation(operation_id, config)
                last_status = str(details.get("status") or "unknown")
            except (AppError, requests.RequestException) as exc:
                self.logger(f"Teksher operation {operation_id} polling transient error: {exc}")
                self.sleep(2.0)
                continue
            self.logger(f"Teksher operation {operation_id} status={last_status}")
            if last_status == expected_status:
                return
            if last_status in {"REJECTED", "FAILED"}:
                raise AppError(f"Teksher operation {operation_id} finished with status {last_status}")
            self.sleep(2.0)

        raise AppError(
            f"Teksher operation {operation_id} did not reach status {expected_status} "
            f"within {timeout_seconds} seconds (last status {last_status})."
        )

    def _create_utilisation(self, order_id: str, config: AppConfig) -> str:
        payload = {
            "extension": config.teksher_extension,
            "orderId": order_id,
            "dataSupplier": "AUTO",
        }
        response = self.session.post(
            self._url(config, "/facade/order/api/v1/operations/utilisation"),
            headers=self._headers(config),
            json=payload,
            timeout=30,
        )
        self._raise_for_status(response)
        data = self._json(response)
        operation_id = str(data.get("data") or "")
        if not operation_id:
            raise AppError(f"Teksher did not return a marking operation id: {data}")
        return operation_id

    def _download_marking_codes(self, operation_id: str, config: AppConfig) -> list[str]:
        timeout_seconds = max(config.step_timeout_seconds, 30)
        deadline = time.monotonic() + timeout_seconds
        last_error = ""

        while time.monotonic() < deadline:
            try:
                csv_bytes = self._get_marking_csv(operation_id, config)
                codes = self._parse_codes(csv_bytes)
                if codes:
                    return codes
                last_error = "empty CSV"
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "unknown"
                last_error = f"HTTP {status}"
            self.sleep(2.0)

        raise AppError(
            f"Teksher marking CSV for operation {operation_id} was not available within "
            f"{timeout_seconds} seconds ({last_error})."
        )

    def _get_marking_csv(self, operation_id: str, config: AppConfig) -> bytes:
        response = self.session.get(
            self._url(config, f"/facade/api/v1/marking_codes/csv?operationId={operation_id}"),
            headers=self._headers(config, accept="application/octet-stream"),
            timeout=30,
        )
        self._raise_for_status(response)
        return response.content

    def _apply_codes(self, tasks: list[MarkingTask], codes: list[str]) -> None:
        if len(codes) < len(tasks):
            raise AppError(
                f"Teksher returned {len(codes)} marking codes for {len(tasks)} tasks (GTIN {tasks[0].gtin})."
            )

        issued_at = datetime.now(timezone.utc)
        for task, code in zip(tasks, codes, strict=False):
            task.status = "issued"
            task.mark_code = code
            task.created_at = issued_at

    def _get_operation(self, operation_id: str, config: AppConfig) -> dict:
        response = self.session.get(
            self._url(config, f"/facade/api/v1/operations/{operation_id}"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        return self._json(response)

    def _is_operation_ready(self, operation_id: str, config: AppConfig) -> bool:
        response = self.session.get(
            self._url(config, f"/facade/api/v1/operations/{operation_id}/ready"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        return bool(payload.get("data"))

    def _upload_transgran_marking_file(self, csv_path: Path, config: AppConfig) -> str:
        with csv_path.open("rb") as handle:
            response = self.session.post(
                self._url(config, "/facade/transgran/api/v1/files/marking_code"),
                headers=self._headers(config, content_type=None),
                files={"file": (csv_path.name, handle, "text/csv")},
                timeout=30,
            )
        self._raise_for_status(response)
        payload = self._json(response)
        file_id = str((payload.get("data") or {}).get("id") or "")
        if not file_id:
            raise AppError(f"Teksher transgran file upload did not return file id: {payload}")
        return file_id

    def _create_transgran_operation(
        self,
        file_id: str,
        gtins: list[str],
        document_number: str,
        document_date: datetime,
        shipment_date: datetime,
        config: AppConfig,
    ) -> str:
        if not config.teksher_transgran_recipient_name:
            raise AppError("Teksher transgran recipient name is not configured.")
        if not config.teksher_transgran_recipient_inn:
            raise AppError("Teksher transgran recipient INN is not configured.")
        if not config.teksher_transgran_recipient_kpp:
            raise AppError("Teksher transgran recipient KPP is not configured.")

        payload = {
            "countryCode": config.teksher_transgran_country_code,
            "extension": config.teksher_extension,
            "documentNumber": document_number,
            "recipientName": config.teksher_transgran_recipient_name,
            "recipientKpp": config.teksher_transgran_recipient_kpp,
            "recipientInn": config.teksher_transgran_recipient_inn,
            "documentDate": document_date.isoformat(timespec="seconds"),
            "shipmentDate": shipment_date.isoformat(timespec="seconds"),
            "products": [{"gtin": gtin} for gtin in gtins],
            "fileId": file_id,
        }
        response = self.session.post(
            self._url(config, "/facade/transgran/api/v1/operations/create"),
            headers=self._headers(config),
            json=payload,
            timeout=30,
        )
        self._raise_for_status(response)
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                payload_response = response.json()
            except ValueError:
                payload_response = response.text.strip()
        else:
            payload_response = response.text.strip()
        operation_id = payload_response if isinstance(payload_response, str) else str(payload_response)
        if not operation_id:
            raise AppError("Teksher transgran create did not return an operation id.")
        return operation_id

    def _wait_for_notifications_unread(self, config: AppConfig) -> None:
        timeout_seconds = max(config.step_timeout_seconds, 30)
        deadline = time.monotonic() + timeout_seconds
        last_payload = None

        while time.monotonic() < deadline:
            try:
                payload = self._get_unread_notifications(config)
            except (AppError, requests.RequestException) as exc:
                self.logger(f"Teksher unread notifications polling transient error: {exc}")
                self.sleep(2.0)
                continue
            last_payload = payload
            self.logger(f"Teksher unread notifications payload={payload}")
            if payload.get("status") == "200" and bool(payload.get("data")):
                return
            self.sleep(2.0)

        raise AppError(
            "Teksher unread notifications endpoint did not return a successful result "
            f"within {timeout_seconds} seconds (last payload {last_payload})."
        )

    def _wait_for_operation_registration(self, operation_id: str, config: AppConfig) -> dict:
        timeout_seconds = max(config.step_timeout_seconds, 30)
        deadline = time.monotonic() + timeout_seconds
        last_details: dict | None = None

        while time.monotonic() < deadline:
            try:
                details = self._get_operation(operation_id, config)
            except (AppError, requests.RequestException) as exc:
                self.logger(f"Teksher operation {operation_id} registration transient error: {exc}")
                self.sleep(2.0)
                continue
            last_details = details
            status = str(details.get("status") or "unknown")
            operation_type = str(details.get("operationType") or "unknown")
            self.logger(
                f"Teksher operation {operation_id} registered: "
                f"type={operation_type}, status={status}"
            )
            if operation_type == "TRANSGRAN_REGISTRATION" and status in {"CREATED", "PROGRESS", "ACCEPTED"}:
                return details
            if status in {"REJECTED", "FAILED"}:
                raise AppError(f"Teksher operation {operation_id} finished with status {status}")
            self.sleep(2.0)

        raise AppError(
            "Teksher transgran operation was not registered successfully "
            f"within {timeout_seconds} seconds (last details {last_details})."
        )

    def resolve_transgran_dates(
        self,
        marking_operation_id: str,
        requested_document_date: datetime,
        requested_shipment_date: datetime,
        config: AppConfig,
    ) -> tuple[datetime, datetime]:
        details = self._get_operation(marking_operation_id, config)
        end_at_raw = str(details.get("endAt") or "")
        if not end_at_raw:
            return requested_document_date, requested_shipment_date

        end_at = datetime.fromisoformat(end_at_raw)
        minimum_allowed = end_at + timedelta(minutes=1)
        document_date = max(requested_document_date, minimum_allowed)
        shipment_date = max(requested_shipment_date, minimum_allowed)
        return document_date, shipment_date

    def _get_unread_notifications(self, config: AppConfig) -> dict:
        response = self.session.get(
            self._url(config, "/facade/api/v1/notifications/posts/participants/unread"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        return self._json(response)

    def _headers(
        self,
        config: AppConfig,
        accept: str = "application/json",
        content_type: str | None = "application/json",
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {config.teksher_api_token}",
            "Accept": accept,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    def _has_valid_token(self, config: AppConfig) -> bool:
        token = config.teksher_api_token.strip()
        if not token:
            return False
        exp = self._jwt_exp(token)
        if exp is None:
            return True
        return exp - int(time.time()) > 30

    def _jwt_exp(self, token: str) -> int | None:
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return None
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
            data = json.loads(decoded.decode("utf-8"))
            exp = data.get("exp")
            return int(exp) if exp is not None else None
        except Exception:
            return None

    def _url(self, config: AppConfig, path: str) -> str:
        return f"{config.teksher_url.rstrip('/')}{path}"

    def _json(self, response: requests.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            body = response.text.strip()
            if body:
                raise AppError(f"Teksher API returned a non-JSON response: {response.status_code} {body[:500]}") from exc
            raise AppError(f"Teksher API returned an empty response: {response.status_code}") from exc
        if not isinstance(payload, dict):
            raise AppError(f"Unexpected Teksher response: {payload!r}")
        return payload

    def _json_or_empty(self, response: requests.Response) -> dict | None:
        body = response.text.strip()
        if not body and not response.content:
            return None
        try:
            return self._json(response)
        except AppError:
            if not body:
                return None
            raise

    def _parse_codes(self, csv_bytes: bytes) -> list[str]:
        text = csv_bytes.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        return [line for line in text.split("\n") if line.strip()]

    def _raise_for_status(self, response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = response.text.strip()
            if body:
                raise AppError(f"Teksher API request failed: {response.status_code} {body}") from exc
            raise
