"""Interactive sign-in: save a browser session for later headless scans."""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

START_URL = "https://app.powerbi.com/home"


async def _login(out_path: Path, start_url: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()
        await page.goto(start_url)
        print("A browser window has opened. Sign in to Power BI (including MFA).")
        print("When you can see the Power BI home page, come back here and press Enter.")
        await asyncio.get_running_loop().run_in_executor(None, input)
        await context.storage_state(path=str(out_path))
        await browser.close()
    print(f"Session saved to {out_path}. Keep this file private; it grants access to your account.")


def login(out_path: str = ".auth/state.json", start_url: str = START_URL) -> None:
    asyncio.run(_login(Path(out_path), start_url))
