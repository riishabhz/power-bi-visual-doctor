"""Playwright crawler: open each report page and read every visual as text."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from . import detect
from .models import ERROR, OK, TIMEOUT, ScanResult, VisualRecord

# Runs inside the page. Marks each visual container with data-pbid-idx so the
# crawler can find it again, and returns plain-text facts about it.
EXTRACT_JS = r"""
(cfg) => {
  const first = (root, sels) => {
    for (const s of sels) { try { const el = root.querySelector(s); if (el) return el; } catch (e) {} }
    return null;
  };
  let containers = [];
  for (const s of cfg.visual_container) {
    try {
      const found = Array.from(document.querySelectorAll(s));
      if (found.length) { containers = found; break; }
    } catch (e) {}
  }
  // Idle spinners stay in the DOM with no size or display:none; only count drawn ones.
  const shown = el => {
    const b = el.getBoundingClientRect();
    return b.width > 0 && b.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const firstShown = (root, sels) => {
    for (const s of sels) {
      try { const el = Array.from(root.querySelectorAll(s)).find(shown); if (el) return el; } catch (e) {}
    }
    return null;
  };
  containers = containers.filter(c => !containers.some(o => o !== c && o.contains(c)));
  return containers.map((c, i) => {
    c.setAttribute('data-pbid-idx', String(i));
    // The Power BI service renders <visual-container> inline with a 0x0 box;
    // the size is on its children.
    let r = c.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) {
      for (const child of Array.from(c.children)) {
        const b = child.getBoundingClientRect();
        if (b.width * b.height > r.width * r.height) r = b;
      }
    }
    const titleEl = first(c, cfg.visual_title);
    let vtype = '';
    for (const a of cfg.visual_type_attr) {
      const holder = c.hasAttribute(a) ? c : c.querySelector('[' + a + ']');
      if (holder) { vtype = holder.getAttribute(a) || ''; break; }
    }
    if (!vtype) {
      // The service marks the type as a class, e.g. <div class="visual visual-barChart">.
      const v = c.querySelector('.visual[class*="visual-"]');
      const cls = v ? Array.from(v.classList).find(k => k.startsWith('visual-')) : null;
      if (cls) vtype = cls.slice('visual-'.length);
    }
    const spinner = !!firstShown(c, cfg.spinner) || c.getAttribute('aria-busy') === 'true';
    const graphics = Array.from(c.querySelectorAll('svg, canvas, img')).filter(g => {
      const b = g.getBoundingClientRect(); return b.width > 4 && b.height > 4;
    }).length;
    return {
      index: i,
      title: titleEl ? (titleEl.innerText || '').trim() : '',
      visual_type: vtype,
      aria_label: c.getAttribute('aria-label') || '',
      text: (c.innerText || '').trim().slice(0, 4000),
      spinner: spinner,
      graphics: graphics,
      width: r.width,
      height: r.height
    };
  });
}
"""

TABS_JS = r"""
(sels) => {
  for (const s of sels) {
    try {
      const tabs = Array.from(document.querySelectorAll(s));
      if (tabs.length) {
        return tabs.map((t, i) => {
          t.setAttribute('data-pbid-tab', String(i));
          return (t.innerText || t.getAttribute('aria-label') || ('Page ' + (i + 1))).trim();
        });
      }
    } catch (e) {}
  }
  return [];
}
"""

LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.windows.net")


@dataclass
class PageJob:
    report_name: str
    page_name: str
    url: str


def build_jobs(reports: list[dict[str, Any]]) -> tuple[list[PageJob], list[dict[str, Any]]]:
    """Split reports into explicit page jobs and reports that need tab discovery."""
    jobs: list[PageJob] = []
    tab_reports: list[dict[str, Any]] = []
    for report in reports:
        name = report.get("name") or report.get("url", "report")
        pages = report.get("pages") or []
        if not pages:
            tab_reports.append(report)
            continue
        for i, page in enumerate(pages):
            if isinstance(page, dict):
                url = page.get("url") or f"{report['url'].rstrip('/')}/{page['section']}"
                jobs.append(PageJob(name, page.get("name") or f"Page {i + 1}", url))
            else:
                jobs.append(PageJob(name, str(page), f"{report['url'].rstrip('/')}/{page}"))
    return jobs, tab_reports


class Crawler:
    def __init__(
        self,
        cfg: dict[str, Any],
        storage_state: Optional[str] = None,
        headed: bool = False,
        progress: Callable[[str], None] = print,
        include_hidden: bool = False,
    ) -> None:
        self.cfg = cfg
        self.include_hidden = include_hidden
        self.sel = cfg["selectors"]
        self.timing = cfg["timing"]
        self.storage_state = storage_state
        self.headed = headed
        self.progress = progress
        self.result = ScanResult()

    # ---------------------------------------------------------------- public
    async def crawl(self, reports: list[dict[str, Any]]) -> ScanResult:
        self.result.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        jobs, tab_reports = build_jobs(reports)
        sem = asyncio.Semaphore(int(self.timing.get("concurrency", 3)))

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(headless=not self.headed)
            context: BrowserContext = await browser.new_context(
                storage_state=self.storage_state,
                viewport={"width": 1600, "height": 1000},
            )
            context.set_default_timeout(int(self.timing["page_load_timeout_ms"]))

            async def run(coro_factory):
                async with sem:
                    await coro_factory()

            tasks = [run(lambda j=j: self._scan_page_job(context, j)) for j in jobs]
            tasks += [run(lambda r=r: self._scan_report_tabs(context, r)) for r in tab_reports]
            await asyncio.gather(*tasks)
            await context.close()
            await browser.close()

        self.result.visuals.sort(key=lambda v: (v.report_name, v.page_name, v.visual_index))
        self.result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self.result

    # --------------------------------------------------------------- helpers
    def _page_error(self, report: str, page: str, url: str, message: str) -> None:
        self.progress(f"  ! {report} / {page}: {message}")
        self.result.page_errors.append({"report": report, "page": page, "url": url, "error": message})

    async def _open(self, context: BrowserContext, url: str) -> Page:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=int(self.timing["page_load_timeout_ms"]))
        if any(host in page.url for host in LOGIN_HOSTS):
            await page.close()
            raise RuntimeError("Redirected to sign-in. Run 'pbi-doctor login' to refresh the saved session.")
        return page

    async def _extract(self, page: Page) -> list[dict[str, Any]]:
        return await page.evaluate(EXTRACT_JS, self.sel)

    async def _wait_for_render(self, page: Page) -> tuple[list[dict[str, Any]], bool]:
        """Poll until the visual count is stable and nothing is loading.

        Returns (visuals, timed_out). timed_out is True when render_max_wait_ms
        expired with visuals still showing a spinner.
        """
        settle = int(self.timing["render_settle_ms"]) / 1000
        max_wait = int(self.timing["render_max_wait_ms"]) / 1000
        start = time.monotonic()
        last_count = -1
        stable_since = time.monotonic()
        raw: list[dict[str, Any]] = []
        while True:
            raw = await self._extract(page)
            now = time.monotonic()
            if len(raw) != last_count:
                last_count = len(raw)
                stable_since = now
            loading = any(v.get("spinner") for v in raw)
            if raw and not loading and now - stable_since >= settle:
                return raw, False
            if now - start >= max_wait:
                return raw, loading
            await asyncio.sleep(0.5)

    async def _read_details(self, page: Page, index: int) -> str:
        """Click 'See details' inside a visual and return the dialog text."""
        container = page.locator(f'[data-pbid-idx="{index}"]')
        timeout = int(self.timing["details_timeout_ms"])
        for label in self.sel["details_button_text"]:
            button = container.get_by_text(label, exact=False)
            try:
                if await button.count() == 0:
                    continue
                await button.first.click(timeout=timeout)
            except Exception:
                continue
            for dialog_sel in self.sel["dialog"]:
                dialog = page.locator(dialog_sel).last
                try:
                    await dialog.wait_for(state="visible", timeout=timeout)
                    text = (await dialog.inner_text()).strip()
                except Exception:
                    continue
                await page.keyboard.press("Escape")
                try:
                    await dialog.wait_for(state="hidden", timeout=timeout)
                except Exception:
                    pass
                return text
        return ""

    async def _is_hidden_page(self, page: Page, page_name: str) -> bool:
        """A page opened by URL that is missing from the page tabs is hidden in the report."""
        tabs: list[str] = await page.evaluate(TABS_JS, self.sel["page_tabs"])
        names = {t.strip().lower() for t in tabs}
        return bool(names) and page_name.strip().lower() not in names

    async def _collect(self, page: Page, report: str, page_name: str, url: str, check_hidden: bool = False) -> None:
        raw, timed_out = await self._wait_for_render(page)
        hidden = check_hidden and await self._is_hidden_page(page, page_name)
        if hidden and not self.include_hidden:
            self.progress(f"  - {report} / {page_name}: skipped (hidden page)")
            return
        if not raw:
            self._page_error(report, page_name, url, "No visuals found (check selectors or permissions).")
            return
        counts = {"ok": 0, "problem": 0}
        for item in raw:
            if detect.is_ignored(item, self.cfg):
                continue
            status, error_text = detect.classify(item, self.cfg, timed_out=timed_out)
            record = VisualRecord(
                report_name=report,
                page_name=page_name,
                page_url=url,
                visual_index=int(item["index"]),
                title=item.get("title", ""),
                visual_type=item.get("visual_type", ""),
                status=status,
                error_text=error_text,
                aria_label=item.get("aria_label", ""),
                text_sample=(item.get("text") or "")[:500],
                page_hidden=hidden,
            )
            if status == ERROR:
                record.details_text = detect.clean_details(
                    await self._read_details(page, record.visual_index), self.cfg
                )
            counts["ok" if status == OK else "problem"] += 1
            self.result.visuals.append(record)
        note = " (hidden page)" if hidden else ""
        self.progress(f"  - {report} / {page_name}{note}: {counts['ok']} ok, {counts['problem']} to triage")

    # ------------------------------------------------------------ page modes
    async def _scan_page_job(self, context: BrowserContext, job: PageJob) -> None:
        try:
            page = await self._open(context, job.url)
        except Exception as exc:
            self._page_error(job.report_name, job.page_name, job.url, str(exc).splitlines()[0])
            return
        try:
            await self._collect(page, job.report_name, job.page_name, job.url, check_hidden=True)
        except Exception as exc:
            self._page_error(job.report_name, job.page_name, job.url, str(exc).splitlines()[0])
        finally:
            await page.close()

    async def _scan_report_tabs(self, context: BrowserContext, report: dict[str, Any]) -> None:
        name = report.get("name") or report["url"]
        url = report["url"]
        try:
            page = await self._open(context, url)
        except Exception as exc:
            self._page_error(name, "(report)", url, str(exc).splitlines()[0])
            return
        try:
            await self._wait_for_render(page)
            tab_names: list[str] = await page.evaluate(TABS_JS, self.sel["page_tabs"])
            if not tab_names:
                await self._collect(page, name, "Page 1", url)
                return
            for i, tab_name in enumerate(tab_names):
                try:
                    await page.locator(f'[data-pbid-tab="{i}"]').click()
                    await page.wait_for_timeout(300)
                    await self._collect(page, name, tab_name or f"Page {i + 1}", page.url)
                except Exception as exc:
                    self._page_error(name, tab_name, page.url, str(exc).splitlines()[0])
        except Exception as exc:
            self._page_error(name, "(report)", url, str(exc).splitlines()[0])
        finally:
            await page.close()


async def crawl(
    reports: list[dict[str, Any]],
    cfg: dict[str, Any],
    storage_state: Optional[str] = None,
    headed: bool = False,
    progress: Callable[[str], None] = print,
    include_hidden: bool = False,
) -> ScanResult:
    return await Crawler(cfg, storage_state, headed, progress, include_hidden).crawl(reports)
