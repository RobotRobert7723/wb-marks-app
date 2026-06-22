from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from wb_marks_app.db import Base
from wb_marks_app.services.product_cards import ProductCardMappingRow, ProductCardTemplate, WbProductSummary
from wb_marks_app.services.teksher_mapping import TeksherMappingService


def test_apply_latest_clears_green_fields_when_mapping_is_empty() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    service = TeksherMappingService()

    with Session(engine) as session:
        mapped_card = service.apply_latest(session, "user-1", _product_card(with_template_values=True))

    assert mapped_card.has_teksher_mapping is False
    assert mapped_card.mapping_version == 0
    assert mapped_card.rows[0].barcode == "2049271462689"
    assert mapped_card.rows[0].wb_size == "38"
    assert mapped_card.rows[0].teksher_size == ""
    assert mapped_card.rows[0].product_type == ""
    assert mapped_card.rows[0].gtin == ""
    assert mapped_card.rows[0].vendor_article == ""
    assert mapped_card.rows[0].color == ""
    assert mapped_card.rows[0].trademark == ""


def test_save_version_and_apply_latest_mapping() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    service = TeksherMappingService()
    product_card = _product_card()

    with Session(engine) as session:
        version, created = service.save_version(
            session,
            "user-1",
            product_card,
            [
                {
                    "wb_barcode": "2049271462689",
                    "wb_size": "38",
                    "wb_ru_size": "134",
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "04709055620626",
                    "vendor_article": "cv_nk_white_smr",
                    "color": "БЕЛЫЙ",
                    "trademark": "ErLine",
                }
            ],
            source="gtin_excel",
        )
        session.commit()

    assert version == 1
    assert created == 1

    with Session(engine) as session:
        mapped_card = service.apply_latest(session, "user-1", _product_card())

    assert mapped_card.has_teksher_mapping is True
    assert mapped_card.mapping_version == 1
    assert mapped_card.rows[0].gtin == "04709055620626"
    assert mapped_card.rows[0].product_type == "КОСТЮМ СПОРТИВНЫЙ"
    assert mapped_card.rows[0].color == "БЕЛЫЙ"


def test_save_version_fills_missing_values_from_wb() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    service = TeksherMappingService()
    product_card = _product_card(gender="мальчики")

    with Session(engine) as session:
        service.save_version(
            session,
            "user-1",
            product_card,
            [
                {
                    "wb_barcode": "2049271462689",
                    "wb_size": "38",
                    "wb_ru_size": "134",
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "04709055620626",
                    "vendor_article": "cv_nk_white_smr",
                    "color": "БЕЛЫЙ",
                    "trademark": "ErLine",
                }
            ],
            source="gtin_excel",
        )
        session.commit()

    with Session(engine) as session:
        mapped_card = service.apply_latest(session, "user-1", _product_card(gender="мальчики"))

    row = mapped_card.rows[0]
    assert row.tnved == "6112120000"
    assert row.country == "KG"
    assert row.composition == "polyester 100%"
    assert row.target_gender == "МУЖСКОЙ"


