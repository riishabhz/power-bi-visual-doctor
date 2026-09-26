"""Interactive sign-in: save a browser session for later headless scans."""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from .discovery import TOKEN_JS

START_URL = "https://app.powerbi.com/home"
SIGN_IN_TIMEOUT_S = 300


async def _signed_in(page) -> bool:
    try:
        return "app.powerbi.com" in page.url and bool(await page.evaluate(TOKEN_JS))
    except Exception:
        return False  # the page navigates during sign-in; try again


async def _login(out_path: Path, start_url: str) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await context.new_page()
            await page.goto(start_url)
            print("A browser window has opened. Sign in to Power BI (including MFA).")
            print("It closes by itself once you reach the Power BI home page.")
            for _ in range(SIGN_IN_TIMEOUT_S):
                if await _signed_in(page):
                    break
                if page.is_closed():
                    return False
                await asyncio.sleep(1)
            else:
                return False
            await asyncio.sleep(2)  # let the web app finish writing its session
            await context.storage_state(path=str(out_path))
        finally:
            await browser.close()
    print(f"Session saved to {out_path}. Keep this file private; it grants access to your account.")
    return True


def login(out_path: str = ".auth/state.json", start_url: str = START_URL) -> None:
    if not asyncio.run(_login(Path(out_path), start_url)):
        raise SystemExit("Sign-in was not completed. Run 'pbi-doctor login' again.")
