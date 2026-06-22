from __future__ import annotations

import traceback
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from wb_marks_app.config import load_config, save_config
from wb_marks_app.models import AppConfig, ProgressEvent, WorkflowResult
from wb_marks_app.services.browser import BrowserSessionManager
from wb_marks_app.services.mapping import MappingService, ResultCsvService
from wb_marks_app.services.teksher import TeksherService
from wb_marks_app.services.wb import WBService
from wb_marks_app.services.wbarcode import WbarcodeService
from wb_marks_app.services.workflow import WorkflowOrchestrator, WorkflowServices


class WorkflowWorker(QObject):
    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config

    def run(self) -> None:
        browser = BrowserSessionManager(
            profile_dir=self.config.resolved_profile_dir(),
            logger=lambda message: self.progress.emit(ProgressEvent(level="info", message=message)),
        )
        try:
            services = WorkflowServices(
                wb_service=WBService(browser),
                mapping_service=MappingService(),
                result_csv_service=ResultCsvService(),
                teksher_service=TeksherService(browser),
                wbarcode_service=WbarcodeService(browser),
            )
            orchestrator = WorkflowOrchestrator(services, progress=lambda event: self.progress.emit(event))
            outcome = orchestrator.run(self.config)
            self.finished.emit(outcome.result)
        except Exception as exc:  # pragma: no cover - UI glue
            details = "".join(traceback.format_exception(exc))
            self.failed.emit(details)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("WB Marks App")
        self.resize(1100, 760)
        self._thread: QThread | None = None
        self._worker: WorkflowWorker | None = None
        self._result: WorkflowResult | None = None
        self.config = load_config()

        self.mapping_path_input = QLineEdit()
        self.output_dir_input = QLineEdit()
        self.profile_dir_input = QLineEdit()
        self.wb_mode_input = QComboBox()
        self.pause_checkbox = QCheckBox("Pause on manual browser step")
        self.timeout_input = QSpinBox()
        self.wb_api_base_url_input = QLineEdit()
        self.wb_api_token_input = QLineEdit()
        self.wb_json_input = QLineEdit()
        self.teksher_url_input = QLineEdit()
        self.wbarcode_url_input = QLineEdit()
        self.log_output = QPlainTextEdit()
        self.status_label = QLabel("Idle")
        self.summary_label = QLabel("No run yet")
        self.last_csv_label = QLabel("-")
        self.last_pdf_label = QLabel("-")

        self._build_ui()
        self._load_into_form()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(12)

        layout.addWidget(self._build_settings_group())
        layout.addWidget(self._build_runtime_group())
        layout.addWidget(self._build_log_group(), stretch=1)

        self.setCentralWidget(root)

    def _build_settings_group(self) -> QGroupBox:
        group = QGroupBox("Settings")
        grid = QGridLayout(group)

        form = QFormLayout()
        self.wb_mode_input.addItems(["auto", "api", "browser"])
        self.timeout_input.setRange(5, 600)
        self.timeout_input.setSingleStep(5)
        self.wb_api_token_input.setEchoMode(QLineEdit.PasswordEchoOnEdit)

        form.addRow("Mapping CSV", self._path_field(self.mapping_path_input, self._pick_mapping_file))
        form.addRow("Output folder", self._path_field(self.output_dir_input, self._pick_output_dir))
        form.addRow("Browser profile", self._path_field(self.profile_dir_input, self._pick_profile_dir))
        form.addRow("WB mode", self.wb_mode_input)
        form.addRow("Step timeout (sec)", self.timeout_input)
        form.addRow("WB API base URL", self.wb_api_base_url_input)
        form.addRow("WB API token", self.wb_api_token_input)
        form.addRow("WB draft JSON", self._path_field(self.wb_json_input, self._pick_wb_json))
        form.addRow("Teksher URL", self.teksher_url_input)
        form.addRow("Wbarcode URL", self.wbarcode_url_input)
        form.addRow("", self.pause_checkbox)

        buttons = QHBoxLayout()
        save_button = QPushButton("Save settings")
        save_button.clicked.connect(self.save_settings)
        run_button = QPushButton("Run workflow")
        run_button.clicked.connect(self.run_workflow)
        buttons.addWidget(save_button)
        buttons.addWidget(run_button)
        buttons.addStretch(1)

        grid.addLayout(form, 0, 0)
        grid.addLayout(buttons, 1, 0)
        return group

    def _build_runtime_group(self) -> QGroupBox:
        group = QGroupBox("Runtime")
        form = QFormLayout(group)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.summary_label.setWordWrap(True)
        self.last_csv_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.last_pdf_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Status", self.status_label)
        form.addRow("Summary", self.summary_label)
        form.addRow("Last marks CSV", self.last_csv_label)
        form.addRow("Last PDF", self.last_pdf_label)
        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("Log")
        layout = QVBoxLayout(group)
        self.log_output.setReadOnly(True)
        layout.addWidget(self.log_output)
        return group

    def _path_field(self, line_edit: QLineEdit, picker) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton("Browse")
        button.clicked.connect(picker)
        layout.addWidget(line_edit, stretch=1)
        layout.addWidget(button)
        return wrapper

    def _load_into_form(self) -> None:
        self.mapping_path_input.setText(self.config.mapping_csv_path)
        self.output_dir_input.setText(self.config.output_dir)
        self.profile_dir_input.setText(self.config.browser_profile_dir)
        self.wb_mode_input.setCurrentText(self.config.wb_mode)
        self.pause_checkbox.setChecked(self.config.pause_on_manual_step)
        self.timeout_input.setValue(self.config.step_timeout_seconds)
        self.wb_api_base_url_input.setText(self.config.wb_api_base_url)
        self.wb_api_token_input.setText(self.config.wb_api_token)
        self.wb_json_input.setText(self.config.wb_draft_source_file)
        self.teksher_url_input.setText(self.config.teksher_url)
        self.wbarcode_url_input.setText(self.config.wbarcode_url)
        self.last_csv_label.setText(self.config.last_marks_csv_path or "-")
        self.last_pdf_label.setText(self.config.last_pdf_path or "-")

    def _collect_config(self) -> AppConfig:
        return AppConfig(
            mapping_csv_path=self.mapping_path_input.text().strip(),
            output_dir=self.output_dir_input.text().strip(),
            browser_profile_dir=self.profile_dir_input.text().strip(),
            wb_mode=self.wb_mode_input.currentText(),  # type: ignore[arg-type]
            pause_on_manual_step=self.pause_checkbox.isChecked(),
            step_timeout_seconds=self.timeout_input.value(),
            wb_api_base_url=self.wb_api_base_url_input.text().strip(),
            wb_api_token=self.wb_api_token_input.text().strip(),
            wb_draft_source_file=self.wb_json_input.text().strip(),
            teksher_url=self.teksher_url_input.text().strip() or self.config.teksher_url,
            wbarcode_url=self.wbarcode_url_input.text().strip() or self.config.wbarcode_url,
            last_marks_csv_path=self.config.last_marks_csv_path,
            last_pdf_path=self.config.last_pdf_path,
        )

    def save_settings(self) -> None:
        self.config = self._collect_config()
        path = save_config(self.config)
        self._append_log(f"Settings saved to {path}")
        QMessageBox.information(self, "Saved", f"Settings saved to:\n{path}")

    def run_workflow(self) -> None:
        if self._thread is not None:
            QMessageBox.warning(self, "Busy", "The workflow is already running.")
            return

        self.config = self._collect_config()
        save_config(self.config)
        self.status_label.setText("Running")
        self.summary_label.setText("Workflow in progress...")
        self.log_output.clear()
        self._append_log("Starting workflow")

        self._worker = WorkflowWorker(self.config)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._cleanup_worker)
        self._worker.failed.connect(self._cleanup_worker)
        self._thread.start()

    def _on_progress(self, event: ProgressEvent) -> None:
        prefix = event.level.upper()
        self._append_log(f"[{prefix}] {event.message}")

    def _on_finished(self, result: WorkflowResult | None) -> None:
        self._result = result
        self.status_label.setText("Completed with manual steps" if result and result.warnings else "Completed")
        if result is None:
            self.summary_label.setText("Workflow completed without a result payload.")
            return

        self.summary_label.setText(
            f"Supply {result.supply.supply_id}: {len(result.supply.items)} items, {len(result.tasks)} marking tasks."
        )
        self.last_csv_label.setText(str(result.marks_csv_path))
        self.last_pdf_label.setText(str(result.pdf_path) if result.pdf_path else "-")
        self.config.last_marks_csv_path = str(result.marks_csv_path)
        self.config.last_pdf_path = str(result.pdf_path) if result.pdf_path else ""
        save_config(self.config)
        if result.warnings:
            self._append_log("Warnings:")
            for warning in result.warnings:
                self._append_log(f"[WARNING] {warning}")

    def _on_failed(self, details: str) -> None:
        self.status_label.setText("Failed")
        self.summary_label.setText("Workflow failed. See the log for details.")
        self._append_log(details)
        QMessageBox.critical(self, "Workflow failed", details)

    def _cleanup_worker(self, *_args) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
        self._worker = None

    def _append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def _pick_mapping_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose mapping CSV", str(Path.cwd()), "CSV Files (*.csv)")
        if path:
            self.mapping_path_input.setText(path)

    def _pick_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", str(Path.cwd()))
        if path:
            self.output_dir_input.setText(path)

    def _pick_profile_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose browser profile folder", str(Path.cwd()))
        if path:
            self.profile_dir_input.setText(path)

    def _pick_wb_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose WB draft JSON", str(Path.cwd()), "JSON Files (*.json)")
        if path:
            self.wb_json_input.setText(path)

