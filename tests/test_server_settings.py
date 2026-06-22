import json
import unittest

from wb_marks_app.models import SupplyItem
from wb_marks_app.server_settings import MappingRuleSet


class MappingRuleSetTests(unittest.TestCase):
    def test_resolve_gtin_prefers_barcode_then_vendor_size_then_size(self) -> None:
        rules = MappingRuleSet(
            mode="mixed",
            payload={
                "barcode_to_gtin": {"111": "GTIN-BARCODE"},
                "vendor_size_to_gtin": {"art1:M": "GTIN-VENDOR-SIZE"},
                "size_to_gtin": {"M": "GTIN-SIZE"},
            },
        )
        by_barcode = SupplyItem(barcode="111", name="Item", quantity=1, supplier_article="art1", size="M")
        by_vendor_size = SupplyItem(barcode="222", name="Item", quantity=1, supplier_article="art1", size="M")
        by_size = SupplyItem(barcode="333", name="Item", quantity=1, supplier_article="art2", size="M")

        self.assertEqual("GTIN-BARCODE", rules.resolve_gtin(by_barcode))
        self.assertEqual("GTIN-VENDOR-SIZE", rules.resolve_gtin(by_vendor_size))
        self.assertEqual("GTIN-SIZE", rules.resolve_gtin(by_size))

    def test_missing_gtin_returns_empty_string(self) -> None:
        rules = MappingRuleSet(mode="size", payload=json.loads("{}"))
        item = SupplyItem(barcode="1", name="Item", quantity=1, supplier_article="art", size="S")
        self.assertEqual("", rules.resolve_gtin(item))
