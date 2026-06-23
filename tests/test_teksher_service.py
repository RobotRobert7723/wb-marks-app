from pathlib import Path
from tempfile import TemporaryDirectory
import base64
import json
import time
from datetime import datetime
import unittest
from urllib.parse import parse_qs, urlparse

from wb_marks_app.models import AppConfig, MarkingTask
from wb_marks_app.services.product_cards import ProductCardMappingRow, ProductCardTemplate, WbProductSummary
from wb_marks_app.services.teksher import (
    TEKSHER_CLOTHING_REGULATION,
    TeksherService,
)
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
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text or ("" if json_data is None else str(json_data))
        self.headers = headers or {"content-type": "application/json"}
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
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


class FakeSessionProducts(FakeSession):
    def __init__(
        self,
        existing: bool = False,
        existing_gtins: set[str] | None = None,
        empty_create_response: bool = False,
        no_content_lookup: bool = False,
    ) -> None:
        super().__init__()
        self.existing = existing
        self.existing_gtins = existing_gtins or set()
        self.empty_create_response = empty_create_response
        self.no_content_lookup = no_content_lookup
        self.created_payloads: list[dict] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None))
        if "/facade/api/v1/products?gtin=" in url:
            query_gtin = parse_qs(urlparse(url).query).get("gtin", [""])[0]
            if self.no_content_lookup:
                return FakeResponse(204, text="", json_error=True)
            if self.existing or query_gtin in self.existing_gtins:
                return FakeResponse(
                    200,
                    json_data={
                        "data": [
                            {
                                "id": "product-1",
                                "gtin": query_gtin or "04709055620626",
                                "status": "DRAFT",
                            }
                        ]
                    },
                )
            return FakeResponse(200, json_data={"data": []})
        if url.endswith("/facade/api/v1/products/product-1"):
            return FakeResponse(
                200,
                json_data={
                    "data": {
                        "id": "product-1",
                        "gtin": "04709055620626",
                        "fullName": "Костюм спортивный",
                        "trademark": "ErLine",
                        "tnved": {"id": 999, "code": "6112120000"},
                        "manufacturedCountry": {"id": 242, "name": "КЫРГЫЗСТАН"},
                        "attributes": [
                            {"attributeTypeCode": "35", "name": "Размер одежды / изделия", "value": "38 МЕЖДУНАРОДНЫЙ"},
                            {"attributeTypeCode": "12", "name": "Вид товара", "value": "КОСТЮМ СПОРТИВНЫЙ"},
                            {"attributeTypeCode": "36", "name": "Цвет", "value": "БЕЛЫЙ"},
                            {"attributeTypeCode": "2483", "name": "Состав", "value": "полиэстер 100%"},
                            {"attributeTypeCode": "14013", "name": "Целевой пол", "value": "МУЖСКОЙ"},
                            {"attributeTypeCode": "13914", "name": "Модель / артикул производителя", "value": "cv_nk_white_smr"},
                        ],
                    }
                },
            )
        if "/facade/api/v1/participants/manufacturer_info" in url:
            return FakeResponse(
                200,
                json_data={
                    "fullName": "ОсОО ЭрЛайн",
                    "inn": "12345678901234",
                },
            )
        if "/facade/api/v1/tnveds" in url:
            return FakeResponse(200, json_data={"data": [{"id": 999, "code": "6112120000"}]})
        if url.endswith("/facade/api/v1/countries"):
            return FakeResponse(
                200,
                json_data=[
                    {"id": 199, "name": "РОССИЯ", "alpha2": "RU"},
                    {"id": 242, "name": "КЫРГЫЗСТАН", "alpha2": "KG"},
                ],
            )
        if "/facade/api/v1/products/attribute_templates" in url:
            return FakeResponse(
                200,
                json_data=[
                    {
                        "attributeType": {
                            "code": "12",
                            "name": "Вид товара",
                            "values": ["КОСТЮМ СПОРТИВНЫЙ"],
                            "unitCodes": [],
                        }
                    },
                    {
                        "attributeType": {
                            "code": "35",
                            "name": "Размер одежды / изделия",
                            "values": [],
                            "unitCodes": ["МЕЖДУНАРОДНЫЙ"],
                        }
                    },
                    {
                        "attributeType": {
                            "code": "36",
                            "name": "Цвет",
                            "values": ["БЕЛЫЙ"],
                            "unitCodes": [],
                        }
                    },
                    {
                        "attributeType": {
                            "code": "14013",
                            "name": "Целевой пол",
                            "values": ["МУЖСКОЙ", "ЖЕНСКИЙ", "УНИВЕРСАЛЬНЫЙ (УНИСЕКС)"],
                            "unitCodes": [],
                        }
                    },
                    {
                        "attributeType": {
                            "code": "13836",
                            "name": "Номер регламента/стандарта",
                            "values": [TEKSHER_CLOTHING_REGULATION],
                            "unitCodes": [],
                        }
                    },
                ],
            )
        return super().get(url, headers=headers, timeout=timeout)

    def post(self, url, headers=None, json=None, files=None, timeout=None):
        self.calls.append(("POST", url, json if json is not None else files))
        if url.endswith("/facade/api/v1/products/create"):
            self.created_payloads.append(json or {})
            if self.empty_create_response:
                return FakeResponse(201, text="", json_error=True)
            return FakeResponse(201, json_data={"data": {"id": "draft-1", "status": "DRAFT"}})
        return super().post(url, headers=headers, json=json, files=files, timeout=timeout)


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


