from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Callable

from wb_marks_app.exceptions import IntegrationUnavailableError

try:
    from playwright.sync_api import BrowserContext, Page, sync_playwright
except ImportError:  # pragma: no cover - optional runtime dependency in tests
    BrowserContext = object  # type: ignore[assignment]
    Page = object  # type: ignore[assignment]
    sync_playwright = None


Logger = Callable[[str], None]


class BrowserSessionManager:
    def __init__(self, profile_dir: Path, logger: Logger | None = None) -> None:
        self.profile_dir = profile_dir
        self.logger = logger or (lambda _: None)
        self._playwright = None
        self._context: BrowserContext | None = None

    def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if sync_playwright is None:
            self.logger("Playwright is not installed; falling back to the system browser.")
            return
        if self._context is not None:
            return

        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=False,
        )
        self.logger(f"Opened Chromium persistent profile at {self.profile_dir}")

    def get_page(self) -> Page | None:
        if self._context is None:
            return None
        pages = self._context.pages
        if pages:
            return pages[0]
        return self._context.new_page()

    def open_url(self, url: str) -> None:
        self.start()
        page = self.get_page()
        if page is None:
            webbrowser.open(url)
            return
        page.goto(url, wait_until="domcontentloaded")

    def ensure_page(self, url: str) -> Page:
        self.start()
        page = self.get_page()
        if page is None:
            webbrowser.open(url)
            raise IntegrationUnavailableError(
                "Playwright is not available. Install dependencies and Chromium to enable embedded browser automation."
            )
        page.goto(url, wait_until="domcontentloaded")
        return page

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

