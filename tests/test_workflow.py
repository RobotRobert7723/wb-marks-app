from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from wb_marks_app.exceptions import ManualStepRequired
from wb_marks_app.models import AppConfig, DraftSupply, SupplyItem
from wb_marks_app.services.mapping import MappingService, ResultCsvService
from wb_marks_app.services.workflow import WorkflowOrchestrator, WorkflowServices


class FakeWbService:
    def get_latest_draft_supply(self, _config: AppConfig) -> DraftSupply:
        return DraftSupply(
            supply_id="SUP-1",
            name="Draft",
            status="draft",
            created_at=datetime.now(timezone.utc),
            source="test",
            items=[
                SupplyItem(
                    barcode="123",
                    name="T-Shirt",
                    quantity=2,
                    draft_supply_id="SUP-1",
                )
            ],
        )


class FakeTeksherService:
    def issue_marks(self, tasks, _config):
        for index, task in enumerate(tasks, start=1):
            task.status = "issued"
            task.mark_code = f"MARK-{index}"
        return tasks


class FakeWbarcodeService:
    def prepare_labels(self, _marks_csv_path: Path, _config: AppConfig):
        raise ManualStepRequired("Manual PDF step")


class WorkflowTests(unittest.TestCase):
    def test_workflow_generates_csv_and_preserves_manual_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            mapping_path = Path(tmp) / "mapping.csv"
            mapping_path.write_text("barcode,gtin\n123,GTIN123\n", encoding="utf-8")

            config = AppConfig(
                mapping_csv_path=str(mapping_path),
                output_dir=tmp,
                browser_profile_dir=str(Path(tmp) / "profile"),
            )
            services = WorkflowServices(
                wb_service=FakeWbService(),
                mapping_service=MappingService(),
                result_csv_service=ResultCsvService(),
                teksher_service=FakeTeksherService(),
                wbarcode_service=FakeWbarcodeService(),
            )
            events = []
            orchestrator = WorkflowOrchestrator(services, progress=events.append)
            outcome = orchestrator.run(config)

            self.assertIsNotNone(outcome.result)
            self.assertTrue(outcome.result.marks_csv_path.exists())
            self.assertEqual(2, len(outcome.result.tasks))
            self.assertEqual(["Manual PDF step"], outcome.warnings)
            self.assertTrue(any("Prepared 2 marking tasks." in event.message for event in events))


if __name__ == "__main__":
    unittest.main()
