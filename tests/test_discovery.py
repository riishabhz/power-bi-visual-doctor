"""Workspace discovery against a fake Power BI REST API (no network)."""

import httpx
import pytest

from pbi_visual_doctor.discovery import DiscoveryError, discover, parse_group_url, token_from_session

SALES = "aaaaaaaa-0000-0000-0000-000000000001"
OPS = "bbbbbbbb-0000-0000-0000-000000000002"
PIPELINE = "11111111-1111-1111-1111-111111111111"
GUEST = "22222222-2222-2222-2222-222222222222"
INVOICE = "33333333-3333-3333-3333-333333333333"
APP = "https://app.powerbi.com"


def _report(group, rid, name, report_type="PowerBIReport"):
    return {"id": rid, "name": name, "webUrl": f"{APP}/groups/{group}/reports/{rid}", "reportType": report_type}


GROUPS = [{"id": SALES, "name": "Sales Analytics"}, {"id": OPS, "name": "Ops"}]
REPORTS = {
    SALES: [_report(SALES, PIPELINE, "Pipeline"), _report(SALES, INVOICE, "Invoices", "PaginatedReport")],
    OPS: [_report(OPS, GUEST, "Guest Experience")],
}
PAGES = [
    {"name": "ReportSectionB", "displayName": "Bookings", "order": 1, "visibility": 0},
    {"name": "ReportSectionH", "displayName": "Hidden Drillthrough", "order": 2, "visibility": 1},
    {"name": "ReportSectionA", "displayName": "Overview", "order": 0, "visibility": 0},
]


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer fake-token"
    parts = request.url.path.split("/")[3:]  # after /v1.0/myorg
    if parts == ["groups"]:
        return httpx.Response(200, json={"value": GROUPS})
    if len(parts) == 3 and parts[0] == "groups" and parts[2] == "reports":
        return httpx.Response(200, json={"value": REPORTS[parts[1]]})
    if len(parts) == 4 and parts[2] == "reports":
        return httpx.Response(200, json=next(r for r in REPORTS[parts[1]] if r["id"] == parts[3]))
    if len(parts) == 5 and parts[4] == "pages":
        return httpx.Response(200, json={"value": PAGES})
    return httpx.Response(404)


def _run(**kw):
    return discover("fake-token", progress=lambda _: None, transport=httpx.MockTransport(_handler), **kw)


async def test_default_discovery_finds_every_report():
    reports = await _run()
    assert [r["name"] for r in reports] == ["Sales Analytics / Pipeline", "Ops / Guest Experience"]
    pages = reports[0]["pages"]
    assert [p["name"] for p in pages] == ["Overview", "Bookings"]
    assert pages[0]["url"].endswith(f"/reports/{PIPELINE}/ReportSectionA")


async def test_workspace_filter_partial_name():
    reports = await _run(workspaces=["sales"])
    assert [r["name"] for r in reports] == ["Sales Analytics / Pipeline"]


async def test_unknown_workspace_lists_available_names():
    with pytest.raises(DiscoveryError) as err:
        await _run(workspaces=["Finance"])
    assert "Sales Analytics" in str(err.value) and "Ops" in str(err.value)


async def test_report_url_from_browser():
    url = f"{APP}/groups/{OPS}/reports/{GUEST}/ReportSection123abc?experience=power-bi"
    reports = await _run(urls=[url])
    assert [r["name"] for r in reports] == ["Ops / Guest Experience"]
    assert reports[0]["pages"][0]["url"] == f"{APP}/groups/{OPS}/reports/{GUEST}/ReportSectionA"


def test_parse_group_url():
    assert parse_group_url(f"{APP}/groups/{SALES}/list?experience=power-bi") == SALES
    assert parse_group_url(f"{APP}/groups/me/reports/{PIPELINE}") == "me"
    assert parse_group_url(f"{APP}/home") is None


async def test_token_from_session_reads_web_app_token(tmp_path):
    token = "x" * 200
    page = tmp_path / "home.html"
    page.write_text(
        f"<html><body><script>setTimeout(() => {{ window.powerBIAccessToken = '{token}'; }}, 300);"
        "</script></body></html>",
        encoding="utf-8",
    )
    got = await token_from_session({"cookies": [], "origins": []}, start_url=page.as_uri(), timeout_s=10)
    assert got == token
