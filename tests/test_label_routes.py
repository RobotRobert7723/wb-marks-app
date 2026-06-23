import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlparse

from fastapi.testclient import TestClient


class LabelRouteTests(unittest.TestCase):
    def test_manual_label_pages_render(self) -> None:
        old_database_url = os.environ.get("DATABASE_URL")
        old_database_schema = os.environ.get("DATABASE_SCHEMA")
        old_output_dir = os.environ.get("OUTPUT_DIR")
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
            os.environ["DATABASE_SCHEMA"] = ""
            os.environ["OUTPUT_DIR"] = (Path(tmp) / "output").as_posix()

            from wb_marks_app import db
            from wb_marks_app.webapp import create_app

            if db._engine is not None:
                db._engine.dispose()
            db._engine = None
            db._session_factory = None
            client = TestClient(create_app())

            register = client.post(
                "/register",
                data={
                    "email": "user@example.com",
                    "login": "user1",
                    "password": "password123",
                    "password_confirm": "password123",
                },
            )
            self.assertEqual(200, register.status_code)

            page = client.get("/labels")
            self.assertEqual(200, page.status_code)
            self.assertIn("Этикетки", page.text)
            self.assertIn("SRad", page.text)
            self.assertIn("Simple", page.text)
            self.assertIn("Medium", page.text)
            self.assertIn("template_srad.png", page.text)
            self.assertIn("template_simple.png", page.text)
            self.assertIn("template_medium.png", page.text)
            self.assertNotIn('id="mark_codes"', page.text)
            self.assertNotIn('id="wb_barcode"', page.text)

            preview = client.post(
                "/labels/preview",
                data={
                    "template": "srad",
                    "item_name": "Suit",
                    "vendor_code": "sku",
                    "size": "S",
                    "wb_barcode": "2043467523239",
                    "mark_codes": "0104700092263449215abc\x1d91EE12\x1d92crypto",
                },
            )
            self.assertEqual(200, preview.status_code)
            self.assertIn("bwip-js-min.js", preview.text)
            self.assertIn("2043467523239", preview.text)

            if shutil.which("node") is not None:
                valid_mark_code = "0104709055620664215YudSpca<mc9X\x1d91EE12\x1d92" + ("A" * 44)
                pdf = client.post(
                    "/labels/pdf",
                    data={
                        "template": "srad",
                        "item_name": "Suit",
                        "vendor_code": "sku",
                        "size": "S",
                        "wb_barcode": "2043467523239",
                        "mark_codes": valid_mark_code,
                    },
                )
                self.assertEqual(200, pdf.status_code)
                self.assertEqual("application/pdf", pdf.headers["content-type"].split(";")[0])
                self.assertTrue(pdf.content.startswith(b"%PDF"))

                bad_pdf = client.post(
                    "/labels/pdf",
                    data={
                        "template": "srad",
                        "item_name": "Suit",
                        "wb_barcode": "2043467523239",
                        "mark_codes": "0104709055620664215YudSpca<mc9X",
                    },
                )
                self.assertEqual(400, bad_pdf.status_code)
                self.assertIn("AI 91/92", bad_pdf.text)

                api_pdf = client.post(
                    "/api/labels/pdf",
                    json={
                        "Название шаблона": "SRad",
                        "Наименование": "Suit",
                        "WB barcode": "2043467523239",
                        "Артикул": "sku",
                        "Размер": "S",
                        "Цвет": "blue",
                        "Состав": "Cotton 50%",
                        "Поставщик": "Supplier",
                        "Дата производства": "01.06.2026",
                        "Страна производства": "Kyrgyzstan",
                        "Комплектность": "Hoodie-1pc",
                        "Адрес поставщика": "Bishkek, Test street, 35",
                        "Коды ЧЗ": [valid_mark_code],
                    },
                )
                self.assertEqual(200, api_pdf.status_code)
                api_payload = api_pdf.json()
                self.assertTrue(api_payload["ok"])
                self.assertEqual("srad", api_payload["template"])
                self.assertEqual(1, api_payload["labels_count"])
                self.assertEqual(4, api_payload["pages_count"])

                download_path = urlparse(api_payload["download_url"]).path
                download = client.get(download_path)
                self.assertEqual(200, download.status_code)
                self.assertEqual("application/pdf", download.headers["content-type"].split(";")[0])
                self.assertTrue(download.content.startswith(b"%PDF"))

            client.close()
            if db._engine is not None:
                db._engine.dispose()
            db._engine = None
            db._session_factory = None
        if old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_database_url
        if old_database_schema is None:
            os.environ.pop("DATABASE_SCHEMA", None)
        else:
            os.environ["DATABASE_SCHEMA"] = old_database_schema
        if old_output_dir is None:
            os.environ.pop("OUTPUT_DIR", None)
        else:
            os.environ["OUTPUT_DIR"] = old_output_dir


if __name__ == "__main__":
    unittest.main()
