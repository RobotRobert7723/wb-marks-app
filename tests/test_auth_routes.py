import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient


class AuthRouteTests(unittest.TestCase):
    def test_runs_requires_login(self) -> None:
        with _isolated_app() as client:
            response = client.get("/runs", follow_redirects=False)
            self.assertEqual(303, response.status_code)
            self.assertEqual("/login", response.headers["location"])

    def test_two_users_can_share_email_with_isolated_settings(self) -> None:
        with _isolated_app() as client:
            register1 = client.post(
                "/register",
                data={
                    "email": "shared@example.com",
                    "login": "user_one",
                    "password": "password123",
                    "password_confirm": "password123",
                },
            )
            self.assertEqual(200, register1.status_code)

            save1 = client.post(
                "/settings",
                data={
                    "wb_api_base_url": "https://supplies-api.wildberries.ru",
                    "wb_api_token": "token-1",
                    "teksher_username": "teksher-1",
                    "teksher_password": "secret-1",
                    "teksher_transgran_recipient_name": "Recipient One",
                    "teksher_transgran_recipient_inn": "111",
                    "teksher_transgran_recipient_kpp": "222",
                    "supplier_name": "Supplier One",
                    "production_address": "Address One",
                    "mapping_mode": "size",
                    "mapping_payload": "{\"size_to_gtin\":{\"S\":\"GTIN-1\"}}",
                    "artifact_storage_dir": "/tmp/a1",
                    "transgran_document_number_prefix": "U1",
                    "step_timeout_seconds": "300",
                },
            )
            self.assertEqual(200, save1.status_code)
            self.assertIn("Recipient One", client.get("/settings").text)
            self.assertIn("Supplier One", client.get("/settings").text)

            logout1 = client.post("/logout", follow_redirects=False)
            self.assertEqual(303, logout1.status_code)

            register2 = client.post(
                "/register",
                data={
                    "email": "shared@example.com",
                    "login": "user_two",
                    "password": "password123",
                    "password_confirm": "password123",
                },
            )
            self.assertEqual(200, register2.status_code)
            settings2 = client.get("/settings")
            self.assertEqual(200, settings2.status_code)
            self.assertNotIn("Recipient One", settings2.text)
            self.assertNotIn("Supplier One", settings2.text)

            save2 = client.post(
                "/settings",
                data={
                    "wb_api_base_url": "https://supplies-api.wildberries.ru",
                    "wb_api_token": "token-2",
                    "teksher_username": "teksher-2",
                    "teksher_password": "secret-2",
                    "teksher_transgran_recipient_name": "Recipient Two",
                    "teksher_transgran_recipient_inn": "333",
                    "teksher_transgran_recipient_kpp": "444",
                    "supplier_name": "Supplier Two",
                    "production_address": "Address Two",
                    "mapping_mode": "size",
                    "mapping_payload": "{\"size_to_gtin\":{\"M\":\"GTIN-2\"}}",
                    "artifact_storage_dir": "/tmp/a2",
                    "transgran_document_number_prefix": "U2",
                    "step_timeout_seconds": "300",
                },
            )
            self.assertEqual(200, save2.status_code)
            self.assertIn("Recipient Two", client.get("/settings").text)
            self.assertIn("Supplier Two", client.get("/settings").text)

            client.post("/logout")
            login1 = client.post("/login", data={"login": "user_one", "password": "password123"})
            self.assertEqual(200, login1.status_code)
            self.assertIn("Recipient One", client.get("/settings").text)
            self.assertNotIn("Recipient Two", client.get("/settings").text)
            self.assertIn("Supplier One", client.get("/settings").text)
            self.assertNotIn("Supplier Two", client.get("/settings").text)

    def test_password_reset_flow_changes_password(self) -> None:
        with _isolated_app() as client:
            register = client.post(
                "/register",
                data={
                    "email": "reset@example.com",
                    "login": "reset_user",
                    "password": "password123",
                    "password_confirm": "password123",
                },
            )
            self.assertEqual(200, register.status_code)
            client.post("/logout")

            import wb_marks_app.webapp as webapp

            captured = {}

            def fake_send(config, to_email, login, reset_link):
                captured["to_email"] = to_email
                captured["login"] = login
                captured["reset_link"] = reset_link

            old_sender = webapp.send_password_reset_email
            old_app_base_url = os.environ.get("APP_BASE_URL")
            old_smtp_host = os.environ.get("SMTP_HOST")
            old_smtp_from = os.environ.get("SMTP_FROM_EMAIL")
            os.environ["APP_BASE_URL"] = "http://testserver"
            os.environ["SMTP_HOST"] = "smtp.example.com"
            os.environ["SMTP_FROM_EMAIL"] = "noreply@example.com"
            webapp.send_password_reset_email = fake_send
            try:
                response = client.post(
                    "/forgot-password",
                    data={"email": "reset@example.com", "login": "reset_user"},
                )
                self.assertEqual(200, response.status_code)
                self.assertIn("reset link has been sent", response.text)
                self.assertEqual("reset@example.com", captured["to_email"])
                self.assertEqual("reset_user", captured["login"])
                token = captured["reset_link"].rsplit("/", 1)[-1]

                reset_page = client.get(f"/reset-password/{token}")
                self.assertEqual(200, reset_page.status_code)

                reset_submit = client.post(
                    f"/reset-password/{token}",
                    data={"password": "newpassword123", "password_confirm": "newpassword123"},
                )
                self.assertEqual(200, reset_submit.status_code)

                client.post("/logout")
                login_old = client.post("/login", data={"login": "reset_user", "password": "password123"})
                self.assertIn("Неверный логин или пароль.", login_old.text)
                login_new = client.post("/login", data={"login": "reset_user", "password": "newpassword123"})
                self.assertEqual(200, login_new.status_code)
                self.assertIn("/runs", str(login_new.url))
            finally:
                webapp.send_password_reset_email = old_sender
                if old_app_base_url is None:
                    os.environ.pop("APP_BASE_URL", None)
                else:
                    os.environ["APP_BASE_URL"] = old_app_base_url
                if old_smtp_host is None:
                    os.environ.pop("SMTP_HOST", None)
                else:
                    os.environ["SMTP_HOST"] = old_smtp_host
                if old_smtp_from is None:
                    os.environ.pop("SMTP_FROM_EMAIL", None)
                else:
                    os.environ["SMTP_FROM_EMAIL"] = old_smtp_from


class _isolated_app:
    def __enter__(self):
        self.old_database_url = os.environ.get("DATABASE_URL")
        self.old_database_schema = os.environ.get("DATABASE_SCHEMA")
        self.tmp = TemporaryDirectory()
        db_path = Path(self.tmp.name) / "app.db"
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
        os.environ["DATABASE_SCHEMA"] = ""

        from wb_marks_app import db
        from wb_marks_app.webapp import create_app

        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._session_factory = None
        self.db = db
        self.client = TestClient(create_app())
        return self.client

    def __exit__(self, exc_type, exc, tb):
        self.client.close()
        if self.db._engine is not None:
            self.db._engine.dispose()
        self.db._engine = None
        self.db._session_factory = None
        self.tmp.cleanup()
        if self.old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.old_database_url
        if self.old_database_schema is None:
            os.environ.pop("DATABASE_SCHEMA", None)
        else:
            os.environ["DATABASE_SCHEMA"] = self.old_database_schema


if __name__ == "__main__":
    unittest.main()
