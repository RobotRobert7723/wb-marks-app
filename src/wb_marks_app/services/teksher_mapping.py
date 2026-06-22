from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wb_marks_app.server_models import TeksherMappingModel
from wb_marks_app.services.product_cards import ProductCardMappingRow, ProductCardTemplate


TEKSHER_FIELDS = (
    "teksher_size",
    "product_type",
    "gtin",
    "tnved",
    "country",
    "vendor_article",
    "color",
    "composition",
    "target_gender",
    "trademark",
)


class TeksherMappingService:
    def apply_latest(self, session: Session, user_id: str, product_card: ProductCardTemplate) -> ProductCardTemplate:
        version = self.latest_version(session, user_id, product_card.wb_article)
        if version == 0:
            return product_card

        saved_rows = session.execute(
            select(TeksherMappingModel)
            .where(TeksherMappingModel.user_id == user_id)
            .where(TeksherMappingModel.wb_article == product_card.wb_article)
            .where(TeksherMappingModel.version == version)
        ).scalars().all()
        by_key = {self._row_key(row.wb_barcode, row.wb_size): row for row in saved_rows}
        by_size = {self._normalize(row.wb_size): row for row in saved_rows if row.wb_size}

        rows: list[ProductCardMappingRow] = []
        for wb_row in product_card.rows:
            saved = by_key.get(self._row_key(wb_row.barcode, wb_row.wb_size)) or by_size.get(self._normalize(wb_row.wb_size))
            if saved is None:
                rows.append(wb_row)
                continue
            rows.append(
                replace(
                    wb_row,
                    teksher_size=saved.teksher_size,
                    product_type=saved.product_type,
                    gtin=saved.gtin,
                    tnved=saved.tnved,
                    country=saved.country,
                    vendor_article=saved.vendor_article,
                    color=saved.color,
                    composition=saved.composition,
                    target_gender=saved.target_gender,
                    trademark=saved.trademark,
                )
            )

        return replace(product_card, rows=rows, has_teksher_mapping=True, mapping_version=version)

    def save_version(
        self,
        session: Session,
        user_id: str,
        product_card: ProductCardTemplate,
        rows_payload: list[dict],
        source: str = "product_card",
    ) -> tuple[int, int]:
        wb_rows = {self._row_key(row.barcode, row.wb_size): row for row in product_card.rows}
        wb_rows_by_size = {self._normalize(row.wb_size): row for row in product_card.rows if row.wb_size}
        version = self.latest_version(session, user_id, product_card.wb_article) + 1
        created = 0

        for payload in rows_payload:
            if not self._has_teksher_values(payload):
                continue
            wb_barcode = self._text(payload.get("wb_barcode"))
            wb_size = self._text(payload.get("wb_size"))
            wb_row = wb_rows.get(self._row_key(wb_barcode, wb_size)) or wb_rows_by_size.get(self._normalize(wb_size))
            session.add(self._model_from_payload(user_id, product_card, wb_row, payload, version, source))
            created += 1

        if created == 0:
            raise ValueError("Нет данных мэппинга для сохранения.")
        session.flush()
        return version, created

    def latest_version(self, session: Session, user_id: str, wb_article: str) -> int:
        version = session.execute(
            select(func.max(TeksherMappingModel.version))
            .where(TeksherMappingModel.user_id == user_id)
            .where(TeksherMappingModel.wb_article == wb_article)
        ).scalar_one_or_none()
        return int(version or 0)

    def _model_from_payload(
        self,
        user_id: str,
        product_card: ProductCardTemplate,
        wb_row: ProductCardMappingRow | None,
        payload: dict,
        version: int,
        source: str,
    ) -> TeksherMappingModel:
        summary = product_card.wb_summary
        return TeksherMappingModel(
            user_id=user_id,
            version=version,
            source=source,
            wb_article=product_card.wb_article,
            wb_name=summary.name,
            wb_seller_category=summary.seller_category,
            wb_tnved=summary.tnved,
            wb_country=summary.country,
            wb_seller_article=summary.seller_article,
            wb_color=summary.color,
            wb_composition=summary.composition,
            wb_gender=summary.gender,
            wb_brand=summary.brand,
            wb_barcode=self._text(payload.get("wb_barcode")) or (wb_row.barcode if wb_row else ""),
            wb_size=self._text(payload.get("wb_size")) or (wb_row.wb_size if wb_row else ""),
            wb_ru_size=self._text(payload.get("wb_ru_size")) or (wb_row.ru_size if wb_row else ""),
            teksher_size=self._text(payload.get("teksher_size")),
            product_type=self._text(payload.get("product_type")),
            gtin=self._text(payload.get("gtin")),
            tnved=self._text(payload.get("tnved")),
            country=self._text(payload.get("country")),
            vendor_article=self._text(payload.get("vendor_article")),
            color=self._text(payload.get("color")),
            composition=self._text(payload.get("composition")),
            target_gender=self._text(payload.get("target_gender")),
            trademark=self._text(payload.get("trademark")),
        )

    def _has_teksher_values(self, payload: dict) -> bool:
        return any(self._text(payload.get(field)) for field in TEKSHER_FIELDS)

    def _row_key(self, barcode: str, size: str) -> str:
        return f"{self._normalize(barcode)}::{self._normalize(size)}"

    def _normalize(self, value: str) -> str:
        return self._text(value).casefold()

    def _text(self, value) -> str:
        return str(value or "").strip()
