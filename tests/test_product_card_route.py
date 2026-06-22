import json
import unittest
from base64 import b64encode

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner


class ProductCardRouteTests(unittest.TestCase):
    def test_product_card_page_uses_wb_article_from_url(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.models import AppConfig

        old_create_all = webapp.create_all
        old_load_config = webapp.load_config
        webapp.create_all = lambda: None
        webapp.load_config = lambda: AppConfig(secret_key="test-secret")
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


def _session_cookie(payload: dict[str, str], secret_key: str) -> str:
    data = b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(secret_key).sign(data).decode("utf-8")


if __name__ == "__main__":
    unittest.main()
