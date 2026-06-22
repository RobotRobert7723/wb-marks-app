import json
import unittest
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
        webapp.create_all = lambda: None
        webapp.load_config = lambda: AppConfig(secret_key="test-secret")
        webapp.session_scope = lambda: FakeSessionScope()
        webapp.get_or_create_settings = lambda _session, _user_id: object()
        webapp.settings_to_app_config = lambda _settings: AppConfig(wb_api_token="token")
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

            client.close()
        finally:
            webapp.create_all = old_create_all
            webapp.load_config = old_load_config
            webapp.session_scope = old_session_scope
            webapp.get_or_create_settings = old_get_or_create_settings
            webapp.settings_to_app_config = old_settings_to_app_config
            webapp.product_card_service = old_product_card_service

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
