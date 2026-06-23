from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


WbMode = Literal["auto", "api", "browser"]
TaskStatus = Literal["pending", "issued", "failed"]


@dataclass(slots=True)
class AppConfig:
    app_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./wb_marks_app.db"
    database_schema: str = ""
    secret_key: str = ""
    artifact_storage_dir: str = ""
    mapping_csv_path: str = ""
    output_dir: str = ""
    browser_profile_dir: str = ""
    wb_mode: WbMode = "auto"
    pause_on_manual_step: bool = True
    step_timeout_seconds: int = 300
    wb_api_base_url: str = ""
    wb_content_api_base_url: str = "https://content-api.wildberries.ru"
    wb_api_token: str = ""
    wb_draft_source_file: str = ""
    wb_seller_url: str = "https://seller.wildberries.ru/"
    teksher_url: str = "https://label.teksher.kg/"
    teksher_api_token: str = ""
    teksher_refresh_token: str = ""
    teksher_username: str = ""
    teksher_password: str = ""
    teksher_extension: str = "lp"
    teksher_country_id: int = 242
    teksher_transgran_country_code: str = "RU"
    teksher_transgran_recipient_name: str = ""
    teksher_transgran_recipient_inn: str = ""
    teksher_transgran_recipient_kpp: str = ""
    supplier_name: str = ""
    production_address: str = ""
    transgran_document_number_prefix: str = "WB"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    password_reset_ttl_minutes: int = 60
    wbarcode_url: str = "https://wbarcode.ru/"
    last_marks_csv_path: str = ""
    last_pdf_path: str = ""

    def resolved_output_dir(self) -> Path:
        if self.output_dir:
            return Path(self.output_dir).expanduser()
        return Path.cwd() / "output"

    def resolved_mapping_path(self) -> Path | None:
        return Path(self.mapping_csv_path).expanduser() if self.mapping_csv_path else None

    def resolved_profile_dir(self) -> Path:
        if self.browser_profile_dir:
            return Path(self.browser_profile_dir).expanduser()
        return Path.cwd() / ".browser-profile"

    def resolved_artifact_storage_dir(self) -> Path:
        if self.artifact_storage_dir:
            return Path(self.artifact_storage_dir).expanduser()
        return self.resolved_output_dir() / "artifacts"


@dataclass(slots=True)
class SupplyItem:
    barcode: str
    name: str
    quantity: int
    supplier_article: str = ""
    size: str = ""
    nm_id: str = ""
    sku: str = ""
    draft_supply_id: str = ""


@dataclass(slots=True)
class DraftSupply:
    supply_id: str
    name: str
    status: str
    created_at: datetime
    items: list[SupplyItem] = field(default_factory=list)
    source: str = ""


@dataclass(slots=True)
class MarkingTask:
    task_id: str
    barcode: str
    gtin: str
    wb_item_name: str
    quantity_index: int
    supply_id: str
    supplier_article: str = ""
    size: str = ""
    nm_id: str = ""
    sku: str = ""
    status: TaskStatus = "pending"
    mark_code: str = ""
    serial_or_aux_fields: str = ""
    error: str = ""
    created_at: datetime | None = None


@dataclass(slots=True)
class WorkflowResult:
    supply: DraftSupply
    tasks: list[MarkingTask]
    marks_csv_path: Path
    pdf_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProgressEvent:
    level: Literal["info", "warning", "error"] = "info"
    message: str = ""
