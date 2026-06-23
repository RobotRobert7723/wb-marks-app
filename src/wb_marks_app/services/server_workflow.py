from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from wb_marks_app.db import session_scope
from wb_marks_app.models import AppConfig, DraftSupply, SupplyItem
from wb_marks_app.server_models import (
    ArtifactModel,
    MarkCodeModel,
    TeksherOperationModel,
    WorkflowRunItemModel,
    WorkflowRunModel,
)
from wb_marks_app.server_settings import get_or_create_settings, load_mapping_rules, settings_to_app_config
from wb_marks_app.services.browser import BrowserSessionManager
from wb_marks_app.services.teksher import TeksherService
from wb_marks_app.services.wb import WBService


class _NullBrowser:
    def open_url(self, url: str) -> None:
        return


@dataclass(slots=True)
class LaunchRequest:
    user_id: str
    draft_id: str
    source_url: str = ""


class WorkflowRunService:
    def __init__(self) -> None:
        self._threads: dict[str, threading.Thread] = {}

    def create_or_resume_run(self, launch: LaunchRequest) -> str:
        with session_scope() as session:
            existing = session.execute(
                select(WorkflowRunModel)
                .where(WorkflowRunModel.user_id == launch.user_id)
                .where(WorkflowRunModel.draft_id == launch.draft_id)
                .where(WorkflowRunModel.status.in_(["created", "running", "partial_failed"]))
                .order_by(WorkflowRunModel.created_at.desc())
            ).scalars().first()
            if existing is not None:
                run_id = existing.id
            else:
                run = WorkflowRunModel(
                    user_id=launch.user_id,
                    draft_id=launch.draft_id,
                    source_url=launch.source_url,
                    status="created",
                )
                session.add(run)
                session.flush()
                run_id = run.id
        self.start_background(run_id)
        return run_id

    def create_product_card_run(self, user_id: str, wb_article: str, product_card, rows: list[dict]) -> str:
        clean_rows = self._product_card_rows(product_card, rows)
        if not clean_rows:
            raise ValueError("Нет строк с количеством к заказу.")

        started_at = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        draft_id = f"CARD-{wb_article}-{started_at}"
        with session_scope() as session:
            run = WorkflowRunModel(
                user_id=user_id,
                draft_id=draft_id,
                source_url=f"/product-cards/{wb_article}",
                status="created",
                summary_json=json.dumps(
                    {
                        "source": "product_card",
                        "wb_article": wb_article,
                    },
                    ensure_ascii=True,
                ),
            )
            session.add(run)
            session.flush()
            for row in clean_rows:
                item = WorkflowRunItemModel(
                    run_id=run.id,
                    barcode=row["barcode"],
                    vendor_code=row["vendor_code"],
                    size=row["size"],
                    gtin=row["gtin"],
                    quantity=row["quantity"],
                    wb_item_name=row["name"],
                    status="pending",
                )
                session.add(item)
                session.flush()
                if not row["transgran"]:
                    session.add(
                        TeksherOperationModel(
                            run_item_id=item.id,
                            operation_kind="transgran",
                            external_operation_id="",
                            status="skipped",
                            payload_json=json.dumps({"enabled": False}, ensure_ascii=True),
                        )
                    )
            run_id = run.id

        self.start_background(run_id)
        return run_id

    def start_background(self, run_id: str) -> None:
        thread = self._threads.get(run_id)
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=self._run, args=(run_id,), daemon=True)
        self._threads[run_id] = thread
        thread.start()

    def retry_failed_items(self, run_id: str) -> None:
        with session_scope() as session:
            run = session.get(WorkflowRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            run.status = "running"
            run.error = ""
            for item in run.items:
                if item.status == "failed":
                    item.status = "pending"
                    item.error = ""
        self.start_background(run_id)

    def cancel_run(self, run_id: str) -> None:
        with session_scope() as session:
            run = session.get(WorkflowRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            run.status = "cancelled"

    def _run(self, run_id: str) -> None:
        try:
            self._run_internal(run_id)
        except Exception as exc:
            with session_scope() as session:
                run = session.get(WorkflowRunModel, run_id)
                if run is not None and run.status != "cancelled":
                    run.status = "partial_failed"
                    run.error = str(exc)

    def _run_internal(self, run_id: str) -> None:
        with session_scope() as session:
            run = session.get(WorkflowRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            if not run.user_id:
                raise ValueError(f"Run {run_id} has no user_id")
            settings = get_or_create_settings(session, run.user_id)
            config = settings_to_app_config(settings)
            mapping_rules = load_mapping_rules(settings)
            run.status = "running"
            if not run.items:
                supply = self._load_supply(run.draft_id, config)
                self._populate_run_items(session, run, supply, mapping_rules)
            else:
                supply = DraftSupply(
                    supply_id=run.draft_id,
                    name=f"WB draft {run.draft_id}",
                    status="draft",
                    created_at=datetime.now(),
                    items=[],
                    source="db",
                )

        for item_id in self._run_item_ids(run_id):
            with session_scope() as session:
                run = session.get(WorkflowRunModel, run_id)
                if run is None or run.status == "cancelled":
                    return
                item = session.get(WorkflowRunItemModel, item_id)
                if item is None or item.status == "completed":
                    continue
                if not run.user_id:
                    raise ValueError(f"Run {run_id} has no user_id")
                settings = get_or_create_settings(session, run.user_id)
                config = settings_to_app_config(settings)
                try:
                    self._process_item(session, run, item, config)
                except Exception:
                    continue

        with session_scope() as session:
            run = session.get(WorkflowRunModel, run_id)
            if run is None:
                return
            if run.status == "cancelled":
                return
            failed = sum(1 for item in run.items if item.status == "failed")
            completed = sum(1 for item in run.items if item.status == "completed")
            run.status = "partial_failed" if failed else "completed"
            try:
                summary = json.loads(run.summary_json or "{}")
            except json.JSONDecodeError:
                summary = {}
            summary.update(
                {
                    "completed_items": completed,
                    "failed_items": failed,
                    "total_items": len(run.items),
                }
            )
            run.summary_json = json.dumps(
                summary,
                ensure_ascii=True,
            )

    def _run_item_ids(self, run_id: str) -> list[str]:
        with session_scope() as session:
            return list(
                session.execute(
                    select(WorkflowRunItemModel.id)
                    .where(WorkflowRunItemModel.run_id == run_id)
                    .order_by(WorkflowRunItemModel.vendor_code, WorkflowRunItemModel.size)
                ).scalars()
            )

    def _load_supply(self, draft_id: str, config: AppConfig) -> DraftSupply:
        wb_service = WBService(BrowserSessionManager(Path.cwd() / ".noop"))
        return wb_service.get_draft_supply_by_id(draft_id, config)

    def _populate_run_items(self, session: Session, run: WorkflowRunModel, supply: DraftSupply, mapping_rules) -> None:
        missing: list[str] = []
        for supply_item in supply.items:
            gtin = mapping_rules.resolve_gtin(supply_item)
            if not gtin:
                missing.append(f"{supply_item.supplier_article}:{supply_item.size}")
                continue
            item = WorkflowRunItemModel(
                run_id=run.id,
                barcode=supply_item.barcode,
                vendor_code=supply_item.supplier_article,
                size=supply_item.size or supply_item.sku,
                gtin=gtin,
                quantity=supply_item.quantity,
                wb_item_name=supply_item.name,
                status="pending",
            )
            session.add(item)
        if missing:
            run.status = "partial_failed"
            run.error = "Missing GTIN mapping for: " + ", ".join(sorted(missing))
            raise ValueError(run.error)
        session.flush()

    def _process_item(self, session: Session, run: WorkflowRunModel, item: WorkflowRunItemModel, config: AppConfig) -> None:
        artifact_root = Path(config.resolved_artifact_storage_dir()) / f"run_{run.id}"
        artifact_root.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_root / f"{item.vendor_code}_{item.size}.csv"
        teksher = TeksherService(browser=_NullBrowser(), logger=lambda msg: None)

        try:
            item.error = ""
            item.status = "order_running"
            session.commit()
            order_op = self._get_or_create_operation(
                session,
                item,
                "order",
                lambda: teksher.create_mark_code_order(item.gtin, item.quantity, config),
            )
            item.document_number = self._document_number(run, item, config)
            order_op.status = order_op.status or "created"
            session.commit()
            order_details = teksher.wait_for_order_ready(order_op.external_operation_id, config)
            order_op.status = str(order_details.get("status") or "ACCEPTED")
            order_op.end_at = str(order_details.get("endAt") or "")
            item.status = "order_completed"
            session.commit()

            item.status = "marking_running"
            session.commit()
            marking_op = self._get_or_create_operation(
                session,
                item,
                "marking",
                lambda: teksher.create_marking_operation(order_op.external_operation_id, config),
            )
            marking_op.status = marking_op.status or "created"
            session.commit()
            marking_details = teksher.wait_for_operation(marking_op.external_operation_id, "ACCEPTED", config)
            marking_op.status = str(marking_details.get("status") or "ACCEPTED")
            marking_op.end_at = str(marking_details.get("endAt") or "")
            item.status = "marking_completed"
            session.commit()

            if not artifact_path.exists():
                teksher.save_operation_csv(marking_op.external_operation_id, artifact_path, config)
            self._upsert_artifact(session, item, artifact_path)
            self._store_mark_codes(session, item, teksher.read_operation_codes(marking_op.external_operation_id, config))
            item.status = "csv_saved"
            session.commit()

            transgran_op = self._find_operation(session, item, "transgran")
            if transgran_op is not None and transgran_op.status == "skipped":
                item.status = "completed"
                item.error = ""
                session.commit()
                return

            item.status = "transgran_running"
            session.commit()
            if transgran_op is None or not transgran_op.external_operation_id:
                doc_date, ship_date = teksher.resolve_transgran_dates(
                    marking_op.external_operation_id,
                    requested_document_date=datetime.now(),
                    requested_shipment_date=datetime.now(),
                    config=config,
                )
                transgran_id = teksher.run_transgran_cycle(
                    csv_path=artifact_path,
                    gtins=[item.gtin],
                    document_number=item.document_number,
                    document_date=doc_date,
                    shipment_date=ship_date,
                    config=config,
                )
                payload_json = json.dumps(
                    {
                        "document_number": item.document_number,
                        "document_date": doc_date.isoformat(timespec="seconds"),
                        "shipment_date": ship_date.isoformat(timespec="seconds"),
                    },
                    ensure_ascii=True,
                )
                if transgran_op is None:
                    transgran_op = TeksherOperationModel(
                        run_item_id=item.id,
                        operation_kind="transgran",
                        external_operation_id=transgran_id,
                        status="created",
                        payload_json=payload_json,
                    )
                    session.add(transgran_op)
                else:
                    transgran_op.external_operation_id = transgran_id
                    transgran_op.status = "created"
                    transgran_op.payload_json = payload_json
            item.status = "completed"
            item.error = ""
            session.commit()
        except Exception as exc:
            item.status = "failed"
            item.error = str(exc)
            run.status = "partial_failed"
            session.commit()
            raise

    def _get_or_create_operation(self, session: Session, item: WorkflowRunItemModel, kind: str, factory) -> TeksherOperationModel:
        operation = self._find_operation(session, item, kind)
        if operation is not None and operation.external_operation_id:
            return operation
        external_id = factory()
        operation = TeksherOperationModel(
            run_item_id=item.id,
            operation_kind=kind,
            external_operation_id=external_id,
            status="created",
        )
        session.add(operation)
        session.flush()
        return operation

    def _find_operation(self, session: Session, item: WorkflowRunItemModel, kind: str) -> TeksherOperationModel | None:
        return session.execute(
            select(TeksherOperationModel)
            .where(TeksherOperationModel.run_item_id == item.id)
            .where(TeksherOperationModel.operation_kind == kind)
        ).scalars().first()

    def _upsert_artifact(self, session: Session, item: WorkflowRunItemModel, artifact_path: Path) -> None:
        artifact = session.execute(
            select(ArtifactModel)
            .where(ArtifactModel.run_item_id == item.id)
            .where(ArtifactModel.kind == "csv")
        ).scalars().first()
        if artifact is None:
            artifact = ArtifactModel(
                run_item_id=item.id,
                kind="csv",
                file_name=artifact_path.name,
                file_path=str(artifact_path),
                size_bytes=artifact_path.stat().st_size,
            )
            session.add(artifact)
        else:
            artifact.file_name = artifact_path.name
            artifact.file_path = str(artifact_path)
            artifact.size_bytes = artifact_path.stat().st_size
        session.flush()

    def _store_mark_codes(self, session: Session, item: WorkflowRunItemModel, codes: list[str]) -> None:
        existing = session.execute(
            select(MarkCodeModel).where(MarkCodeModel.run_item_id == item.id)
        ).scalars().all()
        if existing:
            return
        for index, code in enumerate(codes, start=1):
            session.add(MarkCodeModel(run_item_id=item.id, position=index, mark_code=code))
        session.flush()

    def _document_number(self, run: WorkflowRunModel, item: WorkflowRunItemModel, config: AppConfig) -> str:
        prefix = config.transgran_document_number_prefix or "WB"
        return f"{prefix}-{run.draft_id}-{item.vendor_code}_{item.size}"

    def _product_card_rows(self, product_card, rows: list[dict]) -> list[dict]:
        product_rows_by_size = {self._row_key(row.wb_size): row for row in product_card.rows}
        result: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            size = str(row.get("size") or row.get("wb_size") or "").strip()
            gtin = str(row.get("gtin") or "").strip()
            try:
                quantity = int(row.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0
            if not size or not gtin or quantity <= 0:
                continue
            product_row = product_rows_by_size.get(self._row_key(size))
            if product_row is None:
                continue
            result.append(
                {
                    "size": size,
                    "gtin": gtin,
                    "quantity": quantity,
                    "barcode": product_row.barcode,
                    "vendor_code": product_card.wb_summary.seller_article,
                    "name": product_card.wb_summary.name,
                    "transgran": bool(row.get("transgran", True)),
                }
            )
        return result

    def _row_key(self, value: str) -> str:
        return str(value or "").strip().casefold()
