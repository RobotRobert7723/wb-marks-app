import unittest

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


if __name__ == "__main__":
    unittest.main()
