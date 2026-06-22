from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from wb_marks_app.exceptions import ConfigurationError, IntegrationUnavailableError, ManualStepRequired
from wb_marks_app.models import AppConfig, DraftSupply, SupplyItem
from wb_marks_app.services.browser import BrowserSessionManager


Logger = Callable[[str], None]


class WBService:
    def __init__(self, browser: BrowserSessionManager, logger: Logger | None = None, session=None) -> None:
        self.browser = browser
        self.logger = logger or (lambda _: None)
        self.session = session

    def get_latest_draft_supply(self, config: AppConfig) -> DraftSupply:
        errors: list[str] = []
        mode = config.wb_mode

        if mode in {"auto", "api"}:
            try:
                return self._fetch_via_api(config)
            except Exception as exc:
                errors.append(f"API: {exc}")
                self.logger(f"WB API fetch failed: {exc}")
                if mode == "api":
                    raise

        if mode in {"auto", "browser"}:
            try:
                return self._fetch_via_browser(config)
            except Exception as exc:
                errors.append(f"Browser: {exc}")
                self.logger(f"WB browser fetch failed: {exc}")
                if mode == "browser":
                    raise

        if config.wb_draft_source_file:
            try:
                return self._load_from_json(Path(config.wb_draft_source_file))
            except Exception as exc:
                errors.append(f"Local JSON: {exc}")

        joined = " | ".join(errors) if errors else "No WB integration path is configured."
        raise IntegrationUnavailableError(
            "Could not load the latest WB draft supply automatically. "
            + joined
            + " Configure WB API details, add browser automation, or provide wb_draft_source_file."
        )

    def _fetch_via_api(self, config: AppConfig) -> DraftSupply:
        drafts = self.list_draft_supplies(config)
        if not drafts:
            raise IntegrationUnavailableError("WB API returned no draft supplies.")
        latest = drafts[0]
        return self.get_draft_supply_by_id(latest["preorderID"], config)

    def list_draft_supplies(self, config: AppConfig) -> list[dict]:
        if not config.wb_api_base_url or not config.wb_api_token:
            raise ConfigurationError("WB API base URL or token is missing.")
        session = self._session()
        now = datetime.now(timezone.utc).astimezone()
        from_day = (now - timedelta(days=30)).date().isoformat()
        till_day = (now + timedelta(days=1)).date().isoformat()
        response = session.post(
            config.wb_api_base_url.rstrip("/") + "/api/v1/supplies",
            headers=self._headers(config),
            json={
                "statusIDs": [1],
                "dates": [
                    {
                        "from": from_day,
                        "till": till_day,
                        "type": "createDate",
                    }
                ],
            },
            timeout=config.step_timeout_seconds,
        )
        self._raise_for_status(response)
        payload = response.json()
        supplies = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(supplies, list):
            supplies = []
        draft_supplies = [item for item in supplies if int(item.get("statusID") or 0) == 1]
        draft_supplies.sort(key=lambda item: item.get("createDate") or "", reverse=True)
        return draft_supplies

    def get_draft_supply_by_id(self, preorder_id: str, config: AppConfig) -> DraftSupply:
        if not preorder_id:
            raise ConfigurationError("WB draft id is required.")
        metadata = None
        for item in self.list_draft_supplies(config):
            if str(item.get("preorderID") or "") == str(preorder_id):
                metadata = item
                break
        goods = self.fetch_draft_goods(preorder_id, config)
        if not goods:
            raise IntegrationUnavailableError(f"WB draft {preorder_id} does not contain any goods.")
        created_at_raw = (
            str((metadata or {}).get("createDate") or datetime.now(timezone.utc).isoformat())
            .replace("Z", "+00:00")
        )
        items = [self._supply_item_from_wb_good(preorder_id, raw) for raw in goods]
        return DraftSupply(
            supply_id=str(preorder_id),
            name=f"WB draft {preorder_id}",
            status="draft",
            created_at=datetime.fromisoformat(created_at_raw),
            items=items,
            source="wb_api",
        )

    def fetch_draft_goods(self, preorder_id: str, config: AppConfig) -> list[dict]:
        session = self._session()
        response = session.get(
            config.wb_api_base_url.rstrip("/")
            + f"/api/v1/supplies/{preorder_id}/goods?isPreorderID=true&limit=1000&offset=0",
            headers=self._headers(config),
            timeout=config.step_timeout_seconds,
        )
        self._raise_for_status(response)
        payload = response.json()
        goods = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(goods, list):
            goods = []
        return goods

    def _fetch_via_browser(self, config: AppConfig) -> DraftSupply:
        self.browser.open_url(config.wb_seller_url)
        raise ManualStepRequired(
            "WB browser fallback needs site-specific selectors or export logic. "
            "The browser was opened with the saved profile so you can log in and inspect the current draft."
        )

    def _load_from_json(self, path: Path) -> DraftSupply:
        if not path.exists():
            raise ConfigurationError(f"WB draft source JSON was not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return self._draft_from_api_payload(payload, source="json")

    def _draft_from_api_payload(self, payload: dict, source: str = "api") -> DraftSupply:
        supply_id = str(payload.get("id") or payload.get("supply_id") or payload.get("supplyId") or "")
        created_raw = payload.get("createdAt") or payload.get("created_at") or datetime.utcnow().isoformat()
        items_payload = payload.get("items") or payload.get("products") or []
        if not supply_id:
            raise IntegrationUnavailableError("WB draft payload does not contain a supply id.")

        items: list[SupplyItem] = []
        for raw in items_payload:
            quantity = int(raw.get("quantity") or raw.get("qty") or 1)
            items.append(
                SupplyItem(
                    barcode=str(raw.get("barcode") or raw.get("barcodeValue") or ""),
                    name=str(raw.get("name") or raw.get("title") or ""),
                    quantity=quantity,
                    supplier_article=str(raw.get("supplier_article") or raw.get("supplierArticle") or ""),
                    nm_id=str(raw.get("nmId") or raw.get("nm_id") or ""),
                    sku=str(raw.get("sku") or ""),
                    draft_supply_id=supply_id,
                )
            )

        if not items:
            raise IntegrationUnavailableError("WB draft payload does not contain any items.")

        return DraftSupply(
            supply_id=supply_id,
            name=str(payload.get("name") or f"Draft supply {supply_id}"),
            status=str(payload.get("status") or "draft"),
            created_at=datetime.fromisoformat(str(created_raw).replace("Z", "+00:00")),
            items=items,
            source=source,
        )

    def validate_connection(self, config: AppConfig) -> None:
        self.list_draft_supplies(config)

    def _supply_item_from_wb_good(self, preorder_id: str, raw: dict) -> SupplyItem:
        return SupplyItem(
            barcode=str(raw.get("barcode") or raw.get("barcodeValue") or ""),
            name=str(raw.get("subjectName") or raw.get("name") or raw.get("title") or raw.get("vendorCode") or ""),
            quantity=int(raw.get("quantity") or raw.get("qty") or 0),
            supplier_article=str(raw.get("vendorCode") or raw.get("supplierArticle") or ""),
            size=str(raw.get("techSize") or raw.get("size") or ""),
            nm_id=str(raw.get("nmID") or raw.get("nmId") or raw.get("nm_id") or ""),
            sku=str(raw.get("sku") or raw.get("sa") or ""),
            draft_supply_id=str(preorder_id),
        )

    def _headers(self, config: AppConfig) -> dict[str, str]:
        return {
            "Authorization": config.wb_api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

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
                raise IntegrationUnavailableError(f"WB API request failed: {status_code} {body}") from exc
            raise
