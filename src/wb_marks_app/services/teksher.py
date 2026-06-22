from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

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
        payload = response.json()
        if not isinstance(payload, dict):
            raise AppError(f"Unexpected Teksher response: {payload!r}")
        return payload

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
