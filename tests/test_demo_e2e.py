"""End to end: crawl the bundled demo pages with Playwright and check the report."""

import json

from pbi_visual_doctor.cli import main


def test_demo_end_to_end(tmp_path):
    out = tmp_path / "demo"
    assert main(["demo", "--out", str(out)]) == 0

    data = json.loads((out / "findings.json").read_text())
    by_title = {f["visual"]["title"]: f for f in data["findings"]}

    # Error text and the 'See details' dialog were both captured, without dialog chrome.
    rev = by_title["Revenue by Region"]
    assert "Total Revenue" in rev["visual"]["details_text"]
    assert "Close" not in rev["visual"]["details_text"].splitlines()

    # Pages discovered by clicking tabs.
    assert by_title["Room Nights by Property"]["visual"]["page_name"] == "Reviews"

    # Spinner that never finishes is a timeout; legitimately empty table is cleared.
    assert by_title["Guests by Country"]["visual"]["status"] == "timeout"
    assert by_title["Open Complaints (this week)"]["confirmed_broken"] is False

    # The renamed measure breaks two visuals on two pages and is grouped as one fix.
    top = data["summary"]["top_fixes"][0]
    assert top["visuals"] == 2 and top["pages"] == 2
    assert "Total Revenue" in top["names"]

    # Decorative shape skipped; missing page reported, not crashed.
    assert data["summary"]["totals"]["page_errors"] == 1
    assert (out / "report.html").read_text().count("Missing field") >= 1
    assert (out / "findings.csv").exists()
