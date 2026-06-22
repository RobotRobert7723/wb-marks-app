from io import BytesIO
import unittest

from openpyxl import Workbook

from wb_marks_app.services.gtin_excel import GtinExcelParser


class GtinExcelParserTests(unittest.TestCase):
    def test_parse_template_rows_and_match_vendor_article(self) -> None:
        data = _build_workbook(
            [
                ["04709055620626", "ErLine", "Костюмы спортивные", "Арт.cv_nk_white_smr, цвет: белый, р. 38"],
                ["04709055620633", "ErLine", "Костюмы спортивные", "Арт.cv_nk_white_smr, цвет: белый, р. 40"],
                ["04709055620664", "ErLine", "Костюмы спортивные", "Арт.cv_nk_blue_smr, цвет: голубой, р. 38"],
            ]
        )

        rows = GtinExcelParser().parse(data)
        matched = GtinExcelParser().find_by_vendor_article(rows, "cv_nk_white_smr")

        self.assertEqual(3, len(rows))
        self.assertEqual(2, len(matched))
        self.assertEqual("04709055620626", matched[0].gtin)
        self.assertEqual("ErLine", matched[0].brand)
        self.assertEqual("Костюмы спортивные", matched[0].functional_name)
        self.assertEqual("cv_nk_white_smr", matched[0].vendor_article)
        self.assertEqual("белый", matched[0].color)
        self.assertEqual("38", matched[0].size)


def _build_workbook(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Перечень продукции"])
    sheet.append(["Наименование предприятия", "ОсОО"])
    sheet.append(["Описание", "GTIN status"])
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
    for gtin, brand, functional_name, variety in rows:
        sheet.append(["Единичная упаковка", gtin, brand, "", "Русский", functional_name, variety])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
