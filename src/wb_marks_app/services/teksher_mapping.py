from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wb_marks_app.server_models import TeksherMappingModel
from wb_marks_app.services.product_cards import ProductCardMappingRow, ProductCardTemplate


TEKSHER_FIELDS = (
    "full_name",
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

TARGET_GENDER_MAP = {
    "мальчики": "МУЖСКОЙ",
    "девочки": "ЖЕНСКИЙ",
    "мужской": "МУЖСКОЙ",
    "женский": "ЖЕНСКИЙ",
    "детский": "УНИВЕРСАЛЬНЫЙ (УНИСЕКС)",
}

PRODUCT_TYPE_FULL_NAME_MAP = {
    "костюм спортивный": "Костюм спортивный",
    "костюмы спортивные": "Костюм спортивный",
}


class TeksherMappingService:
    def apply_latest(
        self,
        session: Session,
        user_id: str,
        product_card: ProductCardTemplate,
        teksher_rows_by_gtin: dict[str, dict] | None = None,
    ) -> ProductCardTemplate:
        version, saved_rows = self.latest_payload(session, user_id, product_card.wb_article)
        return self.apply_payload(product_card, saved_rows, version, teksher_rows_by_gtin)

    def latest_payload(
        self,
        session: Session,
        user_id: str,
        wb_article: str,
    ) -> tuple[int, list[dict]]:
        version = self.latest_version(session, user_id, wb_article)
        if version == 0:
            return 0, []

        saved_rows = session.execute(
            select(TeksherMappingModel)
            .where(TeksherMappingModel.user_id == user_id)
            .where(TeksherMappingModel.wb_article == wb_article)
            .where(TeksherMappingModel.version == version)
        ).scalars().all()
        return version, [self._payload_from_saved(row) for row in saved_rows]

    def apply_payload(
        self,
        product_card: ProductCardTemplate,
        saved_rows: list[dict],
        version: int,
        teksher_rows_by_gtin: dict[str, dict] | None = None,
    ) -> ProductCardTemplate:
        if version == 0 or not saved_rows:
            return replace(product_card, rows=[self._clear_teksher_fields(row) for row in product_card.rows])

        by_key = {self._row_key(row["wb_barcode"], row["wb_size"]): row for row in saved_rows}
        by_size = {self._normalize(row["wb_size"]): row for row in saved_rows if row.get("wb_size")}

        rows: list[ProductCardMappingRow] = []
        for wb_row in product_card.rows:
            saved = by_key.get(self._row_key(wb_row.barcode, wb_row.wb_size)) or by_size.get(self._normalize(wb_row.wb_size))
            if saved is None:
                rows.append(self._clear_teksher_fields(wb_row))
                continue
            fields = self._row_fields(saved, teksher_rows_by_gtin)
            rows.append(
                replace(
                    wb_row,
                    full_name=fields["full_name"],
                    teksher_size=fields["teksher_size"],
                    product_type=fields["product_type"],
                    gtin=fields["gtin"],
                    tnved=fields["tnved"],
                    country=fields["country"],
                    vendor_article=fields["vendor_article"],
                    color=fields["color"],
                    composition=fields["composition"],
                    target_gender=fields["target_gender"],
                    trademark=fields["trademark"],
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
        previous_version = self.latest_version(session, user_id, product_card.wb_article)
        version = previous_version + 1
        created = 0

        payload_by_key: dict[str, dict] = {}
        for payload in rows_payload:
            if not self._has_teksher_values(payload):
                continue
            wb_barcode = self._text(payload.get("wb_barcode"))
            wb_size = self._text(payload.get("wb_size"))
            wb_row = wb_rows.get(self._row_key(wb_barcode, wb_size)) or wb_rows_by_size.get(self._normalize(wb_size))
            key = self._row_key(wb_row.barcode, wb_row.wb_size) if wb_row else self._row_key(wb_barcode, wb_size)
            payload_by_key[key] = payload

        if not payload_by_key:
            raise ValueError("Нет данных мэппинга для сохранения.")

        previous_rows = self._version_rows(session, user_id, product_card.wb_article, previous_version)
        previous_by_key = {self._row_key(row.wb_barcode, row.wb_size): row for row in previous_rows}
        previous_by_size = {self._normalize(row.wb_size): row for row in previous_rows if row.wb_size}
        handled_keys: set[str] = set()

        for wb_row in product_card.rows:
            key = self._row_key(wb_row.barcode, wb_row.wb_size)
            payload = payload_by_key.get(key)
            if payload is not None:
                session.add(self._model_from_payload(user_id, product_card, wb_row, payload, version, source))
                handled_keys.add(key)
                created += 1
                continue

            saved = previous_by_key.get(key) or previous_by_size.get(self._normalize(wb_row.wb_size))
            if saved is None:
                continue
            session.add(self._model_from_saved(user_id, product_card, wb_row, saved, version, source))
            created += 1

        for key, payload in payload_by_key.items():
            if key in handled_keys:
                continue
            wb_barcode = self._text(payload.get("wb_barcode"))
            wb_size = self._text(payload.get("wb_size"))
            wb_row = wb_rows.get(self._row_key(wb_barcode, wb_size)) or wb_rows_by_size.get(self._normalize(wb_size))
            session.add(self._model_from_payload(user_id, product_card, wb_row, payload, version, source))
            created += 1

        session.flush()
        return version, created

    def latest_version(self, session: Session, user_id: str, wb_article: str) -> int:
        version = session.execute(
            select(func.max(TeksherMappingModel.version))
            .where(TeksherMappingModel.user_id == user_id)
            .where(TeksherMappingModel.wb_article == wb_article)
        ).scalar_one_or_none()
        return int(version or 0)

    def _version_rows(
        self,
        session: Session,
        user_id: str,
        wb_article: str,
        version: int,
    ) -> list[TeksherMappingModel]:
        if version == 0:
            return []
        return session.execute(
            select(TeksherMappingModel)
            .where(TeksherMappingModel.user_id == user_id)
            .where(TeksherMappingModel.wb_article == wb_article)
            .where(TeksherMappingModel.version == version)
        ).scalars().all()

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
            full_name=self._text(payload.get("full_name"))
            or self._full_name_from_product_type(payload.get("product_type")),
            teksher_size=self._text(payload.get("teksher_size")),
            product_type=self._text(payload.get("product_type")),
            gtin=self._text(payload.get("gtin")),
            tnved=self._text(payload.get("tnved")) or summary.tnved,
            country=self._text(payload.get("country")) or summary.country,
            vendor_article=self._text(payload.get("vendor_article")),
            color=self._text(payload.get("color")),
            composition=self._text(payload.get("composition")) or summary.composition,
            target_gender=self._text(payload.get("target_gender")) or self._target_gender_from_wb(summary.gender),
            trademark=self._text(payload.get("trademark")),
        )

    def _model_from_saved(
        self,
        user_id: str,
        product_card: ProductCardTemplate,
        wb_row: ProductCardMappingRow | None,
        saved: TeksherMappingModel,
        version: int,
        source: str,
    ) -> TeksherMappingModel:
        payload = {
            "wb_barcode": wb_row.barcode if wb_row else saved.wb_barcode,
            "wb_size": wb_row.wb_size if wb_row else saved.wb_size,
            "wb_ru_size": wb_row.ru_size if wb_row else saved.wb_ru_size,
            "full_name": saved.full_name or self._full_name_from_product_type(saved.product_type),
            "teksher_size": saved.teksher_size,
            "product_type": saved.product_type,
            "gtin": saved.gtin,
            "tnved": saved.tnved,
            "country": saved.country,
            "vendor_article": saved.vendor_article,
            "color": saved.color,
            "composition": saved.composition,
            "target_gender": saved.target_gender,
            "trademark": saved.trademark,
        }
        return self._model_from_payload(user_id, product_card, wb_row, payload, version, source)

    def _payload_from_saved(self, saved: TeksherMappingModel) -> dict:
        return {
            "wb_barcode": saved.wb_barcode,
            "wb_size": saved.wb_size,
            "wb_ru_size": saved.wb_ru_size,
            "full_name": saved.full_name or self._full_name_from_product_type(saved.product_type),
            "teksher_size": saved.teksher_size,
            "product_type": saved.product_type,
            "gtin": saved.gtin,
            "tnved": saved.tnved,
            "country": saved.country,
            "vendor_article": saved.vendor_article,
            "color": saved.color,
            "composition": saved.composition,
            "target_gender": saved.target_gender,
            "trademark": saved.trademark,
        }

    def _row_fields(self, saved: dict, teksher_rows_by_gtin: dict[str, dict] | None) -> dict[str, str]:
        saved_gtin = self._text(saved.get("gtin"))
        if teksher_rows_by_gtin is None:
            source = saved
        else:
            source = teksher_rows_by_gtin.get(saved_gtin, {})

        fields = {field: self._text(source.get(field)) for field in TEKSHER_FIELDS}
        fields["full_name"] = (
            fields["full_name"]
            or self._text(saved.get("full_name"))
            or self._full_name_from_product_type(saved.get("product_type"))
        )
        fields["gtin"] = fields["gtin"] or saved_gtin
        return fields

    def _has_teksher_values(self, payload: dict) -> bool:
        return any(self._text(payload.get(field)) for field in TEKSHER_FIELDS)

    def _clear_teksher_fields(self, row: ProductCardMappingRow) -> ProductCardMappingRow:
        return replace(
            row,
            full_name="",
            teksher_size="",
            product_type="",
            gtin="",
            tnved="",
            country="",
            vendor_article="",
            color="",
            composition="",
            target_gender="",
            trademark="",
        )

    def _row_key(self, barcode: str, size: str) -> str:
        return f"{self._normalize(barcode)}::{self._normalize(size)}"

    def _normalize(self, value: str) -> str:
        return self._text(value).casefold()

    def _target_gender_from_wb(self, value: str) -> str:
        text = self._text(value)
        return TARGET_GENDER_MAP.get(text.casefold(), text)

    def _full_name_from_product_type(self, value) -> str:
        text = self._text(value).lower()
        if not text:
            return ""
        mapped = PRODUCT_TYPE_FULL_NAME_MAP.get(text.casefold())
        if mapped:
            return mapped
        return text[:1].upper() + text[1:]

    def _text(self, value) -> str:
        return str(value or "").strip()
