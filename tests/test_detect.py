from pbi_visual_doctor.config import load_config
from pbi_visual_doctor.detect import clean_details, classify, is_ignored
from pbi_visual_doctor.models import BLANK, ERROR, OK, TIMEOUT

CFG = load_config()


def raw(**kw):
    base = {"title": "Revenue", "text": "Revenue", "spinner": False, "graphics": 1, "width": 300, "height": 200, "visual_type": ""}
    base.update(kw)
    return base


def test_healthy_chart_is_ok():
    assert classify(raw(text="Revenue\nQ1 Q2 Q3"), CFG)[0] == OK


def test_error_phrase_is_error_and_title_is_stripped():
    status, text = classify(raw(text="Revenue\nCan't display this visual.\nSee details", graphics=0), CFG)
    assert status == ERROR
    assert not text.startswith("Revenue")


def test_title_alone_never_triggers_error():
    # A visual titled with an error phrase is still fine.
    assert classify(raw(title="No data", text="No data\n42"), CFG)[0] == OK


def test_spinner_only_times_out_after_max_wait():
    item = raw(spinner=True, graphics=0, text="Revenue")
    assert classify(item, CFG, timed_out=True)[0] == TIMEOUT
    assert classify(item, CFG, timed_out=False)[0] != TIMEOUT


def test_blank_markers():
    assert classify(raw(text="Revenue\n(Blank)", graphics=0), CFG)[0] == BLANK
    assert classify(raw(text="Revenue", graphics=0), CFG)[0] == BLANK


def test_decorative_and_hidden_visuals_are_ignored():
    assert is_ignored(raw(visual_type="shape"), CFG)
    assert is_ignored(raw(width=0, height=0), CFG)
    assert not is_ignored(raw(visual_type="card"), CFG)


def test_clean_details_drops_dialog_chrome():
    text = "Error details\n\nThe field 'X' was deleted.\n\nClose"
    assert clean_details(text, CFG) == "The field 'X' was deleted."
