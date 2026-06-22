from __future__ import annotations

from dataclasses import dataclass

from wb_marks_app.exceptions import ConfigurationError, IntegrationUnavailableError
from wb_marks_app.models import AppConfig


DEFAULT_CONTENT_API_BASE_URL = "https://content-api.wildberries.ru"


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
    has_teksher_mapping: bool = False
    mapping_version: int = 0


class ProductCardTemplateService:
    def __init__(self, session=None) -> None:
        self.session = session

    def build_template(self, wb_article: str, config: AppConfig | None = None) -> ProductCardTemplate:
        normalized_article = wb_article.strip()
        if config is None:
            return self._build_static_template(normalized_article)

        try:
            card = self.fetch_wb_card(normalized_article, config)
        except (ConfigurationError, IntegrationUnavailableError) as exc:
            return self._build_empty_template(normalized_article, str(exc))

        if card is None:
            return self._build_empty_template(normalized_article, f"Карточка WB {normalized_article} не найдена в WB API.")

        return self._build_from_wb_card(normalized_article, card)

    def fetch_wb_card(self, wb_article: str, config: AppConfig) -> dict | None:
        if not config.wb_api_token.strip():
            raise ConfigurationError("WB API token не задан в настройках.")

        session = self._session()
        try:
            response = session.post(
                self._content_api_base_url(config) + "/content/v2/get/cards/list",
                headers={
                    "Authorization": config.wb_api_token.strip(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "settings": {
                        "cursor": {"limit": 100},
                        "filter": {
                            "textSearch": wb_article,
                            "withPhoto": -1,
                        },
                    },
                },
                timeout=config.step_timeout_seconds,
            )
        except Exception as exc:
            raise IntegrationUnavailableError(f"WB Content API request failed: {exc}") from exc

        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise IntegrationUnavailableError("WB Content API returned invalid JSON.") from exc

        cards = payload.get("cards") if isinstance(payload, dict) else []
        if not isinstance(cards, list):
            cards = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            if str(card.get("nmID") or card.get("nmId") or card.get("nm_id") or "") == wb_article:
                return card
        for card in cards:
            if isinstance(card, dict):
                return card
        return None

    def _build_from_wb_card(self, wb_article: str, card: dict) -> ProductCardTemplate:
        summary = self._summary_from_wb_card(wb_article, card)
        rows = self._rows_from_wb_card(card, summary)
        return ProductCardTemplate(
            wb_article=wb_article,
            image_url=self._image_url(card),
            api_status=f"Данные WB загружены из Content API для артикула {wb_article}. Поля Текшер пока остаются UI-шаблоном.",
            wb_summary=summary,
            rows=rows,
        )

    def _summary_from_wb_card(self, wb_article: str, card: dict) -> WbProductSummary:
        return WbProductSummary(
            name=self._text(card.get("title") or card.get("name")),
            seller_category=self._text(card.get("subjectName") or card.get("object") or card.get("subject")),
            wb_article=str(card.get("nmID") or card.get("nmId") or card.get("nm_id") or wb_article),
            tnved=self._characteristic(card, "ТНВЭД", "ТН ВЭД", "Код ТНВЭД", "Код ТН ВЭД"),
            country=self._characteristic(card, "Страна производства", "Страна"),
            seller_article=self._text(card.get("vendorCode") or card.get("supplierArticle")),
            color=self._characteristic(card, "Цвет", "Основной цвет"),
            composition=self._characteristic(card, "Состав"),
            gender=self._characteristic(card, "Пол", "Целевой пол"),
            brand=self._text(card.get("brand")),
        )

    def _rows_from_wb_card(self, card: dict, summary: WbProductSummary) -> list[ProductCardMappingRow]:
        rows: list[ProductCardMappingRow] = []
        sizes = card.get("sizes") if isinstance(card, dict) else []
        if not isinstance(sizes, list):
            sizes = []

        for size in sizes:
            if not isinstance(size, dict):
                continue
            wb_size = self._text(size.get("techSize") or size.get("wbSize"))
            ru_size = self._text(size.get("wbSize") or size.get("techSize"))
            skus = size.get("skus") or []
            if not isinstance(skus, list):
                skus = [skus]
            for sku in skus or [""]:
                rows.append(self._row_from_wb(summary, self._text(sku), wb_size, ru_size))

        return rows

    def _row_from_wb(
        self,
        summary: WbProductSummary,
        barcode: str,
        wb_size: str,
        ru_size: str,
    ) -> ProductCardMappingRow:
        teksher_size = f"{wb_size} МЕЖДУНАРОДНЫЙ".strip() if wb_size else ""
        return ProductCardMappingRow(
            barcode=barcode,
            wb_size=wb_size,
            ru_size=ru_size,
            teksher_size=teksher_size,
            product_type=summary.seller_category.upper(),
            gtin="",
            tnved=summary.tnved,
            country=summary.country,
            vendor_article=summary.seller_article,
            color=summary.color.upper(),
            composition=summary.composition,
            target_gender=summary.gender.upper(),
            trademark=summary.brand,
            ready_to_mark=0,
            print_count=0,
            order_count=0,
        )

    def _build_empty_template(self, wb_article: str, status: str) -> ProductCardTemplate:
        return ProductCardTemplate(
            wb_article=wb_article,
            image_url="",
            api_status=status,
            wb_summary=WbProductSummary(
                name="",
                seller_category="",
                wb_article=wb_article,
                tnved="",
                country="",
                seller_article="",
                color="",
                composition="",
                gender="",
                brand="",
            ),
            rows=[],
        )

    def _build_static_template(self, normalized_article: str) -> ProductCardTemplate:
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

    def _content_api_base_url(self, config: AppConfig) -> str:
        return (config.wb_content_api_base_url or DEFAULT_CONTENT_API_BASE_URL).rstrip("/")

    def _characteristic(self, card: dict, *names: str) -> str:
        normalized_names = {name.casefold() for name in names}
        characteristics = card.get("characteristics") if isinstance(card, dict) else []
        if not isinstance(characteristics, list):
            return ""
        for item in characteristics:
            if not isinstance(item, dict):
                continue
            if self._text(item.get("name")).casefold() in normalized_names:
                return self._text(item.get("value"))
        return ""

    def _image_url(self, card: dict) -> str:
        photos = card.get("photos") if isinstance(card, dict) else []
        if not isinstance(photos, list) or not photos:
            return ""
        first = photos[0]
        if not isinstance(first, dict):
            return ""
        for key in ("big", "c516x688", "c246x328", "square", "tm"):
            value = self._text(first.get(key))
            if value:
                return value
        return ""

    def _text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(filter(None, (self._text(item) for item in value)))
        if isinstance(value, dict):
            return ", ".join(filter(None, (self._text(item) for item in value.values())))
        return str(value).strip()

    def _session(self):
        if self.session is not None:
            return self.session
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise IntegrationUnavailableError(
                "The requests package is not installed. Install project dependencies to use WB API mode."
            ) from exc
        self.session = requests.Session()
        return self.session

    def _raise_for_status(self, response) -> None:
        try:
            response.raise_for_status()
        except Exception as exc:
            body = getattr(response, "text", "").strip()
            status_code = getattr(response, "status_code", "unknown")
            if body:
                raise IntegrationUnavailableError(f"WB Content API request failed: {status_code} {body}") from exc
            raise IntegrationUnavailableError(f"WB Content API request failed: {status_code}") from exc

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
