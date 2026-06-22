from pathlib import Path
from tempfile import TemporaryDirectory
import base64
import json
import time
from datetime import datetime
import unittest

from wb_marks_app.models import AppConfig, MarkingTask
from wb_marks_app.services.teksher import TeksherService
from wb_marks_app.teksher_api_cli import run_full_flow


class FakeBrowser:
    def __init__(self) -> None:
        self.opened_urls: list[str] = []

    def open_url(self, url: str) -> None:
        self.opened_urls.append(url)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        json_data=None,
        content: bytes = b"",
        text: str = "",
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text or ("" if json_data is None else str(json_data))
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            from requests import HTTPError

            raise HTTPError(response=self)


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object | None]] = []
        self.ready_checks = 0

    def post(self, url, headers=None, json=None, files=None, timeout=None):
        self.calls.append(("POST", url, json if json is not None else files))
        if url.endswith("/facade/oauth/login"):
            return FakeResponse(
                200,
                json_data={
                    "status": "SUCCESS",
                    "data": {
                        "access_token": _future_token(),
                        "refresh_token": "refresh-token",
                    },
                },
            )
        if url.endswith("/facade/order/api/v1/operations/multi"):
            return FakeResponse(
                202,
                json_data={
                    "status": "202",
                    "data": {
                        "order-op-1": "GTIN1",
                    },
                },
            )
        if url.endswith("/facade/order/api/v1/operations/utilisation"):
            return FakeResponse(
                202,
                json_data={
                    "status": "202",
                    "data": "marking-op-1",
                },
            )
        if url.endswith("/facade/transgran/api/v1/files/marking_code"):
            return FakeResponse(
                201,
                json_data={
                    "status": "201",
                    "data": {
                        "id": "file-1",
                        "name": "codes.csv",
                        "rows": 2,
                        "gtins": ["GTIN1"],
                    },
                },
            )
        if url.endswith("/facade/transgran/api/v1/operations/create"):
            return FakeResponse(201, json_data="transgran-op-1", text="transgran-op-1")
        raise AssertionError(f"Unexpected POST {url}")

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None))
        if url.endswith("/facade/api/v1/operations/order-op-1"):
            self.ready_checks += 1
            status = "PROGRESS" if self.ready_checks == 1 else "ACCEPTED"
            return FakeResponse(200, json_data={"id": "order-op-1", "status": status})
        if url.endswith("/facade/api/v1/operations/transgran-op-1"):
            return FakeResponse(
                200,
                json_data={
                    "id": "transgran-op-1",
                    "operationType": "TRANSGRAN_REGISTRATION",
                    "status": "PROGRESS",
                },
            )
        if url.endswith("/facade/api/v1/operations/marking-op-1"):
            return FakeResponse(
                200,
                json_data={
                    "id": "marking-op-1",
                    "status": "ACCEPTED",
                    "endAt": "2026-04-24T19:40:00",
                },
            )
        if url.endswith("/facade/api/v1/operations/order-op-1/ready"):
            return FakeResponse(200, json_data={"status": "200", "data": self.ready_checks >= 2})
        if url.endswith("/facade/api/v1/notifications/posts/participants/unread"):
            return FakeResponse(200, json_data={"status": "200", "message": None, "data": True})
        if url.endswith("/facade/api/v1/marking_codes/csv?operationId=marking-op-1"):
            return FakeResponse(200, content=b"MARK-1\nMARK-2\n")
        raise AssertionError(f"Unexpected GET {url}")


class FakeSessionResume(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.transgran_attempts = 0

    def post(self, url, headers=None, json=None, files=None, timeout=None):
        if url.endswith("/facade/transgran/api/v1/operations/create"):
            self.transgran_attempts += 1
            if self.transgran_attempts == 1:
                return FakeResponse(
                    400,
                    json_data={"error": "Bad Request", "message": "codes unavailable"},
                    text='{"error":"Bad Request","message":"codes unavailable"}',
                )
        return super().post(url, headers=headers, json=json, files=files, timeout=timeout)


class FakeSessionOrderReadyFalse(FakeSession):
    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None))
        if url.endswith("/facade/api/v1/operations/order-op-1"):
            return FakeResponse(
                200,
                json_data={
                    "id": "order-op-1",
                    "status": "ACCEPTED",
                    "endAt": "2026-04-24T22:42:10.621421",
                },
            )
        if url.endswith("/facade/api/v1/operations/order-op-1/ready"):
            return FakeResponse(200, json_data={"status": "200", "data": False})
        return super().get(url, headers=headers, timeout=timeout)

