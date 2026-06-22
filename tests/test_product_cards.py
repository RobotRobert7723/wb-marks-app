import unittest

from wb_marks_app.models import AppConfig
from wb_marks_app.services.product_cards import ProductCardTemplateService


class ProductCardTemplateServiceTests(unittest.TestCase):
    def test_template_contains_wb_article_and_mapping_rows(self) -> None:
        product_card = ProductCardTemplateService().build_template(" 847012873 ")

        self.assertEqual("847012873", product_card.wb_article)
        self.assertEqual("847012873", product_card.wb_summary.wb_article)
        self.assertEqual(4, len(product_card.rows))
        self.assertEqual("2049271462689", product_card.rows[0].barcode)
        self.assertEqual("38 МЕЖДУНАРОДНЫЙ", product_card.rows[0].teksher_size)
        self.assertEqual(23, product_card.rows[0].ready_to_mark)

    def test_template_can_load_wb_card_from_content_api(self) -> None:
        session = FakeSession(
            {
                "cards": [
                    {
                        "nmID": 847012873,
                        "title": "Костюм спортивный детский",
                        "subjectName": "Костюмы спортивные",
                        "vendorCode": "cv_nk_white_smr",
                        "brand": "ErLine",
                        "photos": [{"big": "https://images.wb.ru/product.jpg"}],
                        "characteristics": [
                            {"name": "ТНВЭД", "value": 6112120000},
                            {"name": "Страна производства", "value": ["Кыргызстан"]},
                            {"name": "Цвет", "value": ["белый"]},
                            {"name": "Состав", "value": ["полиэстер 100%"]},
                            {"name": "Пол", "value": ["мальчики"]},
                        ],
                        "sizes": [
                            {"techSize": "38", "wbSize": "134", "skus": ["2049271462689"]},
                            {"techSize": "40", "wbSize": "140", "skus": ["2049271462672"]},
                        ],
                    }
                ]
            }
        )
        config = AppConfig(
            wb_api_token="token",
            wb_content_api_base_url="https://content-api.example",
            step_timeout_seconds=15,
        )

        product_card = ProductCardTemplateService(session=session).build_template("847012873", config)

        self.assertEqual("Костюм спортивный детский", product_card.wb_summary.name)
        self.assertEqual("Костюмы спортивные", product_card.wb_summary.seller_category)
        self.assertEqual("847012873", product_card.wb_summary.wb_article)
        self.assertEqual("6112120000", product_card.wb_summary.tnved)
        self.assertEqual("Кыргызстан", product_card.wb_summary.country)
        self.assertEqual("cv_nk_white_smr", product_card.wb_summary.seller_article)
        self.assertEqual("белый", product_card.wb_summary.color)
        self.assertEqual("полиэстер 100%", product_card.wb_summary.composition)
        self.assertEqual("мальчики", product_card.wb_summary.gender)
        self.assertEqual("ErLine", product_card.wb_summary.brand)
        self.assertEqual("https://images.wb.ru/product.jpg", product_card.image_url)
        self.assertEqual(2, len(product_card.rows))
        self.assertEqual("2049271462689", product_card.rows[0].barcode)
        self.assertEqual("38", product_card.rows[0].wb_size)
        self.assertEqual("134", product_card.rows[0].ru_size)
        self.assertEqual("38 МЕЖДУНАРОДНЫЙ", product_card.rows[0].teksher_size)
        self.assertEqual(0, product_card.rows[0].ready_to_mark)
        self.assertEqual("https://content-api.example/content/v2/get/cards/list", session.calls[0]["url"])
        self.assertEqual("token", session.calls[0]["headers"]["Authorization"])
        self.assertEqual("847012873", session.calls[0]["json"]["settings"]["filter"]["textSearch"])

    def test_template_shows_status_when_wb_token_is_missing(self) -> None:
        product_card = ProductCardTemplateService(session=FakeSession({"cards": []})).build_template(
            "847012873",
            AppConfig(wb_api_token=""),
        )

        self.assertEqual("847012873", product_card.wb_summary.wb_article)
        self.assertEqual([], product_card.rows)
        self.assertIn("WB API token", product_card.api_status)


class FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payload)


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
