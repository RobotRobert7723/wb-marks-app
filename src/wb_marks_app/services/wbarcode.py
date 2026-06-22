from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from wb_marks_app.exceptions import ManualStepRequired
from wb_marks_app.models import AppConfig
from wb_marks_app.services.browser import BrowserSessionManager


Logger = Callable[[str], None]


class WbarcodeService:
    def __init__(self, browser: BrowserSessionManager, logger: Logger | None = None) -> None:
        self.browser = browser
        self.logger = logger or (lambda _: None)

    def prepare_labels(self, marks_csv_path: Path, config: AppConfig) -> Path | None:
        self.browser.open_url(config.wbarcode_url)
        self.logger(f"Wbarcode input CSV ready: {marks_csv_path}")

        raise ManualStepRequired(
            "Wbarcode is configured as a semi-automatic step. "
            "The browser was opened; complete the click-flow from your operator instruction to generate the PDF."
        )

    @staticmethod
    def expected_pdf_path(output_dir: Path, supply_id: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return output_dir / f"wb_supply_{supply_id}_{timestamp}.pdf"

