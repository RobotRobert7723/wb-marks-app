import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient


class LabelRouteTests(unittest.TestCase):
    def test_manual_label_pages_render(self) -> None:
        old_database_url = os.environ.get("DATABASE_URL")
        old_database_schema = os.environ.get("DATABASE_SCHEMA")
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
            os.environ["DATABASE_SCHEMA"] = ""

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
            self.assertIn("60x40", page.text)

            preview = client.post(
                "/labels/preview",
                data={
                    "template": "combined",
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


if __name__ == "__main__":
    unittest.main()
