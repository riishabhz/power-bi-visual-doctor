"""A page opened by URL that is missing from the page tabs is hidden: skipped, or scanned and labelled."""

from pbi_visual_doctor.config import load_config
from pbi_visual_doctor.crawler import crawl

PAGE = """<html><body>
<div role="tab">Overview</div><div role="tab">Details</div>
<visual-container data-visual-type="card" style="display:block;width:200px;height:100px">
  <div class="visualTitle">Revenue</div><div>42</div>
</visual-container>
</body></html>"""


def _report(tmp_path):
    html = tmp_path / "page.html"
    html.write_text(PAGE, encoding="utf-8")
    url = html.as_uri()
    return {"name": "R", "url": url, "pages": [{"name": "Overview", "url": url}, {"name": "Drill through map", "url": url}]}


CFG = load_config(overrides={"timing": {"render_settle_ms": 200, "render_max_wait_ms": 3000}})


async def test_hidden_page_skipped_by_default(tmp_path):
    scan = await crawl([_report(tmp_path)], CFG, progress=lambda _: None)
    assert {v.page_name for v in scan.visuals} == {"Overview"}


async def test_hidden_page_scanned_and_flagged_when_asked(tmp_path):
    scan = await crawl([_report(tmp_path)], CFG, progress=lambda _: None, include_hidden=True)
    hidden = {v.page_name: v.page_hidden for v in scan.visuals}
    assert hidden == {"Overview": False, "Drill through map": True}