def _product_card() -> ProductCardTemplate:
    summary = WbProductSummary(
        name="Спортивный костюм",
        seller_category="Костюмы спортивные",
        wb_article="847012873",
        tnved="6112120000",
        country="Кыргызстан",
        seller_article="cv_nk_white_smr",
        color="белый",
        composition="полиэстер 100%",
        gender="мальчики",
        brand="ErLine",
    )
    return ProductCardTemplate(
        wb_article="847012873",
        image_url="",
        api_status="",
        wb_summary=summary,
        rows=[
            ProductCardMappingRow(
                barcode="2049271462689",
                wb_size="38",
                ru_size="134",
                teksher_size="",
                product_type="",
                gtin="",
                tnved="",
                country="",
                vendor_article="",
                color="",
                composition="",
                target_gender="",
                trademark="",
                ready_to_mark=0,
                print_count=0,
                order_count=0,
            )
        ],
    )


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

    def test_ensure_product_drafts_for_mapping_skips_existing_gtin(self) -> None:
        session = FakeSessionProducts(existing=True)
        service = TeksherService(browser=FakeBrowser(), session=session, sleep=lambda _: None)
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        draft_ids = service.ensure_product_drafts_for_mapping(
            _product_card(),
            [{"gtin": "04709055620626", "wb_size": "38"}],
            config,
        )

        self.assertEqual([], draft_ids)
        self.assertEqual([], session.created_payloads)

    def test_ensure_product_drafts_for_mapping_creates_only_missing_gtins(self) -> None:
        session = FakeSessionProducts(existing_gtins={"04709055620626"})
        service = TeksherService(browser=FakeBrowser(), session=session, sleep=lambda _: None)
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        draft_ids = service.ensure_product_drafts_for_mapping(
            _product_card(),
            [
                {
                    "wb_size": "38",
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "04709055620626",
                    "tnved": "6112120000",
                    "country": "Киргизия",
                    "color": "БЕЛЫЙ",
                    "composition": "полиэстер 100%",
                    "trademark": "ErLine",
                },
                {
                    "wb_size": "40",
                    "teksher_size": "40 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "04709055620633",
                    "tnved": "6112120000",
                    "country": "Кыргызстан",
                    "color": "БЕЛЫЙ",
                    "composition": "полиэстер 100%",
                    "trademark": "ErLine",
                },
            ],
            config,
        )

        self.assertEqual(["draft-1"], draft_ids)
        self.assertEqual(1, len(session.created_payloads))
        self.assertEqual("04709055620633", session.created_payloads[0]["gtin"])

    def test_product_draft_preview_reports_existing_and_missing_gtins(self) -> None:
        session = FakeSessionProducts(existing_gtins={"04709055620664"})
        service = TeksherService(browser=FakeBrowser(), session=session, sleep=lambda _: None)
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        preview = service.product_draft_preview_for_mapping(
            _product_card(),
            [
                {
                    "wb_size": "38",
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "04709055620664",
                    "tnved": "6112120000",
                    "country": "Киргизия",
                    "color": "БЕЛЫЙ",
                    "composition": "полиэстер 100%",
                    "trademark": "ErLine",
                },
                {
                    "wb_size": "40",
                    "teksher_size": "40 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "04709055620671",
                    "tnved": "6112120000",
                    "country": "Киргизия",
                    "color": "БЕЛЫЙ",
                    "composition": "полиэстер 100%",
                    "trademark": "ErLine",
                },
            ],
            config,
        )

        self.assertEqual(["04709055620664"], preview["existing_gtins"])
        self.assertEqual(["04709055620671"], preview["create_gtins"])
        self.assertEqual("КЫРГЫЗСТАН", preview["draft_fields"]["country"])
        self.assertEqual("ОсОО ЭрЛайн", preview["draft_fields"]["manufacturer_full_name"])
        self.assertEqual("12345678901234", preview["draft_fields"]["manufacturer_inn"])
        self.assertIn({"value": "КОСТЮМ СПОРТИВНЫЙ", "label": ""}, preview["dictionaries"]["product_type"])
        self.assertIn({"value": "МЕЖДУНАРОДНЫЙ", "label": ""}, preview["dictionaries"]["size_unit"])

    def test_ensure_product_drafts_for_mapping_creates_draft_without_approve(self) -> None:
        session = FakeSessionProducts(existing=False)
        service = TeksherService(browser=FakeBrowser(), session=session, sleep=lambda _: None)
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        draft_ids = service.ensure_product_drafts_for_mapping(
            _product_card(),
            [
                {
                    "wb_size": "38",
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "4709055620626",
                    "tnved": "6112120000",
                    "country": "Кыргызстан",
                    "color": "БЕЛЫЙ",
                    "composition": "полиэстер 100%",
                    "trademark": "ErLine",
                }
            ],
            config,
        )

        self.assertEqual(["draft-1"], draft_ids)
        self.assertEqual(1, len(session.created_payloads))
        payload = session.created_payloads[0]
        self.assertEqual("04709055620626", payload["gtin"])
        self.assertEqual("ОсОО ЭрЛайн", payload["manufacturerFullName"])
        self.assertEqual("12345678901234", payload["manufacturerInn"])
        self.assertEqual("470905562", payload["gcp"])
        self.assertEqual("4709055620008", payload["gln"])
        self.assertEqual(242, payload["manufacturedCountryId"])
        self.assertEqual(999, payload["tnved"])
        self.assertEqual("ErLine", payload["trademark"])
        self.assertNotIn("isImport", payload)
        attributes = {attribute["attributeTypeCode"]: attribute for attribute in payload["attributes"]}
        self.assertEqual("КОСТЮМ СПОРТИВНЫЙ", attributes["12"]["value"])
        self.assertEqual("38", attributes["35"]["value"])
        self.assertEqual("МЕЖДУНАРОДНЫЙ", attributes["35"]["unitCode"])
        self.assertEqual("БЕЛЫЙ", attributes["36"]["value"])
        self.assertEqual("полиэстер 100%", attributes["2483"]["value"])
        self.assertEqual("МУЖСКОЙ", attributes["14013"]["value"])
        self.assertEqual("cv_nk_white_smr", attributes["13914"]["value"])
        self.assertEqual("Артикул", attributes["13914"]["unitCode"])
        self.assertEqual(TEKSHER_CLOTHING_REGULATION, attributes["13836"]["value"])
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/approve") for call in session.calls))

    def test_ensure_product_drafts_allows_empty_success_create_response(self) -> None:
        session = FakeSessionProducts(existing=False, empty_create_response=True)
        service = TeksherService(browser=FakeBrowser(), session=session, sleep=lambda _: None)
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        draft_ids = service.ensure_product_drafts_for_mapping(
            _product_card(),
            [
                {
                    "wb_size": "38",
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "04709055620626",
                    "tnved": "6112120000",
                    "country": "Кыргызстан",
                    "color": "БЕЛЫЙ",
                    "composition": "полиэстер 100%",
                    "trademark": "ErLine",
                }
            ],
            config,
        )

        self.assertEqual([""], draft_ids)
        self.assertEqual(1, len(session.created_payloads))

    def test_ensure_product_drafts_treats_no_content_lookup_as_missing_product(self) -> None:
        session = FakeSessionProducts(no_content_lookup=True)
        service = TeksherService(browser=FakeBrowser(), session=session, sleep=lambda _: None)
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        draft_ids = service.ensure_product_drafts_for_mapping(
            _product_card(),
            [
                {
                    "wb_size": "38",
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "04709055620626",
                    "tnved": "6112120000",
                    "country": "Кыргызстан",
                    "color": "БЕЛЫЙ",
                    "composition": "полиэстер 100%",
                    "trademark": "ErLine",
                }
            ],
            config,
        )

        self.assertEqual(["draft-1"], draft_ids)
        self.assertEqual(1, len(session.created_payloads))

    def test_product_mapping_rows_by_gtins_treats_no_content_lookup_as_missing_product(self) -> None:
        session = FakeSessionProducts(no_content_lookup=True)
        service = TeksherService(browser=FakeBrowser(), session=session, sleep=lambda _: None)
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        rows = service.product_mapping_rows_by_gtins(["04709055620626"], config)

        self.assertEqual({}, rows)

    def test_product_mapping_rows_by_gtins_reads_teksher_product_details(self) -> None:
        session = FakeSessionProducts(existing=True)
        service = TeksherService(browser=FakeBrowser(), session=session, sleep=lambda _: None)
        config = AppConfig(teksher_api_token=_future_token(), step_timeout_seconds=30)

        rows = service.product_mapping_rows_by_gtins(["04709055620626"], config)

        row = rows["04709055620626"]
        self.assertEqual("38 МЕЖДУНАРОДНЫЙ", row["teksher_size"])
        self.assertEqual("КОСТЮМ СПОРТИВНЫЙ", row["product_type"])
        self.assertEqual("6112120000", row["tnved"])
        self.assertEqual("КЫРГЫЗСТАН", row["country"])
        self.assertEqual("cv_nk_white_smr", row["vendor_article"])
        self.assertEqual("БЕЛЫЙ", row["color"])
        self.assertEqual("полиэстер 100%", row["composition"])
        self.assertEqual("МУЖСКОЙ", row["target_gender"])
        self.assertEqual("ErLine", row["trademark"])
        self.assertTrue(any(call[0] == "GET" and call[1].endswith("/facade/api/v1/products/product-1") for call in session.calls))


if __name__ == "__main__":
    unittest.main()
