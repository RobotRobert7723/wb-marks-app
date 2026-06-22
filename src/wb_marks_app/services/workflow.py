from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from wb_marks_app.exceptions import AppError, ManualStepRequired
from wb_marks_app.models import AppConfig, ProgressEvent, WorkflowResult
from wb_marks_app.services.mapping import MappingService, ResultCsvService
from wb_marks_app.services.teksher import TeksherService
from wb_marks_app.services.wb import WBService
from wb_marks_app.services.wbarcode import WbarcodeService


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(slots=True)
class WorkflowServices:
    wb_service: WBService
    mapping_service: MappingService
    result_csv_service: ResultCsvService
    teksher_service: TeksherService
    wbarcode_service: WbarcodeService


@dataclass(slots=True)
class WorkflowOutcome:
    result: WorkflowResult | None = None
    warnings: list[str] = field(default_factory=list)


class WorkflowOrchestrator:
    def __init__(self, services: WorkflowServices, progress: ProgressCallback | None = None) -> None:
        self.services = services
        self.progress = progress or (lambda _: None)

    def run(self, config: AppConfig) -> WorkflowOutcome:
        warnings: list[str] = []
        self._info("Loading latest WB draft supply...")
        supply = self.services.wb_service.get_latest_draft_supply(config)
        self._info(
            f"Loaded WB draft {supply.supply_id} from {supply.source or 'unknown source'} with {len(supply.items)} items."
        )

        mapping_path = config.resolved_mapping_path()
        if mapping_path is None:
            raise AppError("Mapping CSV path is not configured.")

        self._info("Validating mapping CSV and building marking tasks...")
        tasks = self.services.mapping_service.build_tasks(supply.items, mapping_path)
        self._info(f"Prepared {len(tasks)} marking tasks.")

        self._info("Opening label.teksher.kg and starting the marking step...")
        try:
            tasks = self.services.teksher_service.issue_marks(tasks, config)
        except ManualStepRequired as exc:
            warnings.append(str(exc))
            self._warn(str(exc))

        output_dir = config.resolved_output_dir()
        self._info("Writing marks CSV to the output folder...")
        marks_csv_path = self.services.result_csv_service.write(tasks, output_dir, supply.supply_id)
        self._info(f"Marks CSV saved to {marks_csv_path}")

        pdf_path: Path | None = None
        self._info("Opening wbarcode.ru for the PDF label step...")
        try:
            pdf_path = self.services.wbarcode_service.prepare_labels(marks_csv_path, config)
        except ManualStepRequired as exc:
            warnings.append(str(exc))
            self._warn(str(exc))

        result = WorkflowResult(
            supply=supply,
            tasks=tasks,
            marks_csv_path=marks_csv_path,
            pdf_path=pdf_path,
            warnings=warnings,
        )
        return WorkflowOutcome(result=result, warnings=warnings)

    def _info(self, message: str) -> None:
        self.progress(ProgressEvent(level="info", message=message))

    def _warn(self, message: str) -> None:
        self.progress(ProgressEvent(level="warning", message=message))

