from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wb_marks_app.config import load_config
from wb_marks_app.db import Base


_CONFIG = load_config()
SCHEMA = _CONFIG.database_schema or None
TEKSHER_SCHEMA = None if _CONFIG.database_url.startswith("sqlite") else "teksher"


def _fk(path: str) -> str:
    return f"{SCHEMA}.{path}" if SCHEMA else path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AppSettingsModel(Base):
    __tablename__ = "app_settings"
    __table_args__ = {"schema": SCHEMA} if SCHEMA else {}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey(_fk("users.id"), ondelete="SET NULL"), unique=True, index=True, nullable=True)
    wb_api_token: Mapped[str] = mapped_column(Text, default="")
    wb_api_base_url: Mapped[str] = mapped_column(String(255), default="https://supplies-api.wildberries.ru")
    teksher_username: Mapped[str] = mapped_column(String(255), default="")
    teksher_password: Mapped[str] = mapped_column(Text, default="")
    teksher_transgran_recipient_name: Mapped[str] = mapped_column(String(255), default="")
    teksher_transgran_recipient_inn: Mapped[str] = mapped_column(String(32), default="")
    teksher_transgran_recipient_kpp: Mapped[str] = mapped_column(String(32), default="")
    mapping_mode: Mapped[str] = mapped_column(String(32), default="size")
    mapping_payload: Mapped[str] = mapped_column(Text, default="{}")
    artifact_storage_dir: Mapped[str] = mapped_column(Text, default="")
    transgran_document_number_prefix: Mapped[str] = mapped_column(String(64), default="WB")
    step_timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    user: Mapped["UserModel"] = relationship(back_populates="settings")


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("login", name="uq_users_login"),
        {"schema": SCHEMA} if SCHEMA else {},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(255), index=True)
    login: Mapped[str] = mapped_column(String(64), index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    settings: Mapped["AppSettingsModel"] = relationship(back_populates="user", uselist=False)
    runs: Mapped[list["WorkflowRunModel"]] = relationship(back_populates="user")


class WorkflowRunModel(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = {"schema": SCHEMA} if SCHEMA else {}

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey(_fk("users.id"), ondelete="SET NULL"), index=True, nullable=True)
    draft_id: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    items: Mapped[list["WorkflowRunItemModel"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    user: Mapped["UserModel"] = relationship(back_populates="runs")


class WorkflowRunItemModel(Base):
    __tablename__ = "workflow_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "vendor_code", "size", name="uq_run_item_vendor_size"),
        {"schema": SCHEMA} if SCHEMA else {},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey(_fk("workflow_runs.id"), ondelete="CASCADE"), index=True)
    barcode: Mapped[str] = mapped_column(String(64), default="")
    vendor_code: Mapped[str] = mapped_column(String(255), index=True)
    size: Mapped[str] = mapped_column(String(64), index=True)
    gtin: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    wb_item_name: Mapped[str] = mapped_column(Text, default="")
    document_number: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    run: Mapped["WorkflowRunModel"] = relationship(back_populates="items")
    operations: Mapped[list["TeksherOperationModel"]] = relationship(
        back_populates="run_item",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    mark_codes: Mapped[list["MarkCodeModel"]] = relationship(
        back_populates="run_item",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    artifacts: Mapped[list["ArtifactModel"]] = relationship(
        back_populates="run_item",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TeksherOperationModel(Base):
    __tablename__ = "teksher_operations"
    __table_args__ = (
        UniqueConstraint("run_item_id", "operation_kind", name="uq_run_item_operation_kind"),
        {"schema": SCHEMA} if SCHEMA else {},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_item_id: Mapped[str] = mapped_column(ForeignKey(_fk("workflow_run_items.id"), ondelete="CASCADE"), index=True)
    operation_kind: Mapped[str] = mapped_column(String(32), index=True)
    external_operation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    end_at: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    run_item: Mapped["WorkflowRunItemModel"] = relationship(back_populates="operations")


class TeksherMappingModel(Base):
    __tablename__ = "mapping"
    __table_args__ = (
        Index("ix_teksher_mapping_user_article_version", "user_id", "wb_article", "version"),
        {"schema": TEKSHER_SCHEMA} if TEKSHER_SCHEMA else {},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(64), default="manual")

    wb_article: Mapped[str] = mapped_column(String(64), index=True)
    wb_name: Mapped[str] = mapped_column(Text, default="")
    wb_seller_category: Mapped[str] = mapped_column(Text, default="")
    wb_tnved: Mapped[str] = mapped_column(String(64), default="")
    wb_country: Mapped[str] = mapped_column(String(255), default="")
    wb_seller_article: Mapped[str] = mapped_column(String(255), index=True, default="")
    wb_color: Mapped[str] = mapped_column(String(255), default="")
    wb_composition: Mapped[str] = mapped_column(Text, default="")
    wb_gender: Mapped[str] = mapped_column(String(255), default="")
    wb_brand: Mapped[str] = mapped_column(String(255), default="")
    wb_barcode: Mapped[str] = mapped_column(String(64), default="")
    wb_size: Mapped[str] = mapped_column(String(64), default="")
    wb_ru_size: Mapped[str] = mapped_column(String(64), default="")

    teksher_size: Mapped[str] = mapped_column(String(255), default="")
    product_type: Mapped[str] = mapped_column(Text, default="")
    gtin: Mapped[str] = mapped_column(String(64), default="")
    tnved: Mapped[str] = mapped_column(String(64), default="")
    country: Mapped[str] = mapped_column(String(255), default="")
    vendor_article: Mapped[str] = mapped_column(String(255), default="")
    color: Mapped[str] = mapped_column(String(255), default="")
    composition: Mapped[str] = mapped_column(Text, default="")
    target_gender: Mapped[str] = mapped_column(String(255), default="")
    trademark: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class MarkCodeModel(Base):
    __tablename__ = "mark_codes"
    __table_args__ = (
        UniqueConstraint("run_item_id", "position", name="uq_mark_code_run_item_position"),
        {"schema": SCHEMA} if SCHEMA else {},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_item_id: Mapped[str] = mapped_column(ForeignKey(_fk("workflow_run_items.id"), ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    mark_code: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    run_item: Mapped["WorkflowRunItemModel"] = relationship(back_populates="mark_codes")


class ArtifactModel(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("run_item_id", "kind", name="uq_artifact_run_item_kind"),
        {"schema": SCHEMA} if SCHEMA else {},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_item_id: Mapped[str] = mapped_column(ForeignKey(_fk("workflow_run_items.id"), ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="csv")
    file_name: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    run_item: Mapped["WorkflowRunItemModel"] = relationship(back_populates="artifacts")


class PasswordResetTokenModel(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_password_reset_token_hash"),
        {"schema": SCHEMA} if SCHEMA else {},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey(_fk("users.id"), ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
