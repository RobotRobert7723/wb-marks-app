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


def _product_card(with_template_values: bool = False) -> ProductCardTemplate:
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
            gender="boys",
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