def test_partial_update_preserves_previous_size_rows() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    service = TeksherMappingService()
    product_card = _product_card_with_sizes(["38", "40"])

    with Session(engine) as session:
        version, created = service.save_version(
            session,
            "user-1",
            product_card,
            [
                {
                    "wb_barcode": "2049271462689",
                    "wb_size": "38",
                    "wb_ru_size": "134",
                    "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "GTIN-38-OLD",
                    "vendor_article": "cv_nk_white_smr",
                    "color": "БЕЛЫЙ",
                    "trademark": "ErLine",
                },
                {
                    "wb_barcode": "2049271462672",
                    "wb_size": "40",
                    "wb_ru_size": "140",
                    "teksher_size": "40 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ",
                    "gtin": "GTIN-40-OLD",
                    "vendor_article": "cv_nk_white_smr",
                    "color": "БЕЛЫЙ",
                    "trademark": "ErLine",
                },
            ],
            source="gtin_excel",
        )
        session.commit()

    assert version == 1
    assert created == 2

    with Session(engine) as session:
        version, created = service.save_version(
            session,
            "user-1",
            product_card,
            [
                {
                    "wb_barcode": "2049271462672",
                    "wb_size": "40",
                    "wb_ru_size": "140",
                    "teksher_size": "40 МЕЖДУНАРОДНЫЙ",
                    "product_type": "КОСТЮМ СПОРТИВНЫЙ ОБНОВЛЕННЫЙ",
                    "gtin": "GTIN-40-NEW",
                    "vendor_article": "cv_nk_white_smr",
                    "color": "БЕЛЫЙ",
                    "trademark": "ErLine",
                }
            ],
            source="gtin_excel",
        )
        session.commit()

    assert version == 2
    assert created == 2

    with Session(engine) as session:
        mapped_card = service.apply_latest(session, "user-1", _product_card_with_sizes(["38", "40"]))

    rows = {row.wb_size: row for row in mapped_card.rows}
    assert mapped_card.mapping_version == 2
    assert rows["38"].gtin == "GTIN-38-OLD"
    assert rows["38"].product_type == "КОСТЮМ СПОРТИВНЫЙ"
    assert rows["40"].gtin == "GTIN-40-NEW"
    assert rows["40"].product_type == "КОСТЮМ СПОРТИВНЫЙ ОБНОВЛЕННЫЙ"


def _product_card(with_template_values: bool = False, gender: str = "boys") -> ProductCardTemplate:
    values = {
        "teksher_size": "38 МЕЖДУНАРОДНЫЙ",
        "product_type": "КОСТЮМ СПОРТИВНЫЙ",
        "gtin": "4709055620220",
        "tnved": "6209200000",
        "country": "Кыргызстан",
        "vendor_article": "Арт.777-erl_22",
        "color": "БЕЛЫЙ",
        "composition": "полиэстер 100%",
        "target_gender": "УНИВЕРСАЛЬНЫЙ (УНИСЕКС)",
        "trademark": "ErLine",
    } if with_template_values else {}
    return ProductCardTemplate(
        wb_article="847012873",
        image_url="",
        api_status="",
        wb_summary=WbProductSummary(
            name="Sport suit",
            seller_category="Sport suits",
            wb_article="847012873",
            tnved="6112120000",
            country="KG",
            seller_article="cv_nk_white_smr",
            color="white",
            composition="polyester 100%",
            gender=gender,
            brand="ErLine",
        ),
        rows=[
            ProductCardMappingRow(
                barcode="2049271462689",
                wb_size="38",
                ru_size="134",
                teksher_size=values.get("teksher_size", ""),
                product_type=values.get("product_type", ""),
                gtin=values.get("gtin", ""),
                tnved=values.get("tnved", ""),
                country=values.get("country", ""),
                vendor_article=values.get("vendor_article", ""),
                color=values.get("color", ""),
                composition=values.get("composition", ""),
                target_gender=values.get("target_gender", ""),
                trademark=values.get("trademark", ""),
                ready_to_mark=0,
                print_count=0,
                order_count=0,
            )
        ],
    )


def _product_card_with_sizes(sizes: list[str]) -> ProductCardTemplate:
    barcodes = {"38": "2049271462689", "40": "2049271462672"}
    ru_sizes = {"38": "134", "40": "140"}
    product_card = _product_card()
    return ProductCardTemplate(
        wb_article=product_card.wb_article,
        image_url=product_card.image_url,
        api_status=product_card.api_status,
        wb_summary=product_card.wb_summary,
        rows=[
            ProductCardMappingRow(
                barcode=barcodes[size],
                wb_size=size,
                ru_size=ru_sizes[size],
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
                ready_to_mark=0,
                print_count=0,
                order_count=0,
            )
            for size in sizes
        ],
    )