def _future_token() -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {"exp": int(time.time()) + 3600}
    return ".".join(
        (
            _b64(json.dumps(header).encode("utf-8")),
            _b64(json.dumps(payload).encode("utf-8")),
            "signature",
        )
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class TeksherServiceTests(unittest.TestCase):
    def test_issue_marks_uses_api_and_assigns_codes(self) -> None:
        service = TeksherService(
            browser=FakeBrowser(),
            session=FakeSession(),
            sleep=lambda _: None,
        )
        config = AppConfig(
            teksher_api_token="token",
            step_timeout_seconds=30,
        )
        tasks = [
            MarkingTask(
                task_id="1",
                barcode="b1",
                gtin="GTIN1",
                wb_item_name="Item",
                quantity_index=1,
                supply_id="SUP",
            ),
            MarkingTask(
                task_id="2",
                barcode="b1",
                gtin="GTIN1",
                wb_item_name="Item",
                quantity_index=2,
                supply_id="SUP",
            ),
        ]

        result = service.issue_marks(tasks, config)

        self.assertEqual(["MARK-1", "MARK-2"], [task.mark_code for task in result])
        self.assertTrue(all(task.status == "issued" for task in result))

    def test_run_full_cycle_saves_csv(self) -> None:
        service = TeksherService(
            browser=FakeBrowser(),
            session=FakeSession(),
            sleep=lambda _: None,
        )
        config = AppConfig(
            teksher_api_token="token",
            output_dir="",
            step_timeout_seconds=30,
        )

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "codes.csv"
            result = service.run_full_cycle("GTIN1", 2, config, destination)

            self.assertEqual(destination, result)
            self.assertTrue(destination.exists())
            self.assertEqual("MARK-1\nMARK-2\n", destination.read_text(encoding="utf-8"))

    def test_run_full_cycle_fetches_token_from_credentials(self) -> None:
        session = FakeSession()
        service = TeksherService(
            browser=FakeBrowser(),
            session=session,
            sleep=lambda _: None,
        )
        config = AppConfig(
            teksher_username="user02562",
            teksher_password="secret",
            teksher_api_token="",
            step_timeout_seconds=30,
        )

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "codes.csv"
            service.run_full_cycle("GTIN1", 2, config, destination)

        self.assertTrue(config.teksher_api_token)
        self.assertTrue(
            any(call[0] == "POST" and call[1].endswith("/facade/oauth/login") for call in session.calls)
        )

    def test_run_transgran_cycle_creates_operation(self) -> None:
        service = TeksherService(
            browser=FakeBrowser(),
            session=FakeSession(),
            sleep=lambda _: None,
        )
        config = AppConfig(
            teksher_api_token=_future_token(),
            teksher_transgran_recipient_name='ООО "РВБ"',
            teksher_transgran_recipient_inn="9714053621",
            teksher_transgran_recipient_kpp="507401001",
        )

        with TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "codes.csv"
            csv_path.write_text("MARK-1\n", encoding="utf-8")
            operation_id = service.run_transgran_cycle(
                csv_path=csv_path,
                gtins=["GTIN1"],
                document_number="123",
                document_date=datetime.fromisoformat("2026-04-24T19:31:39"),
                shipment_date=datetime.fromisoformat("2026-04-24T19:31:41"),
                config=config,
            )

        self.assertEqual("transgran-op-1", operation_id)
        self.assertTrue(
            any(call[0] == "GET" and call[1].endswith("/facade/api/v1/notifications/posts/participants/unread") for call in service.session.calls)
        )
        self.assertTrue(
            any(call[0] == "GET" and call[1].endswith("/facade/api/v1/operations/transgran-op-1") for call in service.session.calls)
        )

    def test_resolve_transgran_dates_uses_marking_end_plus_one_minute(self) -> None:
        service = TeksherService(
            browser=FakeBrowser(),
            session=FakeSession(),
            sleep=lambda _: None,
        )
        config = AppConfig(teksher_api_token=_future_token())

        document_date, shipment_date = service.resolve_transgran_dates(
            marking_operation_id="marking-op-1",
            requested_document_date=datetime.fromisoformat("2026-04-24T19:31:39"),
            requested_shipment_date=datetime.fromisoformat("2026-04-24T19:31:41"),
            config=config,
        )

        self.assertEqual(datetime.fromisoformat("2026-04-24T19:41:00"), document_date)
        self.assertEqual(datetime.fromisoformat("2026-04-24T19:41:00"), shipment_date)

    def test_run_full_flow_resumes_from_saved_state_without_creating_new_order(self) -> None:
        session = FakeSessionResume()
        service = TeksherService(
            browser=FakeBrowser(),
            session=session,
            sleep=lambda _: None,
        )
        config = AppConfig(
            teksher_api_token=_future_token(),
            teksher_transgran_recipient_name='РћРћРћ "Р Р’Р‘"',
            teksher_transgran_recipient_inn="9714053621",
            teksher_transgran_recipient_kpp="507401001",
            step_timeout_seconds=30,
        )

        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "single.csv"
            with self.assertRaises(Exception):
                run_full_flow(
                    service=service,
                    gtins=["GTIN1"],
                    quantities=[2],
                    output_path=output,
                    document_number="123",
                    document_date=datetime.fromisoformat("2026-04-24T19:31:39"),
                    shipment_date=datetime.fromisoformat("2026-04-24T19:31:41"),
                    config=config,
                )

            first_order_creates = [
                call for call in session.calls if call[0] == "POST" and call[1].endswith("/facade/order/api/v1/operations/multi")
            ]
            self.assertEqual(1, len(first_order_creates))

            result = run_full_flow(
                service=service,
                gtins=["GTIN1"],
                quantities=[2],
                output_path=output,
                document_number="123",
                document_date=datetime.fromisoformat("2026-04-24T19:31:39"),
                shipment_date=datetime.fromisoformat("2026-04-24T19:31:41"),
                config=config,
            )

            self.assertEqual(output, result)
            order_creates = [
                call for call in session.calls if call[0] == "POST" and call[1].endswith("/facade/order/api/v1/operations/multi")
            ]
            self.assertEqual(1, len(order_creates))

    def test_wait_for_order_ready_allows_accepted_with_end_at_when_ready_false(self) -> None:
        service = TeksherService(
            browser=FakeBrowser(),
            session=FakeSessionOrderReadyFalse(),
            sleep=lambda _: None,
        )
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        service._wait_for_order_ready("order-op-1", config)


if __name__ == "__main__":
    unittest.main()
