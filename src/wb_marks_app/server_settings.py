from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wb_marks_app.config import load_config
from wb_marks_app.models import AppConfig, SupplyItem
from wb_marks_app.server_models import AppSettingsModel


@dataclass(slots=True)
class MappingRuleSet:
    mode: str
    payload: dict

    def resolve_gtin(self, item: SupplyItem) -> str:
        barcode_map = self.payload.get("barcode_to_gtin") or {}
        if item.barcode and item.barcode in barcode_map:
            return str(barcode_map[item.barcode]).strip()

        vendor_size_map = self.payload.get("vendor_size_to_gtin") or {}
        vendor_key = f"{item.supplier_article}:{item.size}".strip(":")
        if vendor_key and vendor_key in vendor_size_map:
            return str(vendor_size_map[vendor_key]).strip()

        size_map = self.payload.get("size_to_gtin") or {}
        if item.size and item.size in size_map:
            return str(size_map[item.size]).strip()
        return ""


def get_or_create_settings(session: Session, user_id: str) -> AppSettingsModel:
    settings = session.query(AppSettingsModel).filter(AppSettingsModel.user_id == user_id).first()
    if settings is not None:
        return settings

    base = load_config()
    next_id = session.execute(select(func.coalesce(func.max(AppSettingsModel.id), 0) + 1)).scalar_one()
    settings = AppSettingsModel(
        id=int(next_id),
        user_id=user_id,
        wb_api_token=base.wb_api_token,
        wb_api_base_url=base.wb_api_base_url or "https://supplies-api.wildberries.ru",
        teksher_username=base.teksher_username,
        teksher_password=base.teksher_password,
        teksher_transgran_recipient_name=base.teksher_transgran_recipient_name,
        teksher_transgran_recipient_inn=base.teksher_transgran_recipient_inn,
        teksher_transgran_recipient_kpp=base.teksher_transgran_recipient_kpp,
        supplier_name=base.supplier_name,
        production_address=base.production_address,
        artifact_storage_dir=str(base.resolved_artifact_storage_dir()),
        transgran_document_number_prefix=base.transgran_document_number_prefix,
        step_timeout_seconds=base.step_timeout_seconds,
        mapping_mode="size",
        mapping_payload=json.dumps({"size_to_gtin": {}}, ensure_ascii=True),
    )
    session.add(settings)
    session.flush()
    return settings


def update_settings(session: Session, user_id: str, payload: dict) -> AppSettingsModel:
    settings = get_or_create_settings(session, user_id)
    for field in (
        "wb_api_token",
        "wb_api_base_url",
        "teksher_username",
        "teksher_password",
        "teksher_transgran_recipient_name",
        "teksher_transgran_recipient_inn",
        "teksher_transgran_recipient_kpp",
        "supplier_name",
        "production_address",
        "mapping_mode",
        "mapping_payload",
        "artifact_storage_dir",
        "transgran_document_number_prefix",
        "step_timeout_seconds",
    ):
        if field in payload:
            setattr(settings, field, payload[field])
    session.add(settings)
    session.flush()
    return settings


def settings_to_app_config(settings: AppSettingsModel) -> AppConfig:
    base = load_config()
    merged = asdict(base)
    merged.update(
        {
            "wb_api_token": settings.wb_api_token,
            "wb_api_base_url": settings.wb_api_base_url,
            "teksher_username": settings.teksher_username,
            "teksher_password": settings.teksher_password,
            "teksher_transgran_recipient_name": settings.teksher_transgran_recipient_name,
            "teksher_transgran_recipient_inn": settings.teksher_transgran_recipient_inn,
            "teksher_transgran_recipient_kpp": settings.teksher_transgran_recipient_kpp,
            "supplier_name": settings.supplier_name,
            "production_address": settings.production_address,
            "artifact_storage_dir": settings.artifact_storage_dir,
            "transgran_document_number_prefix": settings.transgran_document_number_prefix,
            "step_timeout_seconds": settings.step_timeout_seconds,
        }
    )
    return AppConfig(**merged)


def load_mapping_rules(settings: AppSettingsModel) -> MappingRuleSet:
    try:
        payload = json.loads(settings.mapping_payload or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return MappingRuleSet(mode=settings.mapping_mode or "size", payload=payload)


def settings_public_dict(settings: AppSettingsModel) -> dict:
    return {
        "wb_api_base_url": settings.wb_api_base_url,
        "has_wb_api_token": bool(settings.wb_api_token),
        "teksher_username": settings.teksher_username,
        "has_teksher_password": bool(settings.teksher_password),
        "teksher_transgran_recipient_name": settings.teksher_transgran_recipient_name,
        "teksher_transgran_recipient_inn": settings.teksher_transgran_recipient_inn,
        "teksher_transgran_recipient_kpp": settings.teksher_transgran_recipient_kpp,
        "supplier_name": settings.supplier_name,
        "production_address": settings.production_address,
        "mapping_mode": settings.mapping_mode,
        "mapping_payload": settings.mapping_payload,
        "artifact_storage_dir": settings.artifact_storage_dir,
        "transgran_document_number_prefix": settings.transgran_document_number_prefix,
        "step_timeout_seconds": settings.step_timeout_seconds,
    }
