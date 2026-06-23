from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from wb_marks_app.models import AppConfig


APP_DIR_NAME = "wb-marks-app"
CONFIG_FILE_NAME = "config.json"


def default_config_dir() -> Path:
    override = os.getenv("WB_MARKS_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.cwd() / "data"


def config_file_path() -> Path:
    override = os.getenv("WB_MARKS_CONFIG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return default_config_dir() / CONFIG_FILE_NAME


def load_config() -> AppConfig:
    config = _config_from_env()
    path = config_file_path()
    if not path.exists():
        return config

    payload = json.loads(path.read_text(encoding="utf-8"))
    return AppConfig(**{**asdict(config), **payload})


def save_config(config: AppConfig) -> Path:
    path = config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), ensure_ascii=True, indent=2), encoding="utf-8")
    return path


def _config_from_env() -> AppConfig:
    return AppConfig(
        app_base_url=os.getenv("APP_BASE_URL", "http://localhost:8000"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./wb_marks_app.db"),
        database_schema=os.getenv("DATABASE_SCHEMA", "").strip(),
        secret_key=os.getenv("SECRET_KEY", ""),
        artifact_storage_dir=os.getenv("ARTIFACT_STORAGE_DIR", ""),
        output_dir=os.getenv("OUTPUT_DIR", ""),
        wb_api_base_url=os.getenv("WB_API_BASE_URL", "").strip(),
        wb_content_api_base_url=os.getenv("WB_CONTENT_API_BASE_URL", "https://content-api.wildberries.ru").strip()
        or "https://content-api.wildberries.ru",
        wb_api_token=os.getenv("WB_API_TOKEN", "").strip(),
        teksher_url=os.getenv("TEKSHER_URL", "https://label.teksher.kg/").strip() or "https://label.teksher.kg/",
        teksher_api_token=os.getenv("TEKSHER_API_TOKEN", "").strip(),
        teksher_refresh_token=os.getenv("TEKSHER_REFRESH_TOKEN", "").strip(),
        teksher_username=os.getenv("TEKSHER_USERNAME", "").strip(),
        teksher_password=os.getenv("TEKSHER_PASSWORD", "").strip(),
        teksher_extension=os.getenv("TEKSHER_EXTENSION", "lp").strip() or "lp",
        teksher_country_id=int(os.getenv("TEKSHER_COUNTRY_ID", "242")),
        teksher_transgran_country_code=os.getenv("TEKSHER_TRANSGRAN_COUNTRY_CODE", "RU").strip() or "RU",
        teksher_transgran_recipient_name=os.getenv("TEKSHER_TRANSGRAN_RECIPIENT_NAME", "").strip(),
        teksher_transgran_recipient_inn=os.getenv("TEKSHER_TRANSGRAN_RECIPIENT_INN", "").strip(),
        teksher_transgran_recipient_kpp=os.getenv("TEKSHER_TRANSGRAN_RECIPIENT_KPP", "").strip(),
        supplier_name=os.getenv("SUPPLIER_NAME", "").strip(),
        production_address=os.getenv("PRODUCTION_ADDRESS", "").strip(),
        transgran_document_number_prefix=os.getenv("TRANSGRAN_DOCUMENT_NUMBER_PREFIX", "WB").strip() or "WB",
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        smtp_from_email=os.getenv("SMTP_FROM_EMAIL", "").strip(),
        smtp_use_tls=os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"},
        smtp_use_ssl=os.getenv("SMTP_USE_SSL", "false").strip().lower() in {"1", "true", "yes", "on"},
        password_reset_ttl_minutes=int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "60")),
        step_timeout_seconds=int(os.getenv("STEP_TIMEOUT_SECONDS", "300")),
    )
