"""Optional report and page discovery through the Power BI REST API.

Needs an access token in PBI_ACCESS_TOKEN, for example from the Azure CLI:

    az account get-access-token --resource https://analysis.windows.net/powerbi/api \
        --query accessToken -o tsv

Without a token, pages are discovered by clicking the report's page tabs.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

API = "https://api.powerbi.com/v1.0/myorg"
_REPORT_URL = re.compile(r"/groups/(?P<group>[^/]+)/reports/(?P<report>[0-9a-fA-F-]{36})")


def parse_report_url(url: str) -> Optional[tuple[str, str]]:
    match = _REPORT_URL.search(url or "")
    if not match:
        return None
    return match.group("group"), match.group("report")


async def _get(client: httpx.AsyncClient, path: str) -> list[dict[str, Any]]:
    resp = await client.get(f"{API}{path}")
    resp.raise_for_status()
    return resp.json().get("value", [])


async def list_pages(client: httpx.AsyncClient, group_id: str, report_id: str, report_url: str) -> list[dict[str, str]]:
    pages = await _get(client, f"/groups/{group_id}/reports/{report_id}/pages")
    pages.sort(key=lambda p: p.get("order", 0))
    base = report_url.split("?")[0].rstrip("/")
    # Strip an existing page section from the URL before appending.
    base = re.sub(r"/ReportSection[^/]*$", "", base)
    return [
        {"name": p.get("displayName") or p["name"], "url": f"{base}/{p['name']}"}
        for p in pages
    ]


async def expand_targets(targets: dict[str, Any], token: Optional[str]) -> list[dict[str, Any]]:
    """Turn the targets file into a flat list of reports, each with pages if known."""
    reports: list[dict[str, Any]] = [dict(r) for r in targets.get("reports", []) or []]
    workspaces = targets.get("workspaces", []) or []

    if not token:
        if workspaces:
            raise RuntimeError(
                "The targets file lists workspaces, which need PBI_ACCESS_TOKEN to enumerate reports."
            )
        return reports

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        for ws in workspaces:
            ws_id = ws["id"] if isinstance(ws, dict) else str(ws)
            skip = set((ws.get("exclude") or []) if isinstance(ws, dict) else [])
            for rep in await _get(client, f"/groups/{ws_id}/reports"):
                if rep.get("name") in skip or rep.get("reportType") == "PaginatedReport":
                    continue
                reports.append({"name": rep["name"], "url": rep["webUrl"], "pages": []})

        for report in reports:
            if report.get("pages"):
                continue
            ids = parse_report_url(report.get("url", ""))
            if ids:
                report["pages"] = await list_pages(client, ids[0], ids[1], report["url"])
    return reports
