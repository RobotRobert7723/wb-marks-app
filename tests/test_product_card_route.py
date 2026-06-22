import json
import unittest
from dataclasses import replace
from base64 import b64encode
from io import BytesIO

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from openpyxl import Workbook


class ProductCardRouteTests(unittest.TestCase):
    def test_product_card_page_uses_wb_article_from_url(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.models import AppConfig
        from wb_marks_app.services.product_cards import ProductCardTemplate, ProductCardMappingRow, WbProductSummary

        old_create_all = webapp.create_all
        old_load_config = webapp.load_config
        old_session_scope = webapp.session_scope
        old_get_or_create_settings = webapp.get_or_create_settings
        old_settings_to_app_config = webapp.settings_to_app_config
        old_product_card_service = webapp.product_card_service
        old_teksher_mapping_service = webapp.teksher_mapping_service
        old_teksher_product_service = webapp.teksher_product_service
        webapp.create_all = lambda: None
        webapp.load_config = lambda: AppConfig(secret_key="test-secret")
        webapp.session_scope = lambda: FakeSessionScope()
        webapp.get_or_create_settings = lambda _session, _user_id: object()
        webapp.settings_to_app_config = lambda _settings: AppConfig(wb_api_token="token")
        webapp.teksher_mapping_service = FakeTeksherMappingService()
        webapp.teksher_product_service = FakeTeksherProductService()
        webapp.product_card_service = FakeProductCardService(
            ProductCardTemplate(
                wb_article="847012873",
                image_url="",
                api_status="Данные WB загружены из Content API",
                wb_summary=WbProductSummary(
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
                ),
                rows=[
                    ProductCardMappingRow(
                        barcode="2049271462689",
                        wb_size="38",
                        ru_size="134",
                        teksher_size="38 МЕЖДУНАРОДНЫЙ",
                        product_type="КОСТЮМЫ СПОРТИВНЫЕ",
                        gtin="",
                        tnved="6112120000",
                        country="Кыргызстан",
                        vendor_article="cv_nk_white_smr",
                        color="БЕЛЫЙ",
                        composition="полиэстер 100%",
                        target_gender="МАЛЬЧИКИ",
                        trademark="ErLine",
                        ready_to_mark=0,
                        print_count=0,
                        order_count=0,
                    )
                ],
            )
        )
        try:
            app = webapp.create_app()
            client = TestClient(app)
            client.cookies.set("wb_marks_session", _session_cookie({"user_id": "user-1", "login": "tester"}, "test-secret"))

            page = client.get("/product-cards/847012873")
            self.assertEqual(200, page.status_code)
            self.assertIn("Карточка WB 847012873", page.text)
            self.assertIn('data-wb-article="847012873"', page.text)
            self.assertIn("/product-cards/847012873", page.text)
            self.assertIn("Готовы к нанесению", page.text)
            self.assertIn("Заказать в Текшер", page.text)
            self.assertIn('data-gtin-field="gtin"', page.text)
            self.assertIn('data-gtin-field="tnved"', page.text)
            self.assertIn('data-gtin-field="target_gender"', page.text)
            self.assertIn('data-wb-product-type="Костюмы спортивные"', page.text)
            self.assertIn('data-wb-tnved="6112120000"', page.text)
            self.assertIn('data-wb-country="Кыргызстан"', page.text)
            self.assertIn('data-wb-color="белый"', page.text)
            self.assertIn('data-wb-composition="полиэстер 100%"', page.text)
            self.assertIn('data-wb-brand="ErLine"', page.text)
            self.assertIn('data-has-mapping="false"', page.text)
            self.assertIn('id="mapping-save-button" class="secondary" disabled', page.text)
            self.assertIn("Для этой карточки уже есть GTIN. Действительно хотите обновить", page.text)
            self.assertIn("В таблице есть красные поля. Сохранить?", page.text)
            self.assertIn("gtin-loaded", page.text)
            self.assertIn("gtin-mismatch", page.text)
            self.assertIn("function setLoadedCell(", page.text)
            self.assertIn("function clearGtinTable()", page.text)
            self.assertIn("function targetGenderFromWb(", page.text)

            client.close()
        finally:
            webapp.create_all = old_create_all
            webapp.load_config = old_load_config
            webapp.session_scope = old_session_scope
            webapp.get_or_create_settings = old_get_or_create_settings
            webapp.settings_to_app_config = old_settings_to_app_config
            webapp.product_card_service = old_product_card_service
            webapp.teksher_mapping_service = old_teksher_mapping_service
            webapp.teksher_product_service = old_teksher_product_service

    def test_product_card_page_uses_teksher_data_for_saved_gtin(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.models import AppConfig
        from wb_marks_app.services.product_cards import ProductCardTemplate, ProductCardMappingRow, WbProductSummary

        old_create_all = webapp.create_all
        old_load_config = webapp.load_config
        old_session_scope = webapp.session_scope
        old_get_or_create_settings = webapp.get_or_create_settings
        old_settings_to_app_config = webapp.settings_to_app_config
        old_product_card_service = webapp.product_card_service
        old_teksher_mapping_service = webapp.teksher_mapping_service
        old_teksher_product_service = webapp.teksher_product_service
        webapp.create_all = lambda: None
        webapp.load_config = lambda: AppConfig(secret_key="test-secret")
        webapp.session_scope = lambda: FakeSessionScope()
        webapp.get_or_create_settings = lambda _session, _user_id: object()
        webapp.settings_to_app_config = lambda _settings: AppConfig(wb_api_token="token")
        webapp.teksher_mapping_service = FakeTeksherMappingService(
            version=3,
            latest_rows=[
                {
                    "wb_barcode": "2049271462689",
                    "wb_size": "38",
                    "wb_ru_size": "134",
                    "teksher_size": "STALE SIZE",
                    "product_type": "STALE PRODUCT",
                    "gtin": "04709055620626",
                    "tnved": "STALE TNVED",
                    "country": "STALE COUNTRY",
                    "vendor_article": "STALE ARTICLE",
                    "color": "STALE COLOR",
                    "composition": "STALE COMPOSITION",
                    "target_gender": "STALE GENDER",
                    "trademark": "STALE BRAND",
                }
            ],
        )
        webapp.teksher_product_service = FakeTeksherProductService(
            mapping_rows_by_gtin={
                "04709055620626": {
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "ДАННЫЕ ИЗ ТЕКШЕР",
                    "gtin": "04709055620626",
                    "tnved": "6112120000",
                    "country": "Кыргызстан",
                    "vendor_article": "cv_nk_white_smr",
                    "color": "БЕЛЫЙ",
                    "composition": "полиэстер 100%",
                    "target_gender": "МУЖСКОЙ",
                    "trademark": "ErLine",
                }
            }
        )
        webapp.product_card_service = FakeProductCardService(
            ProductCardTemplate(
                wb_article="847012873",
                image_url="",
                api_status="",
                wb_summary=WbProductSummary(
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
                ),
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
        )
        try:
            app = webapp.create_app()
            client = TestClient(app)
            client.cookies.set("wb_marks_session", _session_cookie({"user_id": "user-1", "login": "tester"}, "test-secret"))

            page = client.get("/product-cards/847012873")

            self.assertEqual(200, page.status_code)
            self.assertIn("ДАННЫЕ ИЗ ТЕКШЕР", page.text)
            self.assertIn("04709055620626", page.text)
            self.assertNotIn("STALE PRODUCT", page.text)
            self.assertEqual(["04709055620626"], webapp.teksher_product_service.gtins)
            client.close()
        finally:
            webapp.create_all = old_create_all
            webapp.load_config = old_load_config
            webapp.session_scope = old_session_scope
            webapp.get_or_create_settings = old_get_or_create_settings
            webapp.settings_to_app_config = old_settings_to_app_config
            webapp.product_card_service = old_product_card_service
            webapp.teksher_mapping_service = old_teksher_mapping_service
            webapp.teksher_product_service = old_teksher_product_service

    def test_gtin_upload_returns_rows_for_matching_vendor_article(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.models import AppConfig
        from wb_marks_app.services.product_cards import ProductCardTemplate, WbProductSummary

        old_create_all = webapp.create_all
        old_load_config = webapp.load_config
        old_session_scope = webapp.session_scope
        old_get_or_create_settings = webapp.get_or_create_settings
        old_settings_to_app_config = webapp.settings_to_app_config
        old_product_card_service = webapp.product_card_service
        webapp.create_all = lambda: None
        webapp.load_config = lambda: AppConfig(secret_key="test-secret")
        webapp.session_scope = lambda: FakeSessionScope()
        webapp.get_or_create_settings = lambda _session, _user_id: object()
        webapp.settings_to_app_config = lambda _settings: AppConfig(wb_api_token="token")
        webapp.product_card_service = FakeProductCardService(
            ProductCardTemplate(
                wb_article="847012873",
                image_url="",
                api_status="",
                wb_summary=WbProductSummary(
                    name="",
                    seller_category="",
                    wb_article="847012873",
                    tnved="",
                    country="",
                    seller_article="cv_nk_white_smr",
                    color="",
                    composition="",
                    gender="",
                    brand="",
                ),
                rows=[],
            )
        )
        try:
            app = webapp.create_app()
            client = TestClient(app)
            client.cookies.set("wb_marks_session", _session_cookie({"user_id": "user-1", "login": "tester"}, "test-secret"))

            response = client.post(
                "/api/product-cards/847012873/gtin-upload",
                files={
                    "file": (
                        "gtin.xlsx",
                        _build_gtin_upload_workbook("Арт.cv_nk_white_smr, цвет: белый, р. 38"),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )

            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual("04709055620626", payload["rows"][0]["gtin"])
            self.assertEqual("cv_nk_white_smr", payload["rows"][0]["vendor_article"])
            self.assertEqual("38", payload["rows"][0]["size"])
            client.close()
        finally:
            webapp.create_all = old_create_all
            webapp.load_config = old_load_config
            webapp.session_scope = old_session_scope
            webapp.get_or_create_settings = old_get_or_create_settings
            webapp.settings_to_app_config = old_settings_to_app_config
            webapp.product_card_service = old_product_card_service

    def test_gtin_upload_reports_missing_vendor_article(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.models import AppConfig
        from wb_marks_app.services.product_cards import ProductCardTemplate, WbProductSummary

        old_create_all = webapp.create_all
        old_load_config = webapp.load_config
        old_session_scope = webapp.session_scope
        old_get_or_create_settings = webapp.get_or_create_settings
        old_settings_to_app_config = webapp.settings_to_app_config
        old_product_card_service = webapp.product_card_service
        webapp.create_all = lambda: None
        webapp.load_config = lambda: AppConfig(secret_key="test-secret")
        webapp.session_scope = lambda: FakeSessionScope()
        webapp.get_or_create_settings = lambda _session, _user_id: object()
        webapp.settings_to_app_config = lambda _settings: AppConfig(wb_api_token="token")
        webapp.product_card_service = FakeProductCardService(
            ProductCardTemplate(
                wb_article="847012873",
                image_url="",
                api_status="",
                wb_summary=WbProductSummary(
                    name="",
                    seller_category="",
                    wb_article="847012873",
                    tnved="",
                    country="",
                    seller_article="cv_nk_white_smr",
                    color="",
                    composition="",
                    gender="",
                    brand="",
                ),
                rows=[],
            )
        )
        try:
            app = webapp.create_app()
            client = TestClient(app)
            client.cookies.set("wb_marks_session", _session_cookie({"user_id": "user-1", "login": "tester"}, "test-secret"))

            response = client.post(
                "/api/product-cards/847012873/gtin-upload",
                files={
                    "file": (
                        "gtin.xlsx",
                        _build_gtin_upload_workbook("Арт.other_article, цвет: белый, р. 38"),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )

            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertFalse(payload["ok"])
            self.assertEqual("Артикул продавца в файле не найден", payload["message"])
            self.assertEqual([], payload["rows"])
            client.close()
        finally:
            webapp.create_all = old_create_all
            webapp.load_config = old_load_config
            webapp.session_scope = old_session_scope
            webapp.get_or_create_settings = old_get_or_create_settings
            webapp.settings_to_app_config = old_settings_to_app_config
            webapp.product_card_service = old_product_card_service

    def test_mapping_save_endpoint_creates_version(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.models import AppConfig
        from wb_marks_app.services.product_cards import ProductCardTemplate, ProductCardMappingRow, WbProductSummary

        old_create_all = webapp.create_all
        old_load_config = webapp.load_config
        old_session_scope = webapp.session_scope
        old_get_or_create_settings = webapp.get_or_create_settings
        old_settings_to_app_config = webapp.settings_to_app_config
        old_product_card_service = webapp.product_card_service
        old_teksher_mapping_service = webapp.teksher_mapping_service
        old_teksher_product_service = webapp.teksher_product_service
        fake_mapping_service = FakeTeksherMappingService(version=2)
        fake_teksher_product_service = FakeTeksherProductService(draft_ids=["draft-1"])
        webapp.create_all = lambda: None
        webapp.load_config = lambda: AppConfig(secret_key="test-secret")
        webapp.session_scope = lambda: FakeSessionScope()
        webapp.get_or_create_settings = lambda _session, _user_id: object()
        webapp.settings_to_app_config = lambda _settings: AppConfig(wb_api_token="token")
        webapp.teksher_mapping_service = fake_mapping_service
        webapp.teksher_product_service = fake_teksher_product_service
        webapp.product_card_service = FakeProductCardService(
            ProductCardTemplate(
                wb_article="847012873",
                image_url="",
                api_status="",
                wb_summary=WbProductSummary(
                    name="Sport suit",
                    seller_category="Sport suits",
                    wb_article="847012873",
                    tnved="6112120000",
                    country="KG",
                    seller_article="cv_nk_white_smr",
                    color="white",
                    composition="polyester 100%",
                    gender="boys",
                    brand="ErLine",
                ),
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
        )
        try:
            app = webapp.create_app()
            client = TestClient(app)
            client.cookies.set("wb_marks_session", _session_cookie({"user_id": "user-1", "login": "tester"}, "test-secret"))

            response = client.post(
                "/api/product-cards/847012873/mapping",
                json={
                    "source": "gtin_excel",
                    "rows": [
                        {
                            "wb_barcode": "2049271462689",
                            "wb_size": "38",
                            "wb_ru_size": "134",
                            "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                            "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                            "gtin": "04709055620626",
                            "vendor_article": "cv_nk_white_smr",
                            "color": "БЕЛЫЙ",
                            "trademark": "ErLine",
                        }
                    ],
                },
            )

            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(2, payload["version"])
            self.assertEqual(1, payload["rows_saved"])
            self.assertEqual("user-1", fake_mapping_service.saved[0]["user_id"])
            self.assertEqual("gtin_excel", fake_mapping_service.saved[0]["source"])
            self.assertEqual("04709055620626", fake_mapping_service.saved[0]["rows"][0]["gtin"])
            self.assertEqual(["draft-1"], payload["teksher_draft_ids"])
            self.assertEqual("04709055620626", fake_teksher_product_service.calls[0]["rows"][0]["gtin"])
            client.close()
        finally:
            webapp.create_all = old_create_all
            webapp.load_config = old_load_config
            webapp.session_scope = old_session_scope
            webapp.get_or_create_settings = old_get_or_create_settings
            webapp.settings_to_app_config = old_settings_to_app_config
            webapp.product_card_service = old_product_card_service
            webapp.teksher_mapping_service = old_teksher_mapping_service
            webapp.teksher_product_service = old_teksher_product_service

    def test_mapping_save_endpoint_does_not_save_when_teksher_gtin_exists(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.models import AppConfig
        from wb_marks_app.services.product_cards import ProductCardTemplate, ProductCardMappingRow, WbProductSummary
        from wb_marks_app.services.teksher import TEKSHER_EXISTING_PRODUCT_MESSAGE

        old_create_all = webapp.create_all
        old_load_config = webapp.load_config
        old_session_scope = webapp.session_scope
        old_get_or_create_settings = webapp.get_or_create_settings
        old_settings_to_app_config = webapp.settings_to_app_config
        old_product_card_service = webapp.product_card_service
        old_teksher_mapping_service = webapp.teksher_mapping_service
        old_teksher_product_service = webapp.teksher_product_service
        fake_mapping_service = FakeTeksherMappingService(version=2)
        webapp.create_all = lambda: None
        webapp.load_config = lambda: AppConfig(secret_key="test-secret")
        webapp.session_scope = lambda: FakeSessionScope()
        webapp.get_or_create_settings = lambda _session, _user_id: object()
        webapp.settings_to_app_config = lambda _settings: AppConfig(wb_api_token="token")
        webapp.teksher_mapping_service = fake_mapping_service
        webapp.teksher_product_service = FakeTeksherProductService(existing=True)
        webapp.product_card_service = FakeProductCardService(
            ProductCardTemplate(
                wb_article="847012873",
                image_url="",
                api_status="",
                wb_summary=WbProductSummary(
                    name="Sport suit",
                    seller_category="Sport suits",
                    wb_article="847012873",
                    tnved="6112120000",
                    country="KG",
                    seller_article="cv_nk_white_smr",
                    color="white",
                    composition="polyester 100%",
                    gender="boys",
                    brand="ErLine",
                ),
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
        )
        try:
            app = webapp.create_app()
            client = TestClient(app)
            client.cookies.set("wb_marks_session", _session_cookie({"user_id": "user-1", "login": "tester"}, "test-secret"))

            response = client.post(
                "/api/product-cards/847012873/mapping",
                json={
                    "source": "gtin_excel",
                    "rows": [
                        {
                            "wb_barcode": "2049271462689",
                            "wb_size": "38",
                            "wb_ru_size": "134",
                            "gtin": "04709055620626",
                        }
                    ],
                },
            )

            self.assertEqual(409, response.status_code)
            payload = response.json()
            self.assertFalse(payload["ok"])
            self.assertEqual(TEKSHER_EXISTING_PRODUCT_MESSAGE, payload["message"])
            self.assertEqual([], fake_mapping_service.saved)
            client.close()
        finally:
            webapp.create_all = old_create_all
            webapp.load_config = old_load_config
            webapp.session_scope = old_session_scope
            webapp.get_or_create_settings = old_get_or_create_settings
            webapp.settings_to_app_config = old_settings_to_app_config
            webapp.product_card_service = old_product_card_service
            webapp.teksher_mapping_service = old_teksher_mapping_service
            webapp.teksher_product_service = old_teksher_product_service


class FakeSessionScope:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeProductCardService:
    def __init__(self, product_card) -> None:
        self.product_card = product_card
        self.calls = []

    def build_template(self, wb_article, config):
        self.calls.append((wb_article, config))
        return self.product_card


class FakeTeksherMappingService:
    def __init__(self, version: int = 1, latest_rows=None) -> None:
        self.version = version
        self.latest_rows = latest_rows or []
        self.saved = []
        self.applied = []

    def apply_latest(self, session, user_id, product_card):
        return product_card

    def latest_payload(self, session, user_id, wb_article):
        if not self.latest_rows:
            return 0, []
        return self.version, self.latest_rows

    def apply_payload(self, product_card, saved_rows, version, teksher_rows_by_gtin=None):
        self.applied.append(
            {
                "product_card": product_card,
                "saved_rows": saved_rows,
                "version": version,
                "teksher_rows_by_gtin": teksher_rows_by_gtin,
            }
        )
        if not saved_rows:
            return product_card
        rows = []
        for row in product_card.rows:
            saved = next((item for item in saved_rows if item.get("wb_size") == row.wb_size), None)
            if saved is None:
                rows.append(row)
                continue
            source = (teksher_rows_by_gtin or {}).get(saved.get("gtin"), {})
            rows.append(
                replace(
                    row,
                    teksher_size=source.get("teksher_size", ""),
                    product_type=source.get("product_type", ""),
                    gtin=source.get("gtin", "") or saved.get("gtin", ""),
                    tnved=source.get("tnved", ""),
                    country=source.get("country", ""),
                    vendor_article=source.get("vendor_article", ""),
                    color=source.get("color", ""),
                    composition=source.get("composition", ""),
                    target_gender=source.get("target_gender", ""),
                    trademark=source.get("trademark", ""),
                )
            )
        return replace(product_card, rows=rows, has_teksher_mapping=True, mapping_version=version)

    def save_version(self, session, user_id, product_card, rows_payload, source="product_card"):
        self.saved.append(
            {
                "session": session,
                "user_id": user_id,
                "product_card": product_card,
                "rows": rows_payload,
                "source": source,
            }
        )
        return self.version, len(rows_payload)


class FakeTeksherProductService:
    def __init__(self, draft_ids=None, existing: bool = False, mapping_rows_by_gtin=None) -> None:
        self.draft_ids = draft_ids or []
        self.existing = existing
        self.mapping_rows_by_gtin = mapping_rows_by_gtin or {}
        self.calls = []
        self.gtins = []

    def product_mapping_rows_by_gtins(self, gtins, config):
        self.gtins = list(gtins)
        return self.mapping_rows_by_gtin

    def ensure_product_drafts_for_mapping(self, product_card, rows_payload, config):
        self.calls.append(
            {
                "product_card": product_card,
                "rows": rows_payload,
                "config": config,
            }
        )
        if self.existing:
            from wb_marks_app.services.teksher import ExistingTeksherProductError, TEKSHER_EXISTING_PRODUCT_MESSAGE

            raise ExistingTeksherProductError(TEKSHER_EXISTING_PRODUCT_MESSAGE)
        return self.draft_ids


def _build_gtin_upload_workbook(variety: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Перечень продукции"])
    sheet.append(["Наименование предприятия", "ОсОО"])
    sheet.append(["Описание"])
    sheet.append(
        [
            "ТИП",
            "GTIN",
            "БРЕНД",
            "СУБ-БРЕНД",
            "ЯЗЫК",
            "ФУНКЦИОНАЛЬНОЕ НАЗВАНИЕ",
            "РАЗНОВИДНОСТЬ",
        ]
    )
    sheet.append(["Единичная упаковка", "04709055620626", "ErLine", "", "Русский", "Костюмы спортивные", variety])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _session_cookie(payload: dict[str, str], secret_key: str) -> str:
    data = b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(secret_key).sign(data).decode("utf-8")


if __name__ == "__main__":
    unittest.main()
