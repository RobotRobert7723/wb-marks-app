import unittest

from wb_marks_app.services.labels import GS, build_manual_labels, extract_mark_codes, parse_mark_code


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

    def test_build_manual_labels_uses_one_label_per_mark_code(self) -> None:
        text = "0104700092263449215/ExwqH/2ziur\\x1d91EE12\\x1d923rhVzA0Rg7nIB\n"

        labels = build_manual_labels(
            template="combined",
            item_name="Suit",
            vendor_code="nbf_gray_z",
            size="S",
            wb_barcode="2043467523239",
            mark_codes_text=text,
        )

        self.assertEqual(1, len(labels))
        self.assertEqual("combined", labels[0].template)
        self.assertEqual("04700092263449", labels[0].gtin)
        self.assertEqual("2043467523239", labels[0].wb_barcode)


if __name__ == "__main__":
    unittest.main()
