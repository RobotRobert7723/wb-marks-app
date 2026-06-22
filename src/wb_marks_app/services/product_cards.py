from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WbProductSummary:
    name: str
    seller_category: str
    wb_article: str
    tnved: str
    country: str
    seller_article: str
    color: str
    composition: str
    gender: str
    brand: str


@dataclass(frozen=True, slots=True)
class ProductCardMappingRow:
    barcode: str
    wb_size: str
    ru_size: str
    teksher_size: str
    product_type: str
    gtin: str
    tnved: str
    country: str
    vendor_article: str
    color: str
    composition: str
    target_gender: str
    trademark: str
    ready_to_mark: int
    print_count: int
    order_count: int


@dataclass(frozen=True, slots=True)
class ProductCardTemplate:
    wb_article: str
    image_url: str
    api_status: str
    wb_summary: WbProductSummary
    rows: list[ProductCardMappingRow]


class ProductCardTemplateService:
    def build_template(self, wb_article: str) -> ProductCardTemplate:
        normalized_article = wb_article.strip()
        return ProductCardTemplate(
            wb_article=normalized_article,
            image_url="",
            api_status="Ожидает загрузки из WB API и плагина",
            wb_summary=WbProductSummary(
                name="Спортивный костюм Nike на молнии с капюшоном",
                seller_category="Костюмы спортивные",
                wb_article=normalized_article,
                tnved="6112120000",
                country="Кыргызстан",
                seller_article="cv_nk_white_smr",
                color="белый",
                composition="полиэстер 100%",
                gender="мальчики",
                brand="ErLine",
            ),
            rows=[
                self._row("2049271462689", "38", "134", 23),
                self._row("2049271462672", "40", "140", 0),
                self._row("2049271462696", "42", "146", 23),
                self._row("2049271462702", "44", "152", 23),
            ],
        )

    def _row(self, barcode: str, wb_size: str, ru_size: str, ready_to_mark: int) -> ProductCardMappingRow:
        return ProductCardMappingRow(
            barcode=barcode,
            wb_size=wb_size,
            ru_size=ru_size,
            teksher_size=f"{wb_size} МЕЖДУНАРОДНЫЙ",
            product_type="КОСТЮМ СПОРТИВНЫЙ",
            gtin="4709055620220",
            tnved="6209200000",
            country="Кыргызстан",
            vendor_article="Арт.777-erl_22",
            color="БЕЛЫЙ",
            composition="полиэстер 100%",
            target_gender="УНИВЕРСАЛЬНЫЙ (УНИСЕКС)",
            trademark="ErLine",
            ready_to_mark=ready_to_mark,
            print_count=0,
            order_count=0,
        )
