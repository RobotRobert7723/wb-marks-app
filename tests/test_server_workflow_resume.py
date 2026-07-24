import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


class WorkflowRunResumeTests(unittest.TestCase):
    def test_completion_callback_runs_after_background_run_finishes(self) -> None:
        import wb_marks_app.services.server_workflow as server_workflow

        service = server_workflow.WorkflowRunService()
        completed_runs: list[str] = []
        service.register_completion_callback(lambda run_id: completed_runs.append(run_id))
        service._run_internal = lambda run_id: None

        service._run("run-completed")

        self.assertEqual(["run-completed"], completed_runs)

    def test_process_item_resumes_after_accepted_order_operation(self) -> None:
        import wb_marks_app.services.server_workflow as server_workflow
        from wb_marks_app.db import Base
        from wb_marks_app.models import AppConfig
        from wb_marks_app.server_models import TeksherOperationModel, WorkflowRunItemModel, WorkflowRunModel

        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)

        with TemporaryDirectory() as tmp:
            with Session(engine) as session:
                run = WorkflowRunModel(
                    id="run-resume",
                    user_id="user-1",
                    draft_id="CARD-1251098137-test",
                    source_url="/product-cards/1251098137",
                    status="running",
                    created_at=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
                )
                item = WorkflowRunItemModel(
                    run=run,
                    barcode="2053269441907",
                    vendor_code="bnk_hacks",
                    size="48",
                    gtin="04700092263593",
                    quantity=1,
                    status="order_running",
                    wb_item_name="Sport suit",
                )
                session.add(run)
                session.flush()
                session.add(
                    TeksherOperationModel(
                        run_item=item,
                        operation_kind="order",
                        external_operation_id="order-op-accepted",
                        status="ACCEPTED",
                        end_at="2026-07-21T23:55:26",
                    )
                )
                session.add(
                    TeksherOperationModel(
                        run_item=item,
                        operation_kind="marking",
                        external_operation_id="marking-op-created",
                        status="created",
                    )
                )
                session.add(
                    TeksherOperationModel(
                        run_item=item,
                        operation_kind="transgran",
                        external_operation_id="",
                        status="skipped",
                    )
                )
                session.commit()

                service = server_workflow.WorkflowRunService()
                old_teksher_service = server_workflow.TeksherService
                fake_teksher = _ResumeFakeTeksher
                fake_teksher.calls = []
                server_workflow.TeksherService = fake_teksher
                try:
                    service._process_item(
                        session,
                        run,
                        item,
                        AppConfig(
                            artifact_storage_dir=tmp,
                            transgran_document_number_prefix="WB",
                        ),
                    )
                finally:
                    server_workflow.TeksherService = old_teksher_service

                self.assertEqual("completed", item.status)
                self.assertEqual("", item.error)
                self.assertEqual("WB-CARD-1251098137-test-bnk_hacks_48", item.document_number)
                self.assertIn(("get_operation", "order-op-accepted"), fake_teksher.calls)
                self.assertIn(("get_operation", "marking-op-created"), fake_teksher.calls)
                self.assertNotIn(("wait_order", "order-op-accepted"), fake_teksher.calls)
                self.assertNotIn(("wait_marking", "marking-op-created"), fake_teksher.calls)
                self.assertEqual(1, len(item.mark_codes))
                self.assertEqual("MARK-CODE-1", item.mark_codes[0].mark_code)


class _ResumeFakeTeksher:
    calls: list[tuple[str, str]] = []

    def __init__(self, *args, **kwargs) -> None:
        return

    def create_mark_code_order(self, *args, **kwargs) -> str:
        self.calls.append(("create_order", ""))
        return "new-order-op"

    def wait_for_order_ready(self, operation_id, config):
        self.calls.append(("wait_order", operation_id))
        raise AssertionError("Accepted order operation must not be waited again")

    def create_marking_operation(self, order_operation_id, config) -> str:
        self.calls.append(("create_marking", order_operation_id))
        return "new-marking-op"

    def wait_for_operation(self, operation_id, expected_status, config):
        self.calls.append(("wait_marking", operation_id))
        return {"status": "ACCEPTED", "endAt": "2026-07-21T23:58:00"}

    def get_operation_details(self, operation_id, config):
        self.calls.append(("get_operation", operation_id))
        return {"status": "ACCEPTED", "endAt": "2026-07-21T23:58:00"}

    def save_operation_csv(self, operation_id, artifact_path, config) -> None:
        self.calls.append(("save_csv", operation_id))
        artifact_path.write_text("MARK-CODE-1\n", encoding="utf-8")

    def read_operation_codes(self, operation_id, config) -> list[str]:
        self.calls.append(("read_codes", operation_id))
        return ["MARK-CODE-1"]


if __name__ == "__main__":
    unittest.main()
