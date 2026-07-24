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
from wb_marks_app.services.labels import extract_teksher_csv_mark_codes


Logger = Callable[[str], None]

TEKSHER_EXISTING_PRODUCT_MESSAGE = "В Текшер уже есть карточка с этим GTIN, данные НЕ СОХРАНЕНЫ"
TEKSHER_DEFAULT_GCP_LENGTH = 9
TEKSHER_POLL_INTERVAL_SECONDS = 7.0
TEKSHER_KYRGYZSTAN_COUNTRY_ID = 242
TEKSHER_SIZE_UNIT_INTERNATIONAL = "\u041c\u0415\u0416\u0414\u0423\u041d\u0410\u0420\u041e\u0414\u041d\u042b\u0419"
TEKSHER_VENDOR_ARTICLE_UNIT = "\u0410\u0440\u0442\u0438\u043a\u0443\u043b"
TEKSHER_DRAFT_PRODUCT_STATUSES = {"DRAFT", "CREATED", "NEW", "\u0427\u0415\u0420\u041D\u041E\u0412\u0418\u041A", "\u0421\u041E\u0417\u0414\u0410\u041D"}
TEKSHER_CLOTHING_REGULATION = (
    "\u0422\u0420 \u0422\u0421 017/2011 "
    '"\u041e \u0411\u0415\u0417\u041e\u041f\u0410\u0421\u041d\u041e\u0421\u0422\u0418 '
    "\u041f\u0420\u041e\u0414\u0423\u041a\u0426\u0418\u0418 \u041b\u0415\u0413\u041a\u041e\u0419 "
    '\u041f\u0420\u041e\u041c\u042b\u0428\u041b\u0415\u041d\u041d\u041e\u0421\u0422\u0418"'
)


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

    def fetch_company_gcp_profile(self, config: AppConfig) -> dict[str, str]:
        self.ensure_authenticated(config)
        current_user = self._current_user(config)
        participant = current_user.get("participant") if isinstance(current_user.get("participant"), dict) else {}
        participant_id = self._text(
            participant.get("id")
            or current_user.get("participantId")
            or current_user.get("participant_id")
            or current_user.get("trafficParticipantId")
        )

        if participant_id and not (self._participant_name(participant) and self._participant_gcp(participant)):
            detail = self._participant_detail(participant_id, config)
            if detail:
                participant = {**participant, **detail}

        name = self._participant_name(participant)
        gcp = self._participant_gcp(participant)
        if not name or not gcp:
            raise AppError("Teksher profile does not contain company name and GCP.")
        return {"name": name, "gcp": gcp, "display": f"{name} ({gcp})"}

    def ensure_product_drafts_for_mapping(
        self,
        product_card,
        rows_payload: list[dict],
        config: AppConfig,
        draft_fields: dict | None = None,
    ) -> list[str]:
        return self.ensure_product_drafts_result_for_mapping(product_card, rows_payload, config, draft_fields)[
            "draft_ids"
        ]

    def ensure_product_drafts_result_for_mapping(
        self,
        product_card,
        rows_payload: list[dict],
        config: AppConfig,
        draft_fields: dict | None = None,
    ) -> dict:
        rows = self._unique_gtin_rows(rows_payload)
        if not rows:
            return {
                "draft_ids": [],
                "created_gtins": [],
                "existing_gtins": [],
                "published_draft_ids": [],
                "publish_forbidden_ids": [],
            }

        self.ensure_authenticated(config)
        rows_to_create: list[dict] = []
        existing_gtins: list[str] = []
        published_draft_ids: list[str] = []
        publish_forbidden_ids: list[str] = []
        for row in rows:
            gtin = self._teksher_gtin(row.get("gtin"))
            existing_product = self._product_by_gtin(gtin, config)
            if existing_product is not None:
                existing_gtins.append(gtin)
                published_id, publish_status = self._approve_existing_product_if_draft(existing_product, config)
                if publish_status == "published":
                    published_draft_ids.append(published_id)
                elif publish_status == "forbidden":
                    publish_forbidden_ids.append(published_id)
                self.logger(f"Teksher product already exists for GTIN {gtin}; draft creation skipped.")
                continue
            rows_to_create.append(row)
        if not rows_to_create:
            return {
                "draft_ids": [],
                "created_gtins": [],
                "existing_gtins": existing_gtins,
                "published_draft_ids": published_draft_ids,
                "publish_forbidden_ids": publish_forbidden_ids,
            }

        manufacturer_info = self._manufacturer_create_fields(config)
        draft_ids: list[str] = []
        created_gtins: list[str] = []
        for row in self.rows_with_product_draft_fields(rows_to_create, draft_fields):
            payload = self._product_draft_payload(product_card, row, manufacturer_info, config)
            try:
                draft_id = self._create_product_draft(payload, config)
                gtin = self._text(payload.get("gtin"))
                product_id = draft_id or self._product_id_by_gtin(gtin, config)
                if not product_id:
                    raise AppError(f"Teksher did not return product id for GTIN {gtin}; publication cannot be completed.")
                publish_status = self._approve_product_draft(product_id, config)
                draft_ids.append(draft_id)
                if publish_status == "published":
                    published_draft_ids.append(product_id)
                elif publish_status == "forbidden":
                    publish_forbidden_ids.append(product_id)
                created_gtins.append(gtin)
            except AppError as exc:
                if self._is_existing_product_error(exc):
                    gtin = self._text(payload.get("gtin"))
                    existing_gtins.append(gtin)
                    published_id, publish_status = self._approve_existing_product_by_gtin_if_draft(gtin, config)
                    if publish_status == "published":
                        published_draft_ids.append(published_id)
                    elif publish_status == "forbidden":
                        publish_forbidden_ids.append(published_id)
                    self.logger(f"Teksher product already exists for GTIN {gtin}; draft creation skipped.")
                    continue
                raise
        return {
            "draft_ids": draft_ids,
            "created_gtins": created_gtins,
            "existing_gtins": existing_gtins,
            "published_draft_ids": published_draft_ids,
            "publish_forbidden_ids": publish_forbidden_ids,
        }

    def product_draft_preview_for_mapping(
        self,
        product_card,
        rows_payload: list[dict],
        config: AppConfig,
    ) -> dict:
        rows = self._unique_gtin_rows(rows_payload)
        if not rows:
            return {
                "existing_gtins": [],
                "create_gtins": [],
                "draft_fields": {},
                "dictionaries": {},
            }

        self.ensure_authenticated(config)
        rows_to_create: list[dict] = []
        existing_gtins: list[str] = []
        for row in rows:
            gtin = self._teksher_gtin(row.get("gtin"))
            if self._product_exists_by_gtin(gtin, config):
                existing_gtins.append(gtin)
            else:
                rows_to_create.append(row)

        draft_fields: dict[str, str] = {}
        dictionaries: dict[str, list[dict[str, str]]] = {}
        if rows_to_create:
            manufacturer_info = self._manufacturer_create_fields(config)
            draft_fields = self._product_draft_form_fields(product_card, rows_to_create[0], manufacturer_info, config)
            dictionaries = self._product_draft_form_dictionaries(draft_fields, config)

        return {
            "existing_gtins": existing_gtins,
            "create_gtins": [self._teksher_gtin(row.get("gtin")) for row in rows_to_create],
            "draft_fields": draft_fields,
            "dictionaries": dictionaries,
        }

    def rows_with_product_draft_fields(self, rows_payload: list[dict], draft_fields: dict | None) -> list[dict]:
        if not isinstance(draft_fields, dict) or not draft_fields:
            return list(rows_payload)
        result: list[dict] = []
        for row in rows_payload:
            if not isinstance(row, dict):
                continue
            merged = dict(row)
            size_value, current_unit = self._teksher_size_parts(
                self._text(merged.get("teksher_size")) or self._text(merged.get("wb_size"))
            )
            field_map = {
                "full_name": "full_name",
                "tnved": "tnved",
                "country": "country",
                "manufacturer_inn": "manufacturer_inn",
                "manufacturer_full_name": "manufacturer_full_name",
                "trademark": "trademark",
                "product_type": "product_type",
                "vendor_article": "vendor_article",
                "regulation": "regulation",
                "composition": "composition",
                "color": "color",
                "target_gender": "target_gender",
            }
            for source_key, target_key in field_map.items():
                value = self._text(draft_fields.get(source_key))
                if value:
                    merged[target_key] = value
            size_unit = self._text(draft_fields.get("size_unit")) or current_unit
            if size_value and size_unit:
                merged["teksher_size"] = f"{size_value} {size_unit}"
                merged["size_unit"] = size_unit
            result.append(merged)
        return result

    def product_exists_by_gtin(self, gtin: str, config: AppConfig) -> bool:
        self.ensure_authenticated(config)
        return self._product_exists_by_gtin(gtin, config)

    def product_mapping_rows_by_gtins(self, gtins: list[str], config: AppConfig) -> dict[str, dict]:
        self.ensure_authenticated(config)
        result: dict[str, dict] = {}
        for gtin in self._unique_values(gtins):
            query_gtin = self._teksher_gtin(gtin)
            product = self._product_by_gtin(query_gtin, config)
            if product is None:
                continue
            row = self._product_mapping_row(product, config)
            row_gtin = self._text(row.get("gtin")) or query_gtin or gtin
            row["gtin"] = row_gtin
            result[row_gtin] = row
            result[gtin] = row
            if query_gtin != gtin:
                result[query_gtin] = row
        return result

    def create_mark_code_order(self, gtin: str, quantity: int, config: AppConfig) -> str:
        self.ensure_authenticated(config)
        payload = self._create_mark_code_orders({gtin: [object()] * quantity}, config)
        order_id = next(iter(payload.keys()), "")
        if not order_id:
            raise AppError(f"Teksher did not return an order id for GTIN {gtin}.")
        return order_id

    def ensure_gtin_ready_for_mark_order(self, gtin: str, config: AppConfig) -> None:
        self.ensure_authenticated(config)
        self._ensure_gtin_ready_for_mark_order(gtin, config)

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
        normalized_gtin = self._teksher_gtin(gtin)
        original_gtin = self._text(gtin)
        if not normalized_gtin:
            return False
        response = self.session.get(
            self._url(config, f"/facade/api/v1/products?gtin={quote(normalized_gtin)}"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json_or_empty(response)
        if payload is None:
            return False
        for record in self._extract_records(payload):
            for key in ("gtin", "GTIN", "gtinCode", "productGtin"):
                if self._text(record.get(key)) in {normalized_gtin, original_gtin}:
                    return True
        return False

    def _current_user(self, config: AppConfig) -> dict:
        response = self.session.get(
            self._url(config, "/facade/api/v1/users/getCurrentUser"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            raise AppError(f"Unexpected Teksher current user response: {payload!r}")
        return data

    def _participant_detail(self, participant_id: str, config: AppConfig) -> dict:
        response = self.session.get(
            self._url(config, f"/facade/api/v1/participants/{quote(participant_id)}"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return {}
        return data

    def _participant_name(self, participant: dict) -> str:
        return self._text(
            participant.get("fullName")
            or participant.get("full_name")
            or participant.get("name")
            or participant.get("title")
        )

    def _participant_gcp(self, participant: dict) -> str:
        for key in ("gcp", "GCP", "gcpCode", "prefixGcp"):
            value = self._digits(participant.get(key))
            if value:
                return value

        identifiers = self._coerce_dict_list(
            participant.get("identifiers") or participant.get("participantIdentifiers")
        )
        final_identifiers = [item for item in identifiers if item.get("isFinal") is True]
        for item in final_identifiers + identifiers:
            value = self._digits(item.get("gcp") or item.get("GCP") or item.get("gcpCode"))
            if value:
                return value
        return ""

    def _product_by_gtin(self, gtin: str, config: AppConfig) -> dict | None:
        normalized_gtin = self._teksher_gtin(gtin)
        original_gtin = self._text(gtin)
        if not normalized_gtin:
            return None
        response = self.session.get(
            self._url(config, f"/facade/api/v1/products?gtin={quote(normalized_gtin)}"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json_or_empty(response)
        if payload is None:
            return None
        product = None
        for record in self._extract_product_records(payload):
            candidate = self._text(
                record.get("gtin") or record.get("GTIN") or record.get("gtinCode") or record.get("productGtin")
            )
            if candidate in {normalized_gtin, original_gtin}:
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
                if not self._text(detail.get("status")) and self._text(product.get("status")):
                    detail["status"] = product.get("status")
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
            "full_name": self._text(
                product.get("fullName")
                or product.get("full_name")
                or product.get("productFullName")
                or product.get("name")
                or product.get("title")
            ),
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

    def _approve_product_draft(self, product_id: str, config: AppConfig) -> str:
        response = self.session.put(
            self._url(config, f"/facade/api/v1/products/{quote(product_id)}/approve"),
            headers=self._headers(config),
            timeout=30,
        )
        if response.status_code == 403:
            self.logger(f"Teksher product {product_id} publication is forbidden for the current account.")
            return "forbidden"
        self._raise_for_status(response)
        return "published"

    def _approve_existing_product_by_gtin_if_draft(self, gtin: str, config: AppConfig) -> tuple[str, str]:
        product = self._product_by_gtin(gtin, config)
        if product is None:
            return "", ""
        return self._approve_existing_product_if_draft(product, config)

    def _approve_existing_product_if_draft(self, product: dict, config: AppConfig) -> tuple[str, str]:
        if not self._is_draft_product(product):
            return "", ""
        product_id = self._product_id_from_response({"data": product})
        if not product_id:
            return "", ""
        return product_id, self._approve_product_draft(product_id, config)

    def _is_draft_product(self, product: dict) -> bool:
        status = self._text(product.get("status") or product.get("state") or product.get("productStatus")).upper()
        return status in TEKSHER_DRAFT_PRODUCT_STATUSES

    def _product_id_by_gtin(self, gtin: str, config: AppConfig) -> str:
        product = self._product_by_gtin(gtin, config)
        if product is None:
            return ""
        return self._product_id_from_response({"data": product})

    def _product_draft_payload(
        self,
        product_card,
        row: dict,
        manufacturer_info: dict[str, str],
        config: AppConfig,
    ) -> dict:
        tnved = self._text(row.get("tnved")) or self._text(product_card.wb_summary.tnved)
        country = self._text(row.get("country")) or self._text(product_card.wb_summary.country)
        gtin = self._teksher_gtin(row.get("gtin"))
        manufacturer_source = dict(manufacturer_info)
        manufacturer_source["manufacturerFullName"] = (
            self._text(row.get("manufacturer_full_name"))
            or self._text(row.get("manufacturerFullName"))
            or self._text(manufacturer_source.get("manufacturerFullName"))
        )
        manufacturer_source["manufacturerInn"] = (
            self._text(row.get("manufacturer_inn"))
            or self._text(row.get("manufacturerInn"))
            or self._text(manufacturer_source.get("manufacturerInn"))
        )
        manufacturer = self._manufacturer_fields_for_gtin(manufacturer_source, gtin)
        payload = {
            "gtin": gtin,
            "fullName": self._product_full_name(product_card, row),
            "manufacturerFullName": manufacturer["manufacturerFullName"],
            "manufacturerInn": manufacturer["manufacturerInn"],
            "gcp": manufacturer["gcp"],
            "gln": manufacturer["gln"],
            "manufacturedCountryId": self._resolve_country_id(country, config),
            "tnved": self._resolve_tnved_id(tnved, config),
            "trademark": self._text(row.get("trademark")) or self._text(product_card.wb_summary.brand),
            "attributes": self._product_draft_attributes(product_card, row),
        }
        return payload

    def _product_draft_form_fields(
        self,
        product_card,
        row: dict,
        manufacturer_info: dict[str, str],
        config: AppConfig,
    ) -> dict[str, str]:
        gtin = self._teksher_gtin(row.get("gtin"))
        manufacturer = self._manufacturer_fields_for_gtin(manufacturer_info, gtin)
        size_value, size_unit = self._teksher_size_parts(
            self._text(row.get("teksher_size")) or self._text(row.get("wb_size"))
        )
        country = self._text(row.get("country")) or self._text(product_card.wb_summary.country)
        if self._is_kyrgyzstan_country(country):
            country = "КЫРГЫЗСТАН"
        return {
            "full_name": self._product_full_name(product_card, row),
            "tnved": self._text(row.get("tnved")) or self._text(product_card.wb_summary.tnved),
            "country": country,
            "manufacturer_inn": manufacturer["manufacturerInn"],
            "manufacturer_full_name": manufacturer["manufacturerFullName"],
            "trademark": self._text(row.get("trademark")) or self._text(product_card.wb_summary.brand),
            "product_type": self._product_type(product_card, row),
            "vendor_article": self._text(row.get("vendor_article")) or self._text(product_card.wb_summary.seller_article),
            "regulation": self._text(row.get("regulation")) or TEKSHER_CLOTHING_REGULATION,
            "size_unit": self._text(row.get("size_unit")) or size_unit or TEKSHER_SIZE_UNIT_INTERNATIONAL,
            "composition": self._text(row.get("composition")) or self._text(product_card.wb_summary.composition),
            "color": self._uppercase_text(self._text(row.get("color")) or self._text(product_card.wb_summary.color)),
            "target_gender": self._text(row.get("target_gender")) or self._target_gender_for_create(product_card.wb_summary.gender),
        }

    def _product_full_name(self, product_card, row: dict) -> str:
        full_name = self._text(row.get("full_name"))
        if full_name:
            return full_name
        full_name = self._capitalize_text(
            self._text(row.get("functional_name"))
            or self._product_type(product_card, row)
            or self._text(product_card.wb_summary.name)
        )
        if full_name:
            return full_name
        return f"WB {self._text(product_card.wb_article)} {self._text(row.get('gtin'))}".strip()

    def _product_draft_attributes(self, product_card, row: dict) -> list[dict]:
        attributes: list[dict] = []
        product_type = self._product_type(product_card, row)
        size_value, size_unit = self._teksher_size_parts(
            self._text(row.get("teksher_size")) or self._text(row.get("wb_size"))
        )
        color = self._uppercase_text(self._text(row.get("color")) or self._text(product_card.wb_summary.color))
        composition = self._text(row.get("composition")) or self._text(product_card.wb_summary.composition)
        target_gender = self._text(row.get("target_gender")) or self._target_gender_for_create(product_card.wb_summary.gender)
        vendor_article = self._text(row.get("vendor_article")) or self._text(product_card.wb_summary.seller_article)
        regulation = self._text(row.get("regulation")) or TEKSHER_CLOTHING_REGULATION

        self._append_product_attribute(attributes, "12", product_type)
        self._append_product_attribute(attributes, "35", size_value, self._text(row.get("size_unit")) or size_unit)
        self._append_product_attribute(attributes, "36", color)
        self._append_product_attribute(attributes, "2483", composition)
        self._append_product_attribute(attributes, "14013", target_gender)
        self._append_product_attribute(attributes, "13914", vendor_article, TEKSHER_VENDOR_ARTICLE_UNIT)
        self._append_product_attribute(attributes, "13836", regulation)
        return attributes

    def _product_type(self, product_card, row: dict) -> str:
        return self._uppercase_text(self._text(row.get("product_type")) or self._text(product_card.wb_summary.seller_category))

    def _uppercase_text(self, value: str) -> str:
        return self._text(value).upper()

    def _capitalize_text(self, value: str) -> str:
        text = self._text(value)
        if not text:
            return ""
        lowered = text.lower()
        return lowered[:1].upper() + lowered[1:]

    def _append_product_attribute(
        self,
        attributes: list[dict],
        code: str,
        value: str,
        unit_code: str = "",
    ) -> None:
        text = self._text(value)
        if not text:
            return
        attributes.append(
            {
                "attributeTypeCode": code,
                "value": text,
                "unitCode": self._text(unit_code),
                "dataType": 0,
            }
        )

    def _teksher_size_parts(self, value: str) -> tuple[str, str]:
        text = self._text(value)
        if not text:
            return "", ""
        normalized_unit = self._normalize_match(TEKSHER_SIZE_UNIT_INTERNATIONAL)
        parts = text.rsplit(" ", 1)
        if len(parts) == 2 and self._normalize_match(parts[1]) == normalized_unit:
            return parts[0].strip(), TEKSHER_SIZE_UNIT_INTERNATIONAL
        return text, TEKSHER_SIZE_UNIT_INTERNATIONAL

    def _manufacturer_create_fields(self, config: AppConfig) -> dict[str, str]:
        response = self.session.get(
            self._url(config, "/facade/api/v1/participants/manufacturer_info?isPresentManufacturerInfo=true"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json_or_empty(response)
        data = self._payload_data(payload)
        manufacturer = self._manufacturer_fields_from_payload(data)
        if not manufacturer["manufacturerFullName"] or not manufacturer["manufacturerInn"]:
            fallback = self._current_user_participant_fields(config)
            for key, value in fallback.items():
                manufacturer[key] = manufacturer[key] or value
        missing = [key for key in ("manufacturerFullName", "manufacturerInn") if not manufacturer[key]]
        if missing:
            raise AppError(
                "Не удалось получить реквизиты производителя Текшер для создания карточки: "
                + ", ".join(missing)
                + "."
            )
        return manufacturer

    def _manufacturer_fields_from_payload(self, data) -> dict[str, str]:
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
        return manufacturer

    def _current_user_participant_fields(self, config: AppConfig) -> dict[str, str]:
        response = self.session.get(
            self._url(config, "/facade/api/v1/users/getCurrentUser"),
            headers=self._headers(config),
            timeout=30,
        )
        self._raise_for_status(response)
        payload = self._json_or_empty(response)
        data = self._payload_data(payload)
        if isinstance(data, dict) and isinstance(data.get("participant"), dict):
            return self._manufacturer_fields_from_payload(data["participant"])
        return self._manufacturer_fields_from_payload(data)

    def _payload_data(self, payload):
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def _manufacturer_fields_for_gtin(self, manufacturer_info: dict[str, str], gtin: str) -> dict[str, str]:
        manufacturer = dict(manufacturer_info)
        manufacturer["gcp"] = self._text(manufacturer.get("gcp")) or self._derive_gcp_from_gtin(gtin)
        manufacturer["gln"] = self._text(manufacturer.get("gln")) or self._derive_gln_from_gcp(manufacturer["gcp"])
        missing = [key for key, value in manufacturer.items() if not self._text(value)]
        if missing:
            raise AppError(
                "Не удалось получить реквизиты производителя Текшер для создания карточки: "
                + ", ".join(missing)
                + "."
            )
        return manufacturer

    def _derive_gcp_from_gtin(self, gtin: str) -> str:
        digits = self._digits(gtin)
        if len(digits) == 14 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) < TEKSHER_DEFAULT_GCP_LENGTH:
            return ""
        return digits[:TEKSHER_DEFAULT_GCP_LENGTH]

    def _derive_gln_from_gcp(self, gcp: str) -> str:
        digits = self._digits(gcp)
        if not digits:
            return ""
        base = (digits + "0" * 12)[:12]
        return base + self._gs1_check_digit(base)

    def _teksher_gtin(self, value) -> str:
        text = self._text(value)
        digits = self._digits(text)
        if len(digits) == 13:
            return "0" + digits
        if len(digits) == 14:
            return digits
        return text

    def _gs1_check_digit(self, base: str) -> str:
        total = 0
        for index, char in enumerate(reversed(self._digits(base))):
            total += int(char) * (3 if index % 2 == 0 else 1)
        return str((10 - total % 10) % 10)

    def _target_gender_for_create(self, value: str) -> str:
        text = self._text(value)
        normalized = self._normalize_match(text)
        mapping = {
            self._normalize_match("\u043c\u0430\u043b\u044c\u0447\u0438\u043a\u0438"): "\u041c\u0423\u0416\u0421\u041a\u041e\u0419",
            self._normalize_match("\u0434\u0435\u0432\u043e\u0447\u043a\u0438"): "\u0416\u0415\u041d\u0421\u041a\u0418\u0419",
            self._normalize_match("\u043c\u0443\u0436\u0441\u043a\u043e\u0439"): "\u041c\u0423\u0416\u0421\u041a\u041e\u0419",
            self._normalize_match("\u0436\u0435\u043d\u0441\u043a\u0438\u0439"): "\u0416\u0415\u041d\u0421\u041a\u0418\u0419",
            self._normalize_match("\u0434\u0435\u0442\u0441\u043a\u0438\u0439"): "\u0423\u041d\u0418\u0412\u0415\u0420\u0421\u0410\u041b\u042c\u041d\u042b\u0419 (\u0423\u041d\u0418\u0421\u0415\u041a\u0421)",
        }
        return mapping.get(normalized, text)

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
        digits = self._digits(value)
        if digits:
            try:
                return self._dictionary_id(
                    value,
                    self._dictionary_items(
                        f"/facade/api/v1/tnveds?page=0&size=10&tnvedCode={quote(digits)}&rootCode=",
                        config,
                    ),
                    ("code", "tnved", "tnvedCode", "name", "title", "label", "value"),
                    "ТНВЭД",
                )
            except AppError:
                pass
        return self._dictionary_id(
            value,
            self._dictionary_items("/facade/api/v1/tnveds", config),
            ("code", "tnved", "tnvedCode", "name", "title", "label", "value"),
            "ТНВЭД",
        )

    def _resolve_country_id(self, value: str, config: AppConfig) -> int:
        country = self._text(value)
        items = self._dictionary_items("/facade/api/v1/countries", config)
        keys = ("name", "nameRu", "shortName", "title", "label", "code", "alpha2", "alpha3", "countryCode")
        if self._is_kyrgyzstan_country(country):
            for alias in ("KG", "КЫРГЫЗСТАН", "Кыргызстан"):
                try:
                    return self._dictionary_id(alias, items, keys, "страну производства")
                except AppError:
                    pass
            return TEKSHER_KYRGYZSTAN_COUNTRY_ID
        return self._dictionary_id(country, items, keys, "страну производства")

    def _is_kyrgyzstan_country(self, value: str) -> bool:
        normalized = self._normalize_match(value)
        return normalized in {
            self._normalize_match(alias)
            for alias in (
                "kg",
                "kyrgyzstan",
                "kyrgyz republic",
                "кыргызстан",
                "киргизия",
                "киргизская республика",
            )
        }

    def _product_draft_form_dictionaries(self, draft_fields: dict[str, str], config: AppConfig) -> dict[str, list[dict[str, str]]]:
        options: dict[str, list[dict[str, str]]] = {
            "tnved": self._safe_tnved_options(draft_fields.get("tnved", ""), config),
            "country": self._safe_dictionary_options(
                "/facade/api/v1/countries",
                ("name", "nameRu", "shortName", "title", "label", "code", "alpha2", "alpha3", "countryCode"),
                config,
            ),
        }
        attribute_options = self._safe_attribute_template_options(draft_fields.get("tnved", ""), config)
        options["product_type"] = attribute_options.get("12", [])
        options["size_unit"] = attribute_options.get("35:unit", [{"value": TEKSHER_SIZE_UNIT_INTERNATIONAL, "label": ""}])
        options["color"] = attribute_options.get("36", [])
        options["target_gender"] = attribute_options.get("14013", [])
        options["regulation"] = attribute_options.get("13836", [{"value": TEKSHER_CLOTHING_REGULATION, "label": ""}])
        for key, value in draft_fields.items():
            if key in options:
                options[key] = self._options_with_current(options[key], value)
        return options

    def _safe_tnved_options(self, value: str, config: AppConfig) -> list[dict[str, str]]:
        try:
            digits = self._digits(value)
            path = (
                f"/facade/api/v1/tnveds?page=0&size=10&tnvedCode={quote(digits)}&rootCode="
                if digits
                else "/facade/api/v1/tnveds?page=0&size=10&tnvedCode=&rootCode="
            )
            return self._tnved_options_from_items(self._dictionary_items(path, config))
        except AppError:
            return self._options_with_current([], value)

    def _safe_dictionary_options(
        self,
        path: str,
        value_keys: tuple[str, ...],
        config: AppConfig,
    ) -> list[dict[str, str]]:
        try:
            return self._dictionary_options_from_items(self._dictionary_items(path, config), value_keys)
        except AppError:
            return []

    def _safe_attribute_template_options(self, tnved: str, config: AppConfig) -> dict[str, list[dict[str, str]]]:
        try:
            subgroup_id = self._tnved_product_subgroup_id(tnved, config) or "2"
            response = self.session.get(
                self._url(config, f"/facade/api/v1/products/attribute_templates?subgroupId={quote(subgroup_id)}"),
                headers=self._headers(config),
                timeout=30,
            )
            self._raise_for_status(response)
            payload = self._json_value(response)
        except AppError:
            return {}

        result: dict[str, list[dict[str, str]]] = {}
        templates = payload if isinstance(payload, list) else []
        for item in templates:
            if not isinstance(item, dict):
                continue
            attribute_type = item.get("attributeType")
            if not isinstance(attribute_type, dict):
                continue
            code = self._text(attribute_type.get("code"))
            if not code:
                continue
            values = attribute_type.get("values")
            if isinstance(values, list):
                result[code] = self._plain_value_options(values)
            unit_codes = attribute_type.get("unitCodes")
            if isinstance(unit_codes, list):
                result[f"{code}:unit"] = self._plain_value_options(unit_codes)
        return result

    def _tnved_product_subgroup_id(self, value: str, config: AppConfig) -> str:
        digits = self._digits(value)
        if not digits:
            return ""
        items = self._dictionary_items(
            f"/facade/api/v1/tnveds?page=0&size=10&tnvedCode={quote(digits)}&rootCode=",
            config,
        )
        for item in items:
            if self._digits(self._first_record_text(item, "code", "tnved", "tnvedCode")) != digits:
                continue
            for record in self._walk_dicts(item):
                if any(self._key_token(str(key)) == "productsubgroup" for key in record.keys()):
                    subgroup = record.get("productSubgroup") or record.get("product_subgroup")
                    if isinstance(subgroup, dict):
                        subgroup_id = self._extract_dict_id(subgroup)
                        if subgroup_id is not None:
                            return str(subgroup_id)
            for record in self._walk_dicts(item):
                if self._text(record.get("alias")) or self._text(record.get("name")):
                    subgroup_id = self._extract_dict_id(record)
                    if subgroup_id is not None and self._text(record.get("code")) in {"01", "02"}:
                        return str(subgroup_id)
        return ""

    def _tnved_options_from_items(self, items: list[dict]) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        for item in items[:50]:
            code = self._first_record_text(item, "code", "tnved", "tnvedCode", "value")
            if not code:
                continue
            name = self._first_record_text(item, "name", "title", "label")
            options.append({"value": code, "label": name})
        return self._dedupe_options(options)

    def _dictionary_options_from_items(self, items: list[dict], value_keys: tuple[str, ...]) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        for item in items[:700]:
            values = self._dictionary_value_texts(item, value_keys)
            value = values[0] if values else self._text(self._extract_dict_id(item))
            if not value:
                continue
            label_values = [entry for entry in values[1:4] if self._normalize_match(entry) != self._normalize_match(value)]
            options.append({"value": value, "label": " / ".join(label_values)})
        return self._dedupe_options(options)

    def _plain_value_options(self, values: list) -> list[dict[str, str]]:
        return self._dedupe_options([{"value": self._text(value), "label": ""} for value in values if self._text(value)])

    def _options_with_current(self, options: list[dict[str, str]], current: str) -> list[dict[str, str]]:
        text = self._text(current)
        if not text:
            return self._dedupe_options(options)
        return self._dedupe_options([{"value": text, "label": ""}, *options])

    def _dedupe_options(self, options: list[dict[str, str]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for option in options:
            value = self._text(option.get("value"))
            if not value:
                continue
            key = self._normalize_match(value)
            if key in seen:
                continue
            seen.add(key)
            result.append({"value": value, "label": self._text(option.get("label"))})
        return result

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
        payload = self._json_value(response)
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
        for gtin in sorted(grouped_tasks):
            self._ensure_gtin_ready_for_mark_order(gtin, config)

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

    def _ensure_gtin_ready_for_mark_order(self, gtin: str, config: AppConfig) -> None:
        normalized_gtin = self._teksher_gtin(gtin)
        if not normalized_gtin:
            return
        product = self._product_by_gtin(normalized_gtin, config)
        if product is None or not self._is_draft_product(product):
            return

        raise AppError(
            "\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 GTIN "
            f"{normalized_gtin} \u0432 \u0422\u0435\u043a\u0448\u0435\u0440 "
            "\u043d\u0430\u0445\u043e\u0434\u0438\u0442\u0441\u044f \u0432 \u0441\u0442\u0430\u0442\u0443\u0441\u0435 "
            "\u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a (DRAFT). "
            "\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u0443\u0439\u0442\u0435 "
            "\u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443 \u043f\u0435\u0440\u0435\u0434 "
            "\u043f\u0435\u0447\u0430\u0442\u044c\u044e \u044d\u0442\u0438\u043a\u0435\u0442\u043e\u043a."
        )

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
                self.sleep(self._poll_interval_seconds())
                continue
            self.logger(
                f"Teksher order {order_id} status={last_status}, ready={ready}, endAt={last_end_at or '-'}"
            )
            if last_status == "ACCEPTED" and (ready or last_end_at):
                return
            if last_status in {"REJECTED", "FAILED"}:
                raise AppError(f"Teksher order {order_id} finished with status {last_status}")
            self.sleep(self._poll_interval_seconds())

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
                self.sleep(self._poll_interval_seconds())
                continue
            self.logger(f"Teksher operation {operation_id} status={last_status}")
            if last_status == expected_status:
                return
            if last_status in {"REJECTED", "FAILED"}:
                raise AppError(f"Teksher operation {operation_id} finished with status {last_status}")
            self.sleep(self._poll_interval_seconds())

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
            self.sleep(self._poll_interval_seconds())

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
                self.sleep(self._poll_interval_seconds())
                continue
            last_payload = payload
            self.logger(f"Teksher unread notifications payload={payload}")
            if payload.get("status") == "200" and bool(payload.get("data")):
                return
            self.sleep(self._poll_interval_seconds())

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
                self.sleep(self._poll_interval_seconds())
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
            self.sleep(self._poll_interval_seconds())

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

    def _poll_interval_seconds(self) -> float:
        return TEKSHER_POLL_INTERVAL_SECONDS

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

    def _json_value(self, response: requests.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            body = response.text.strip()
            if body:
                raise AppError(f"Teksher API returned a non-JSON response: {response.status_code} {body[:500]}") from exc
            raise AppError(f"Teksher API returned an empty response: {response.status_code}") from exc

    def _json(self, response: requests.Response) -> dict:
        payload = self._json_value(response)
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
        return extract_teksher_csv_mark_codes(text)

    def _raise_for_status(self, response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = response.text.strip()
            if body:
                raise AppError(f"Teksher API request failed: {response.status_code} {body}") from exc
            raise
