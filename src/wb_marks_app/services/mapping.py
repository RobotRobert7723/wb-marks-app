from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from wb_marks_app.exceptions import ConfigurationError, MappingValidationError
from wb_marks_app.models import MarkingTask, SupplyItem


class MappingService:
    REQUIRED_COLUMNS = {"barcode", "gtin"}

    def load_mapping(self, csv_path: Path) -> dict[str, str]:
        if not csv_path.exists():
            raise ConfigurationError(f"Mapping CSV was not found: {csv_path}")

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise MappingValidationError("Mapping CSV is empty.")

            columns = {name.strip() for name in reader.fieldnames}
            if not self.REQUIRED_COLUMNS.issubset(columns):
                raise MappingValidationError(
                    "Mapping CSV must contain the columns: barcode,gtin"
                )

            rows: list[dict[str, str]] = []
            for row in reader:
                normalized = {str(k).strip(): str(v).strip() for k, v in row.items() if k}
                if not any(normalized.values()):
                    continue
                rows.append(normalized)

        barcodes = [row.get("barcode", "") for row in rows]
        duplicates = [barcode for barcode, count in Counter(barcodes).items() if barcode and count > 1]
        if duplicates:
            raise MappingValidationError(
                f"Duplicate barcode entries were found in mapping CSV: {', '.join(sorted(duplicates))}"
            )

        missing_gtins = [row.get("barcode", "") for row in rows if not row.get("gtin", "")]
        if missing_gtins:
            raise MappingValidationError(
                f"Some mapping rows have empty GTIN values: {', '.join(sorted(filter(None, missing_gtins)))}"
            )

        return {row["barcode"]: row["gtin"] for row in rows}

    def build_tasks(self, items: list[SupplyItem], csv_path: Path) -> list[MarkingTask]:
        mapping = self.load_mapping(csv_path)
        missing_items = sorted({item.barcode for item in items if item.barcode not in mapping})
        if missing_items:
            raise MappingValidationError(
                "Some supply items do not have GTIN mapping: " + ", ".join(missing_items)
            )

        tasks: list[MarkingTask] = []
        created_at = datetime.now(timezone.utc)
        for item in items:
            gtin = mapping[item.barcode]
            for index in range(1, item.quantity + 1):
                task_id = f"{item.draft_supply_id}:{item.barcode}:{index}"
                tasks.append(
                    MarkingTask(
                        task_id=task_id,
                        barcode=item.barcode,
                        gtin=gtin,
                        wb_item_name=item.name,
                        quantity_index=index,
                        supply_id=item.draft_supply_id,
                        supplier_article=item.supplier_article,
                        nm_id=item.nm_id,
                        sku=item.sku,
                        created_at=created_at,
                    )
                )
        return tasks


class ResultCsvService:
    HEADER = [
        "barcode",
        "gtin",
        "wb_item_name",
        "supply_id",
        "mark_code",
        "serial_or_aux_fields",
        "created_at",
        "status",
        "error",
    ]

    def write(self, tasks: list[MarkingTask], output_dir: Path, supply_id: str) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = output_dir / f"wb_supply_{supply_id}_{timestamp}_marks.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.HEADER)
            writer.writeheader()
            for task in tasks:
                writer.writerow(
                    {
                        "barcode": task.barcode,
                        "gtin": task.gtin,
                        "wb_item_name": task.wb_item_name,
                        "supply_id": task.supply_id,
                        "mark_code": task.mark_code,
                        "serial_or_aux_fields": task.serial_or_aux_fields,
                        "created_at": task.created_at.isoformat() if task.created_at else "",
                        "status": task.status,
                        "error": task.error,
                    }
                )
        return csv_path

