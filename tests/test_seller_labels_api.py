import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


class SellerLabelsApiTests(unittest.TestCase):
    def test_readiness_requires_token_and_mapping(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.models import AppConfig

        with _patched_app(webapp) as client:
            response = client.post("/api/v1/labels/readiness", json={})
            self.assertEqual(401, response.status_code)

            ready = client.post(
                "/api/v1/labels/readiness",
                headers=_auth_headers(),
                json={
                    "wbStoreId": "4006282",
                    "storeName": 'ОсОО "САДИЯН"',
                    "nmId": "336603350",
                    "vendorCode": "Adi_black_line_01",
                    "sizes": ["36"],
                },
            )
            self.assertEqual(200, ready.status_code)
            self.assertEqual("ready", ready.json()["status"])

            missing = client.post(
                "/api/v1/labels/readiness",
                headers=_auth_headers(),
                json={
                    "wbStoreId": "4006282",
                    "nmId": "336603350",
                    "sizes": ["38"],
                },
            )
            self.assertEqual(200, missing.status_code)
            self.assertEqual("setup_required", missing.json()["status"])
            self.assertIn("/product-cards/336603350#4006282", missing.json()["settingsUrl"])

    def test_print_job_is_idempotent_by_request_id(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.services.product_cards import ProductCardMappingRow, ProductCardTemplate, WbProductSummary

        product_card = _product_card(
            ProductCardMappingRow(
                barcode="2049271462634",
                wb_size="36",
                ru_size="134",
                teksher_size="36",
                product_type="Костюм спортивный",
                gtin="04709055620664",
                tnved="6112120000",
                country="КЫРГЫЗСТАН",
                vendor_article="Adi_black_line_01",
                color="ГОЛУБОЙ",
                composition="полиэстер 100%",
                target_gender="УНИВЕРСАЛЬНЫЙ (УНИСЕКС)",
                trademark="ErLine",
                ready_to_mark=0,
                print_count=0,
                order_count=0,
                full_name="Костюм спортивный",
            )
        )

        with _patched_app(webapp, product_card=product_card) as client:
            body = {
                "requestId": "wb-labels-test-1",
                "wbStoreId": "4006282",
                "storeName": 'ОсОО "САДИЯН"',
                "nmId": "336603350",
                "vendorCode": "Adi_black_line_01",
                "template": "simple_brand",
                "items": [{"size": "36", "quantity": 2}],
            }
            first = client.post("/api/v1/labels/print-jobs", headers=_auth_headers(), json=body)
            self.assertEqual(202, first.status_code)
            self.assertEqual("queued", first.json()["status"])
            self.assertEqual("emission", first.json()["rows"][0]["status"])
            self.assertEqual("simple_brand", client._test_context["workflow_service"].calls[0]["rows"][0]["label_template"])

            second = client.post("/api/v1/labels/print-jobs", headers=_auth_headers(), json=body)
            self.assertEqual(200, second.status_code)
            self.assertEqual(first.json()["jobId"], second.json()["jobId"])

            conflict_body = {**body, "items": [{"size": "36", "quantity": 3}]}
            conflict = client.post("/api/v1/labels/print-jobs", headers=_auth_headers(), json=conflict_body)
            self.assertEqual(409, conflict.status_code)

    def test_completed_print_job_generates_authorized_pdf(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.server_models import LabelApiJobModel, LabelApiJobRowModel, LabelPrintJobModel, MarkCodeModel, TeksherOperationModel, WorkflowRunModel
        from wb_marks_app.services.labels import GS
        from wb_marks_app.services.product_cards import ProductCardMappingRow

        product_row = ProductCardMappingRow(
            barcode="2049271462634",
            wb_size="36",
            ru_size="134",
            teksher_size="36",
            product_type="Костюм спортивный",
            gtin="04709055620664",
            tnved="6112120000",
            country="КЫРГЫЗСТАН",
            vendor_article="Adi_black_line_01",
            color="ГОЛУБОЙ",
            composition="полиэстер 100%",
            target_gender="УНИВЕРСАЛЬНЫЙ (УНИСЕКС)",
            trademark="ErLine",
            ready_to_mark=0,
            print_count=0,
            order_count=0,
            full_name="Костюм спортивный",
        )
        product_card = _product_card(product_row)
        valid_mark_code = "0104709055620664215YudSpca<mc9X" + GS + "91EE12" + GS + "92" + ("A" * 44)

        with _patched_app(webapp, product_card=product_card) as client:
            context = client._test_context
            with Session(context["engine"]) as session:
                run, item = _create_completed_run(session)
                session.add(TeksherOperationModel(run_item=item, operation_kind="marking", status="ACCEPTED"))
                session.add(MarkCodeModel(run_item=item, position=1, mark_code=valid_mark_code))
                job = LabelApiJobModel(
                    request_id="wb-labels-test-2",
                    request_hash="hash",
                    wb_store_id="4006282",
                    user_id="user-1",
                    nm_id="336603350",
                    vendor_code="Adi_black_line_01",
                    template="srad",
                    status="processing",
                    run_id=run.id,
                )
                session.add(job)
                session.flush()
                session.add(LabelApiJobRowModel(job_id=job.id, size="36", quantity=1, gtin="04709055620664"))
                session.commit()
                job_id = job.id
                run_id = run.id

            status = client.get(f"/api/v1/labels/print-jobs/{job_id}", headers=_auth_headers())
            self.assertEqual(200, status.status_code)
            payload = status.json()
            self.assertEqual("done", payload["status"])
            self.assertEqual("ready", payload["rows"][0]["status"])
            self.assertEqual("готово к печати", payload["rows"][0]["statusLabel"])
            self.assertIn("/api/v1/labels/files/", payload["pdfUrl"])
            self.assertEqual([], context["workflow_service"].started_runs)

            pdf = client.get(payload["pdfUrl"], headers=_auth_headers())
            self.assertEqual(200, pdf.status_code)
            self.assertEqual(b"%PDF\n/Type /Page\n/Type /Pages\n", pdf.content)
            self.assertIn("wb_Adi_black_line_01_36.pdf", pdf.headers["content-disposition"])

            with Session(context["engine"]) as session:
                run = session.get(WorkflowRunModel, run_id)
                self.assertEqual("completed", run.status)
                print_jobs = session.query(LabelPrintJobModel).all()
                self.assertEqual(1, len(print_jobs))
                self.assertEqual("srad", print_jobs[0].template)
                self.assertEqual("wb_Adi_black_line_01_36.pdf", print_jobs[0].file_name)

    def test_print_job_keeps_polling_until_rows_are_terminal(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.server_models import (
            LabelApiJobModel,
            LabelApiJobRowModel,
            LabelPrintJobModel,
            MarkCodeModel,
            WorkflowRunItemModel,
            WorkflowRunModel,
        )
        from wb_marks_app.services.labels import GS
        from wb_marks_app.services.product_cards import ProductCardMappingRow, ProductCardTemplate, WbProductSummary

        row_36 = ProductCardMappingRow(
            barcode="2049271462634",
            wb_size="36",
            ru_size="134",
            teksher_size="36",
            product_type="Костюм спортивный",
            gtin="04709055620664",
            tnved="6112120000",
            country="КЫРГЫЗСТАН",
            vendor_article="Adi_black_line_01",
            color="ГОЛУБОЙ",
            composition="полиэстер 100%",
            target_gender="УНИВЕРСАЛЬНЫЙ (УНИСЕКС)",
            trademark="ErLine",
            ready_to_mark=0,
            print_count=0,
            order_count=0,
            full_name="Костюм спортивный",
        )
        row_40 = ProductCardMappingRow(
            barcode="2049271462641",
            wb_size="40",
            ru_size="140",
            teksher_size="40",
            product_type="Костюм спортивный",
            gtin="04709055620664",
            tnved="6112120000",
            country="КЫРГЫЗСТАН",
            vendor_article="Adi_black_line_01",
            color="ГОЛУБОЙ",
            composition="полиэстер 100%",
            target_gender="УНИВЕРСАЛЬНЫЙ (УНИСЕКС)",
            trademark="ErLine",
            ready_to_mark=0,
            print_count=0,
            order_count=0,
            full_name="Костюм спортивный",
        )
        product_card = ProductCardTemplate(
            wb_article="336603350",
            image_url="",
            api_status="",
            wb_summary=WbProductSummary(
                name="Sport suit",
                seller_category="Sport suits",
                wb_article="336603350",
                tnved="6112120000",
                country="Киргизия",
                seller_article="Adi_black_line_01",
                color="голубой",
                composition="полиэстер 100%",
                gender="Детский",
                brand="ErLine",
            ),
            rows=[row_36, row_40],
        )

        def mark_code(serial: str) -> str:
            return "010470905562066421" + serial + GS + "91EE12" + GS + "92" + ("A" * 44)

        with _patched_app(webapp, product_card=product_card) as client:
            context = client._test_context
            with Session(context["engine"]) as session:
                run = WorkflowRunModel(
                    id="run-partial",
                    user_id="user-1",
                    draft_id="CARD-336603350-partial",
                    source_url="/product-cards/336603350",
                    status="running",
                    created_at=datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc),
                )
                item_36 = WorkflowRunItemModel(
                    run=run,
                    barcode="2049271462634",
                    vendor_code="Adi_black_line_01",
                    size="36",
                    gtin="04709055620664",
                    quantity=2,
                    status="completed",
                    wb_item_name="Sport suit",
                )
                item_38 = WorkflowRunItemModel(
                    run=run,
                    barcode="2049271462635",
                    vendor_code="Adi_black_line_01",
                    size="38",
                    gtin="04709055620664",
                    quantity=1,
                    status="failed",
                    error="Balance is too low.",
                    wb_item_name="Sport suit",
                )
                item_40 = WorkflowRunItemModel(
                    run=run,
                    barcode="2049271462641",
                    vendor_code="Adi_black_line_01",
                    size="40",
                    gtin="04709055620664",
                    quantity=1,
                    status="order_running",
                    wb_item_name="Sport suit",
                )
                session.add_all([run, item_36, item_38, item_40])
                session.flush()
                session.add_all(
                    [
                        MarkCodeModel(run_item=item_36, position=1, mark_code=mark_code("SERIAL-A")),
                        MarkCodeModel(run_item=item_36, position=2, mark_code=mark_code("SERIAL-B")),
                        MarkCodeModel(run_item=item_36, position=3, mark_code=mark_code("EXTRA-C")),
                    ]
                )
                job = LabelApiJobModel(
                    request_id="wb-labels-partial",
                    request_hash="hash",
                    wb_store_id="4006282",
                    user_id="user-1",
                    nm_id="336603350",
                    vendor_code="Adi_black_line_01",
                    template="srad",
                    status="processing",
                    run_id=run.id,
                )
                session.add(job)
                session.flush()
                session.add_all(
                    [
                        LabelApiJobRowModel(job_id=job.id, size="36", quantity=2, gtin="04709055620664"),
                        LabelApiJobRowModel(job_id=job.id, size="38", quantity=1, gtin="04709055620664"),
                        LabelApiJobRowModel(job_id=job.id, size="40", quantity=1, gtin="04709055620664"),
                    ]
                )
                session.commit()
                job_id = job.id
                item_40_id = item_40.id

            status = client.get(f"/api/v1/labels/print-jobs/{job_id}", headers=_auth_headers())
            payload = status.json()
            self.assertEqual("processing", payload["status"])
            statuses = {row["size"]: row for row in payload["rows"]}
            self.assertEqual("transgran", statuses["36"]["status"])
            self.assertEqual("error", statuses["38"]["status"])
            self.assertEqual("emission", statuses["40"]["status"])

            with Session(context["engine"]) as session:
                item_40 = session.get(WorkflowRunItemModel, item_40_id)
                item_40.status = "completed"
                run = session.get(WorkflowRunModel, "run-partial")
                run.status = "partial_failed"
                session.add(MarkCodeModel(run_item=item_40, position=1, mark_code=mark_code("SERIAL-D")))
                session.commit()

            final = client.get(f"/api/v1/labels/print-jobs/{job_id}", headers=_auth_headers())
            final_payload = final.json()
            self.assertEqual("partial_failed", final_payload["status"])
            final_rows = {row["size"]: row for row in final_payload["rows"]}
            self.assertEqual("ready", final_rows["36"]["status"])
            self.assertEqual(2, final_rows["36"]["readyToPrintCount"])
            self.assertEqual("error", final_rows["38"]["status"])
            self.assertEqual("ready", final_rows["40"]["status"])
            self.assertIn("/api/v1/labels/files/", final_payload["pdfUrl"])
            self.assertIn("/api/v1/labels/files/", final_rows["36"]["pdfUrl"])
            self.assertIn("/api/v1/labels/files/", final_rows["40"]["pdfUrl"])
            self.assertNotEqual(final_rows["36"]["pdfUrl"], final_rows["40"]["pdfUrl"])

            with Session(context["engine"]) as session:
                print_jobs = session.query(LabelPrintJobModel).order_by(LabelPrintJobModel.wb_size).all()
                self.assertEqual([2, 1], [print_job.quantity for print_job in print_jobs])
                self.assertEqual(
                    ["wb_Adi_black_line_01_36.pdf", "wb_Adi_black_line_01_40.pdf"],
                    [print_job.file_name for print_job in print_jobs],
                )

            pdf_36 = client.get(final_rows["36"]["pdfUrl"], headers=_auth_headers())
            pdf_40 = client.get(final_rows["40"]["pdfUrl"], headers=_auth_headers())
            self.assertEqual(200, pdf_36.status_code)
            self.assertEqual(200, pdf_40.status_code)
            self.assertIn("wb_Adi_black_line_01_36.pdf", pdf_36.headers["content-disposition"])
            self.assertIn("wb_Adi_black_line_01_40.pdf", pdf_40.headers["content-disposition"])

    def test_print_job_retry_resumes_orphaned_running_run(self) -> None:
        import wb_marks_app.webapp as webapp
        from wb_marks_app.server_models import LabelApiJobModel, LabelApiJobRowModel

        with _patched_app(webapp) as client:
            context = client._test_context
            with Session(context["engine"]) as session:
                run = _create_running_run(session)
                run.error = "stale infrastructure error"
                job = LabelApiJobModel(
                    request_id="wb-labels-orphaned",
                    request_hash="hash",
                    wb_store_id="4006282",
                    user_id="user-1",
                    nm_id="336603350",
                    vendor_code="Adi_black_line_01",
                    template="srad",
                    status="processing",
                    run_id=run.id,
                )
                session.add(job)
                session.flush()
                session.add(
                    LabelApiJobRowModel(
                        job_id=job.id,
                        size="36",
                        quantity=1,
                        gtin="04709055620664",
                        status="error",
                        error_message="Local polling timeout.",
                    )
                )
                session.commit()
                job_id = job.id

            retry = client.post(
                f"/api/v1/labels/print-jobs/{job_id}/retry",
                headers=_auth_headers(),
                json={"sizes": ["36"]},
            )

            self.assertEqual(200, retry.status_code)
            payload = retry.json()
            self.assertEqual("processing", payload["status"])
            self.assertEqual("emission", payload["rows"][0]["status"])
            self.assertEqual(["run-1"], context["workflow_service"].started_runs)
            with Session(context["engine"]) as session:
                from wb_marks_app.server_models import WorkflowRunModel

                run = session.get(WorkflowRunModel, "run-1")
                self.assertEqual("", run.error)


class _FakeProductCardService:
    def __init__(self, product_card) -> None:
        self.product_card = product_card

    def build_template(self, wb_article, config):
        return self.product_card


class _FakeWorkflowService:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.calls = []
        self.started_runs = []
        self.retried_items = []

    def create_product_card_run(self, user_id, wb_article, product_card, rows):
        self.calls.append({"user_id": user_id, "wb_article": wb_article, "rows": rows})
        with Session(self.engine) as session:
            run = _create_running_run(session, quantity=rows[0]["quantity"])
            session.commit()
            return run.id

    def start_background(self, run_id):
        self.started_runs.append(run_id)

    def retry_items(self, run_id, item_ids):
        self.retried_items.append({"run_id": run_id, "item_ids": set(item_ids or [])})
        with Session(self.engine) as session:
            from wb_marks_app.server_models import WorkflowRunItemModel, WorkflowRunModel

            run = session.get(WorkflowRunModel, run_id)
            if run is not None:
                run.status = "running"
            for item in session.query(WorkflowRunItemModel).filter(WorkflowRunItemModel.run_id == run_id).all():
                if item.id in set(item_ids or []) and item.status == "failed":
                    item.status = "pending"
                    item.error = ""
            session.commit()


@contextmanager
def _patched_app(webapp, product_card=None):
    from wb_marks_app.db import Base
    from wb_marks_app.models import AppConfig

    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_store(session)
        session.commit()

    old_create_all = webapp.create_all
    old_load_config = webapp.load_config
    old_session_scope = webapp.session_scope
    old_product_card_service = webapp.product_card_service
    old_workflow_service = webapp.workflow_service
    old_render_labels_pdf = webapp.render_labels_pdf
    old_label_pdf_file_path = webapp._label_pdf_file_path

    @contextmanager
    def scope():
        with Session(engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    with TemporaryDirectory() as tmp:
        try:
            webapp.create_all = lambda: None
            webapp.load_config = lambda: AppConfig(
                secret_key="test-secret",
                label_api_token="label-token",
                app_base_url="https://marksapp.sesrv.ru",
                artifact_storage_dir=tmp,
            )
            webapp.session_scope = scope
            if product_card is not None:
                webapp.product_card_service = _FakeProductCardService(product_card)
            fake_workflow_service = _FakeWorkflowService(engine)
            webapp.workflow_service = fake_workflow_service
            webapp.render_labels_pdf = lambda _labels, template: b"%PDF\n/Type /Page\n/Type /Pages\n"
            webapp._label_pdf_file_path = lambda _user_id, file_id: Path(tmp) / f"{file_id}.pdf"

            client = TestClient(webapp.create_app())
            client._test_context = {"engine": engine, "workflow_service": fake_workflow_service}
            try:
                yield client
            finally:
                client.close()
        finally:
            webapp.create_all = old_create_all
            webapp.load_config = old_load_config
            webapp.session_scope = old_session_scope
            webapp.product_card_service = old_product_card_service
            webapp.workflow_service = old_workflow_service
            webapp.render_labels_pdf = old_render_labels_pdf
            webapp._label_pdf_file_path = old_label_pdf_file_path


def _seed_store(session: Session) -> None:
    from wb_marks_app.server_models import AppSettingsModel, TeksherMappingModel, UserModel

    session.add(
        UserModel(
            id="user-1",
            email="sadiyan@example.test",
            login="sadiyan",
            password_hash="hash",
            wb_store_id="4006282",
        )
    )
    session.add(
        AppSettingsModel(
            user_id="user-1",
            wb_store_id="4006282",
            wb_api_token="wb-token",
            teksher_username="teksher-user",
            teksher_password="teksher-password",
            teksher_transgran_recipient_name="Recipient",
            teksher_transgran_recipient_inn="1234567890",
            teksher_transgran_recipient_kpp="123456789",
            supplier_name="Supplier",
            production_address="Address",
        )
    )
    session.add(
        TeksherMappingModel(
            user_id="user-1",
            version=1,
            source="test",
            wb_article="336603350",
            wb_name="Sport suit",
            wb_seller_category="Sport suits",
            wb_tnved="6112120000",
            wb_country="Киргизия",
            wb_seller_article="Adi_black_line_01",
            wb_color="голубой",
            wb_composition="полиэстер 100%",
            wb_gender="Детский",
            wb_brand="ErLine",
            wb_barcode="2049271462634",
            wb_size="36",
            wb_ru_size="134",
            full_name="Костюм спортивный",
            teksher_size="36",
            product_type="Костюм спортивный",
            gtin="04709055620664",
            tnved="6112120000",
            country="КЫРГЫЗСТАН",
            vendor_article="Adi_black_line_01",
            color="ГОЛУБОЙ",
            composition="полиэстер 100%",
            target_gender="УНИВЕРСАЛЬНЫЙ (УНИСЕКС)",
            trademark="ErLine",
        )
    )


def _product_card(row):
    from wb_marks_app.services.product_cards import ProductCardTemplate, WbProductSummary

    return ProductCardTemplate(
        wb_article="336603350",
        image_url="",
        api_status="",
        wb_summary=WbProductSummary(
            name="Sport suit",
            seller_category="Sport suits",
            wb_article="336603350",
            tnved="6112120000",
            country="Киргизия",
            seller_article="Adi_black_line_01",
            color="голубой",
            composition="полиэстер 100%",
            gender="Детский",
            brand="ErLine",
        ),
        rows=[row],
    )


def _create_running_run(session: Session, quantity: int = 1):
    from wb_marks_app.server_models import WorkflowRunItemModel, WorkflowRunModel

    run = WorkflowRunModel(
        id="run-1",
        user_id="user-1",
        draft_id="CARD-336603350-test",
        source_url="/product-cards/336603350",
        status="running",
        created_at=datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc),
    )
    item = WorkflowRunItemModel(
        run=run,
        barcode="2049271462634",
        vendor_code="Adi_black_line_01",
        size="36",
        gtin="04709055620664",
        quantity=quantity,
        status="order_running",
        wb_item_name="Sport suit",
    )
    session.add(run)
    session.flush()
    return run


def _create_completed_run(session: Session):
    from wb_marks_app.server_models import WorkflowRunItemModel, WorkflowRunModel

    run = WorkflowRunModel(
        id="run-completed",
        user_id="user-1",
        draft_id="CARD-336603350-completed",
        source_url="/product-cards/336603350",
        status="completed",
        created_at=datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc),
    )
    item = WorkflowRunItemModel(
        run=run,
        barcode="2049271462634",
        vendor_code="Adi_black_line_01",
        size="36",
        gtin="04709055620664",
        quantity=1,
        status="completed",
        wb_item_name="Sport suit",
    )
    session.add(run)
    session.flush()
    return run, item


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer label-token"}
