import shutil
import unittest

from wb_marks_app.services.label_pdf import LabelPdfError, render_labels_pdf
from wb_marks_app.services.labels import GS, make_label_record


def _valid_mark_code() -> str:
    return "0104709055620664215YudSpca<mc9X" + GS + "91EE12" + GS + "92" + ("A" * 44)


def _pdf_page_count(pdf: bytes) -> int:
    return pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")


@unittest.skipIf(shutil.which("node") is None, "Node.js is required for bwip-js barcode rendering")
class LabelPdfTests(unittest.TestCase):
    def test_render_labels_pdf_returns_pdf_bytes(self) -> None:
        label = make_label_record(
            template="srad",
            item_name="Sport suit",
            vendor_code="cv_nk_blue_smr",
            size="38",
            color="blue",
            composition="Cotton 50%; polyester 50%",
            wb_barcode="2049271462634",
            mark_code=_valid_mark_code(),
            supplier_name="Supplier",
            production_date="01.06.2026",
            country_of_origin="Kyrgyzstan",
            brand="ErLine",
            supplier_address="Bishkek, Test street, 35",
        )

        pdf = render_labels_pdf([label], template="srad")

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)
        self.assertEqual(4, _pdf_page_count(pdf))

    def test_srad_template_renders_four_pages_per_mark_code(self) -> None:
        labels = [
            make_label_record(
                template="srad",
                item_name="Sport suit",
                vendor_code="cv_nk_blue_smr",
                size="38",
                color="blue",
                wb_barcode="2049271462634",
                mark_code=_valid_mark_code(),
            ),
            make_label_record(
                template="srad",
                item_name="Sport suit",
                vendor_code="cv_nk_blue_smr",
                size="40",
                color="blue",
                wb_barcode="2049271462634",
                mark_code=_valid_mark_code().replace("YudSpca<mc9X", "YudSpca<mc9Y"),
            ),
        ]

        pdf = render_labels_pdf(labels, template="srad")

        self.assertEqual(8, _pdf_page_count(pdf))

    def test_simple_and_medium_templates_render_expected_sets(self) -> None:
        label = make_label_record(
            template="simple",
            item_name="Sport suit",
            vendor_code="cv_nk_blue_smr",
            size="38",
            color="blue",
            wb_barcode="2049271462634",
            mark_code=_valid_mark_code(),
        )

        simple_pdf = render_labels_pdf([label], template="simple")
        medium_pdf = render_labels_pdf([label], template="medium")

        self.assertEqual("simple", label.template)
        self.assertEqual(2, _pdf_page_count(simple_pdf))
        self.assertEqual(3, _pdf_page_count(medium_pdf))

    def test_combined_template_alias_still_renders_srad_set(self) -> None:
        label = make_label_record(
            template="combined",
            item_name="Sport suit",
            wb_barcode="2049271462634",
            mark_code=_valid_mark_code(),
        )

        pdf = render_labels_pdf([label], template="combined")

        self.assertEqual("srad", label.template)
        self.assertEqual(4, _pdf_page_count(pdf))

    def test_render_labels_pdf_rejects_mark_code_without_verification_fields(self) -> None:
        label = make_label_record(
            template="srad",
            item_name="Sport suit",
            wb_barcode="2049271462634",
            mark_code="0104709055620664215YudSpca<mc9X",
        )

        with self.assertRaisesRegex(LabelPdfError, "AI 91/92 or AI 93"):
            render_labels_pdf([label], template="srad")


if __name__ == "__main__":
    unittest.main()
