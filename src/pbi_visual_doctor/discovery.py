"""Workspace, report and page discovery through the Power BI REST API.

The access token comes from the saved browser session (the Power BI web app
exposes it as window.powerBIAccessToken), or from PBI_ACCESS_TOKEN if set.
The token is only kept in memory.

Reports without pages are scanned by clicking the report's page tabs.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx

API = "https://api.powerbi.com/v1.0/myorg"
SERVICE_URL = "https://app.powerbi.com/home"
TOKEN_JS = (
    "() => (typeof window.powerBIAccessToken === 'string' && window.powerBIAccessToken.length > 100)"
    " ? window.powerBIAccessToken : null"
)
MY_WORKSPACE = "My workspace"

_REPORT_URL = re.compile(r"/groups/(?P<group>[^/]+)/reports/(?P<report>[0-9a-fA-F-]{36})")
_GROUP_URL = re.compile(r"/groups/(?P<group>me|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27})(?=[/?#]|$)")
_LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com")
_TOKEN_HOSTS = ("analysis.windows.net", "api.powerbi.com")
_RELOGIN = "Run 'pbi-doctor login' again."


class DiscoveryError(RuntimeError):
    """Discovery could not continue; the message is meant for the user."""


def parse_report_url(url: str) -> Optional[tuple[str, str]]:
    match = _REPORT_URL.search(url or "")
    if not match:
        return None
    return match.group("group"), match.group("report")


def parse_group_url(url: str) -> Optional[str]:
    """Workspace id (a GUID or "me") from any URL containing /groups/<id>/."""
    match = _GROUP_URL.search(url or "")
    return match.group("group").lower() if match else None


def page_base_url(report_url: str) -> str:
    base = report_url.split("?")[0].rstrip("/")
    # Strip an existing page section from the URL before appending.
    return re.sub(r"/ReportSection[^/]*$", "", base)


# ------------------------------------------------------------------ token
async def token_from_session(
    storage_state: Any,
    start_url: str = SERVICE_URL,
    timeout_s: float = 60,
    headed: bool = False,
) -> str:
    """Open the saved session and read the access token the web app holds."""
    from playwright.async_api import async_playwright

    captured: dict[str, str] = {}

    def on_request(request: Any) -> None:
        if "token" in captured:
            return
        host = urlsplit(request.url).hostname or ""
        if any(h in host for h in _TOKEN_HOSTS):
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer ") and len(auth) > 107:
                captured["token"] = auth[7:].strip()

    loop = asyncio.get_running_loop()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        try:
            context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()
            page.on("request", on_request)
            try:
                await page.goto(start_url, wait_until="domcontentloaded")
            except Exception as exc:
                raise DiscoveryError(f"Could not open {start_url}: {exc}") from exc
            deadline = loop.time() + timeout_s
            while True:
                host = urlsplit(page.url).hostname or ""
                if any(h in host for h in _LOGIN_HOSTS):
                    raise DiscoveryError(f"Your saved Power BI session has expired. {_RELOGIN}")
                try:
                    token = await page.evaluate(TOKEN_JS)
                except Exception:
                    token = None  # the page is navigating; try again
                token = token or captured.get("token")
                if token:
                    return token
                if loop.time() >= deadline:
                    raise DiscoveryError(
                        f"Could not read a Power BI access token within {int(timeout_s)}s. {_RELOGIN}"
                    )
                await asyncio.sleep(1)
        finally:
            await browser.close()


# -------------------------------------------------------------------- API
class PowerBIApi:
    """Minimal async client for the Power BI REST API. Group "me" is My workspace."""

    def __init__(self, token: str, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=30, transport=transport
        )

    async def __aenter__(self) -> "PowerBIApi":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    @staticmethod
    def _prefix(group_id: str) -> str:
        return "" if group_id == "me" else f"/groups/{group_id}"

    async def _get(self, path: str) -> Any:
        resp = await self._client.get(f"{API}{path}")
        if resp.status_code == 401:
            raise DiscoveryError(f"Power BI rejected the access token (401). {_RELOGIN}")
        resp.raise_for_status()
        return resp.json()

    async def _list(self, path: str) -> list[dict[str, Any]]:
        return (await self._get(path)).get("value", []) or []

    async def workspaces(self) -> list[dict[str, Any]]:
        return await self._list("/groups")

    async def reports(self, group_id: str) -> list[dict[str, Any]]:
        return await self._list(f"{self._prefix(group_id)}/reports")

    async def report(self, group_id: str, report_id: str) -> dict[str, Any]:
        return await self._get(f"{self._prefix(group_id)}/reports/{report_id}")

    async def pages(
        self, group_id: str, report_id: str, report_url: str, include_hidden: bool = False
    ) -> list[dict[str, str]]:
        raw = await self._list(f"{self._prefix(group_id)}/reports/{report_id}/pages")
        # Hidden pages are skipped, unless asked for or every page is hidden. The API
        # does not always report visibility; the crawler then checks the page tabs.
        pages = raw if include_hidden else [p for p in raw if p.get("visibility") != 1] or raw
        pages.sort(key=lambda p: p.get("order", 0))
        base = page_base_url(report_url)
        return [
            {"name": p.get("displayName") or p["name"], "url": f"{base}/{p['name']}"}
            for p in pages
        ]


def _match_workspaces(available: list[dict[str, Any]], wanted: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for want in wanted:
        key = str(want).strip().lower()
        if key in ("me", MY_WORKSPACE.lower()):
            out.append(("me", MY_WORKSPACE))
            continue
        exact = [w for w in available if w["id"].lower() == key or (w.get("name") or "").lower() == key]
        hits = exact or [w for w in available if key in (w.get("name") or "").lower()]
        if not hits:
            names = ", ".join(sorted((w.get("name") or w["id"]) for w in available)) or "(none)"
            raise DiscoveryError(f"No workspace matches '{want}'. Available workspaces: {names}")
        out.extend((w["id"], w.get("name") or w["id"]) for w in hits)
    return out


async def discover(
    token: str,
    workspaces: Optional[list[str]] = None,
    reports: Optional[list[str]] = None,
    urls: Optional[list[str]] = None,
    include_personal: bool = False,
    progress: Callable[[str], None] = print,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    """List reports and pages the token can see, as {"name", "url", "pages"} dicts."""
    async with PowerBIApi(token, transport=transport) as api:
        try:
            available = await api.workspaces()
        except httpx.HTTPError as exc:
            raise DiscoveryError(f"Could not list your Power BI workspaces: {exc}") from exc
        ws_names = {w["id"].lower(): w.get("name") or w["id"] for w in available}
        ws_names["me"] = MY_WORKSPACE

        selected: list[tuple[str, str]] = []
        single: list[tuple[str, str]] = []
        if workspaces:
            selected.extend(_match_workspaces(available, workspaces))
        for url in urls or []:
            ids = parse_report_url(url)
            group = parse_group_url(url)
            if ids and group:
                single.append((group, ids[1]))
            elif group:
                selected.append((group, ws_names.get(group, group)))
            else:
                raise DiscoveryError(f"Not a Power BI workspace or report URL: {url}")
        if not workspaces and not urls:
            selected.extend((w["id"], w.get("name") or w["id"]) for w in available)
        if include_personal:
            selected.append(("me", MY_WORKSPACE))

        found: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()

        def add(group_id: str, ws_name: str, rep: dict[str, Any]) -> None:
            rid = str(rep.get("id", "")).lower()
            if not rid or rid in seen or rep.get("reportType") == "PaginatedReport":
                return
            seen.add(rid)
            url = rep.get("webUrl") or f"https://app.powerbi.com/groups/{group_id}/reports/{rep['id']}"
            found.append((group_id, {"id": rep["id"], "name": f"{ws_name} / {rep.get('name', rep['id'])}", "url": url}))

        for group_id, report_id in single:
            try:
                rep = await api.report(group_id, report_id)
            except httpx.HTTPError as exc:
                progress(f"Warning: could not read report {report_id} ({exc}); skipped.")
                continue
            add(group_id, ws_names.get(group_id.lower(), group_id), rep)

        wanted = [r.lower() for r in reports or []]
        done: set[str] = set()
        for group_id, ws_name in selected:
            if group_id.lower() in done:
                continue
            done.add(group_id.lower())
            try:
                listed = await api.reports(group_id)
            except httpx.HTTPError as exc:
                progress(f"Warning: skipped workspace '{ws_name}' ({exc}).")
                continue
            for rep in listed:
                if wanted and not any(w in (rep.get("name") or "").lower() for w in wanted):
                    continue
                add(group_id, ws_name, rep)

        out: list[dict[str, Any]] = []
        for group_id, rep in found:
            try:
                pages = await api.pages(group_id, rep["id"], rep["url"], include_hidden)
            except httpx.HTTPError:
                pages = []  # the crawler falls back to clicking page tabs
            out.append({"name": rep["name"], "url": rep["url"], "pages": pages})
        return out


async def expand_targets(
    targets: dict[str, Any],
    token: Optional[str],
    progress: Callable[[str], None] = print,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> list[dict[str, Any]]:
    """Turn the targets file into a flat list of reports, each with pages if known."""
    reports: list[dict[str, Any]] = [dict(r) for r in targets.get("reports", []) or []]
    workspaces = targets.get("workspaces", []) or []

    if not token:
        if workspaces:
            raise DiscoveryError(
                f"The targets file lists workspaces, which need you to be signed in. {_RELOGIN}"
            )
        return reports

    async with PowerBIApi(token, transport=transport) as api:
        for report in reports:
            if report.get("pages"):
                continue
            ids = parse_report_url(report.get("url", ""))
            if ids:
                try:
                    report["pages"] = await api.pages(ids[0], ids[1], report["url"])
                except httpx.HTTPError:
                    pass  # the crawler falls back to clicking page tabs

    for ws in workspaces:
        ws_id = ws["id"] if isinstance(ws, dict) else str(ws)
        skip = {str(s) for s in ((ws.get("exclude") or []) if isinstance(ws, dict) else [])}
        found = await discover(token, workspaces=[ws_id], progress=progress, transport=transport)
        reports.extend(r for r in found if r["name"].split(" / ", 1)[-1] not in skip)
    return reports
