import unittest

from wb_marks_app.services.labels import (
    GS,
    build_manual_labels,
    extract_gs1_mark_codes,
    extract_mark_codes,
    extract_teksher_csv_mark_codes,
    parse_mark_code,
    validate_mark_code_for_chestny_znak_light_industry,
    validate_mark_code_for_datamatrix,
    validate_wb_barcode_for_code128,
)


class LabelServiceTests(unittest.TestCase):
    def test_parse_mark_code_preserves_gs1_fields(self) -> None:
        code = (
            "0104700092263449215l(fu2P(Il>rT"
            + GS
            + "91EE12"
            + GS
            + "92ocU54hRCudu420kDv2SgWlmaqBiQ1EX8tqH94ZdpnvQ="
        )

        parsed = parse_mark_code(code)

        self.assertTrue(parsed.valid)
        self.assertEqual("04700092263449", parsed.gtin)
        self.assertEqual("5l(fu2P(Il>rT", parsed.serial)
        self.assertEqual("EE12", parsed.check_key)
        self.assertEqual("ocU54hRCudu420kDv2SgWlmaqBiQ1EX8tqH94ZdpnvQ=", parsed.crypto_tail)

    def test_extract_mark_codes_reads_csv_escaped_quote(self) -> None:
        text = '"010470009226344921""5MQM_dofDhG\\u001d91EE12\\u001d92CU2gRGE80Z9="'

        codes = extract_mark_codes(text)

        self.assertEqual(1, len(codes))
        self.assertEqual('010470009226344921"5MQM_dofDhG', codes[0].split(GS)[0])

    def test_extract_gs1_mark_codes_finds_codes_in_csv_columns(self) -> None:
        code = "0104709055620664215YudSpca<mc9X" + GS + "91EE12" + GS + "92" + ("A" * 44)
        text = f"status;marking_code\naccepted;{code}\nignored;not-a-gs1-code\n"

        codes = extract_gs1_mark_codes(text)

        self.assertEqual([code], codes)

    def test_extract_teksher_csv_mark_codes_decodes_csv_escaped_quote(self) -> None:
        text = '"0104709055620688215hO*7?*I3E""C*\\u001d91EE12\\u001d92AehsMP1ZyziS/MQd+w3uRiJuU7Bgqzca2AKgPL+wpCI="'

        codes = extract_teksher_csv_mark_codes(text)
        parsed = parse_mark_code(codes[0])

        self.assertEqual(1, len(codes))
        self.assertTrue(parsed.valid)
        self.assertEqual('5hO*7?*I3E"C*', parsed.serial)
        self.assertEqual(13, len(parsed.serial))

    def test_build_manual_labels_uses_one_label_per_mark_code(self) -> None:
        text = "0104700092263449215/ExwqH/2ziur\\x1d91EE12\\x1d923rhVzA0Rg7nIB\n"

        labels = build_manual_labels(
            template="srad",
            item_name="Suit",
            vendor_code="nbf_gray_z",
            size="S",
            wb_barcode="2043467523239",
            mark_codes_text=text,
        )

        self.assertEqual(1, len(labels))
        self.assertEqual("srad", labels[0].template)
        self.assertEqual("04700092263449", labels[0].gtin)
        self.assertEqual("2043467523239", labels[0].wb_barcode)

        legacy_labels = build_manual_labels(
            template="combined",
            item_name="Suit",
            mark_codes_text=text,
        )
        self.assertEqual("srad", legacy_labels[0].template)

    def test_parse_mark_code_rejects_invalid_gtin_check_digit(self) -> None:
        parsed = parse_mark_code("0104700092263448215l(fu2P(Il>rT")

        self.assertFalse(parsed.valid)
        self.assertEqual("AI 01 GTIN check digit is invalid", parsed.error)

    def test_validate_mark_code_for_datamatrix_requires_verification_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "AI 91/92 or AI 93"):
            validate_mark_code_for_datamatrix("0104700092263449215l(fu2P(Il>rT")

    def test_validate_mark_code_for_chestny_znak_light_industry_rejects_wrong_serial_length(self) -> None:
        code = "0104709055620688215hO*7?*I3E\"\"C*" + GS + "91EE12" + GS + "92" + ("A" * 44)

        with self.assertRaisesRegex(ValueError, "AI 21 serial must contain 13 characters"):
            validate_mark_code_for_chestny_znak_light_industry(code)

    def test_validate_wb_barcode_for_code128_rejects_control_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "printable ASCII"):
            validate_wb_barcode_for_code128("204346\n7523239")


if __name__ == "__main__":
    unittest.main()
