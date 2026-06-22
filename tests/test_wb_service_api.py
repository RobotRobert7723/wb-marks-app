import unittest

from wb_marks_app.models import AppConfig
from wb_marks_app.services.wb import WBService


class FakeBrowser:
    def open_url(self, url: str) -> None:
        return


class FakeResponse:
    def __init__(self, status_code: int, json_data, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or str(json_data)

    def json(self):
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text)


class FakeSession:
    def post(self, url, headers=None, json=None, timeout=None):
        return FakeResponse(
            200,
            [
                {
                    "preorderID": 50680482,
                    "statusID": 1,
                    "createDate": "2026-04-24T17:42:49+03:00",
                }
            ],
        )

    def get(self, url, headers=None, timeout=None):
        self.last_get = url
        return FakeResponse(
            200,
            [
                {
                    "barcode": "2043467498261",
                    "vendorCode": "nbf_black_z",
                    "techSize": "S",
                    "subjectName": "Костюм спортивный",
                    "quantity": 21,
                    "nmID": 123,
                }
            ],
        )


class WBServiceApiTests(unittest.TestCase):
    def test_get_draft_supply_by_id_uses_real_goods_contract(self) -> None:
        service = WBService(FakeBrowser(), session=FakeSession())
        config = AppConfig(
            wb_api_base_url="https://supplies-api.wildberries.ru",
            wb_api_token="token",
        )

        draft = service.get_draft_supply_by_id("50680482", config)

        self.assertEqual("50680482", draft.supply_id)
        self.assertEqual(1, len(draft.items))
        self.assertEqual("nbf_black_z", draft.items[0].supplier_article)
        self.assertEqual("S", draft.items[0].size)
        self.assertEqual(21, draft.items[0].quantity)
