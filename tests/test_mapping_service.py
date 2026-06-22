from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from wb_marks_app.exceptions import MappingValidationError
from wb_marks_app.models import SupplyItem
from wb_marks_app.services.mapping import MappingService


class MappingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MappingService()

    def test_build_tasks_for_valid_mapping(self) -> None:
        with TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "mapping.csv"
            csv_path.write_text("barcode,gtin\n123,GTIN123\n", encoding="utf-8")

            items = [
                SupplyItem(
                    barcode="123",
                    name="T-Shirt",
                    quantity=2,
                    draft_supply_id="SUP-1",
                )
            ]
            tasks = self.service.build_tasks(items, csv_path)

            self.assertEqual(2, len(tasks))
            self.assertEqual("GTIN123", tasks[0].gtin)
            self.assertEqual("123", tasks[1].barcode)

    def test_duplicate_barcode_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "mapping.csv"
            csv_path.write_text("barcode,gtin\n123,GTIN123\n123,GTIN999\n", encoding="utf-8")

            with self.assertRaises(MappingValidationError):
                self.service.load_mapping(csv_path)

    def test_empty_gtin_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "mapping.csv"
            csv_path.write_text("barcode,gtin\n123,\n", encoding="utf-8")

            with self.assertRaises(MappingValidationError):
                self.service.load_mapping(csv_path)

    def test_missing_mapping_for_supply_item_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "mapping.csv"
            csv_path.write_text("barcode,gtin\n999,GTIN999\n", encoding="utf-8")

            items = [
                SupplyItem(
                    barcode="123",
                    name="T-Shirt",
                    quantity=1,
                    draft_supply_id="SUP-1",
                )
            ]

            with self.assertRaises(MappingValidationError):
                self.service.build_tasks(items, csv_path)


if __name__ == "__main__":
    unittest.main()

