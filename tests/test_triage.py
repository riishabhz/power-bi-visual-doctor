from pbi_visual_doctor.models import BLANK, ERROR, Finding, VisualRecord
from pbi_visual_doctor.triage import (
    build_questions,
    build_request,
    extract_names,
    fingerprint,
    parse_response,
)


def visual(**kw):
    base = dict(report_name="Sales", page_name="Pipeline", page_url="x", visual_index=0, title="Revenue", status=ERROR)
    base.update(kw)
    return VisualRecord(**base)


def test_extract_names_handles_apostrophes_and_dax_refs():
    text = ("Couldn't load the data. The field 'Total Revenue' in table 'Sales' doesn't exist. "
            "Calculation error in measure 'Sales'[Conversion Rate] and Finance[Ledger].")
    names = extract_names(text)
    assert "Total Revenue" in names
    assert "Sales[Conversion Rate]" in names
    assert "Finance[Ledger]" in names
    assert not any("load the data" in n for n in names)


def test_extract_names_handles_curly_quotes():
    text = "The field " + chr(0x2018) + "Net Sales" + chr(0x2019) + " was renamed."
    assert extract_names(text) == ["Net Sales"]


def test_questions_follow_system_one_schema():
    q = build_questions(BLANK)
    assert q["category"]["type"] == "choice" and len(q["category"]["criteria"]) <= 255
    assert q["severity"]["type"] == "score" and 2 <= len(q["severity"]["criteria"]) <= 10
    assert set(q["is_broken"]["criteria"]) == {"true", "false"}
    assert "is_broken" not in build_questions(ERROR)


def test_request_contains_details_and_truncates():
    v = visual(error_text="x" * 5000, details_text="The field 'A' was deleted.")
    req = build_request(v, max_chars=100)
    assert len(req["state"]["message"]) == 100
    assert req["state"]["details"].startswith("The field")


def test_parse_jev_shaped_response():
    # Shapes taken from the TypeSafe API reference.
    response = {
        "model": "jev-1.13.0",
        "answers": {
            "category": {"type": "choice", "choice": "missing_field", "probabilities": {"missing_field": 0.9, "other": 0.1}, "confidence": 0.81},
            "owner": {"type": "choice", "choice": "model_owner", "probabilities": {}, "confidence": 0.7},
            "severity": {"type": "score", "score": 2.3, "legend": {}, "probabilities": {}, "confidence": 0.6},
            "is_broken": {"type": "noul", "noul": 0.93},
        },
        "usage": {"input_tokens": 300, "output_tokens": 40},
    }
    t = parse_response(response, "jev")
    assert t.category == "missing_field" and t.owner == "model_owner"
    assert t.severity_label == "Unusable visual"
    assert t.broken_probability == 0.93
    assert t.model == "jev-1.13.0"


def test_same_missing_field_groups_across_pages():
    msg = "The field 'Total Revenue' in table 'Sales' doesn't exist. Activity ID: {}"
    a = visual(details_text=msg.format("3f1c2a9e-8b77-4c1e-9d0a-5e6f7a8b9c0d"))
    b = visual(page_name="Bookings", details_text=msg.format("7d2e1b0c-8b77-4c1e-9d0a-5e6f7a8b9c0d"))
    t = parse_response({"answers": {"category": {"choice": "missing_field"}}}, "x")
    assert fingerprint(a, t)[0] == fingerprint(b, t)[0]


def test_blank_visual_only_broken_when_model_says_so():
    t_yes = parse_response({"answers": {"category": {"choice": "other"}, "is_broken": {"noul": 0.8}}}, "x")
    t_no = parse_response({"answers": {"category": {"choice": "other"}, "is_broken": {"noul": 0.2}}}, "x")
    v = visual(status=BLANK)
    assert Finding(v, t_yes).is_confirmed_broken
    assert not Finding(v, t_no).is_confirmed_broken
