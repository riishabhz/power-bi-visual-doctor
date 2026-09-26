"""Decide a visual's status from the text the crawler extracted.

This stage is deliberately rule-based: it only decides whether a visual is
worth sending to Jev or Laya. Working out *why* it is broken is the model's job.
"""

from __future__ import annotations

from typing import Any, Optional

from .models import BLANK, ERROR, OK, TIMEOUT


def _contains_any(text: str, phrases: list[str]) -> Optional[str]:
    low = text.lower()
    for phrase in phrases:
        if phrase.lower() in low:
            return phrase
    return None


def is_ignored(raw: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Decorative visuals (shapes, images, text boxes) are never checked."""
    vtype = (raw.get("visual_type") or "").lower()
    ignore = [t.lower() for t in cfg["phrases"].get("ignore_types", [])]
    if vtype and vtype in ignore:
        return True
    # Zero-size containers are hidden visuals (bookmarks, collapsed panes).
    if raw.get("width", 1) < 2 or raw.get("height", 1) < 2:
        return True
    return False


def body_text(raw: dict[str, Any]) -> str:
    """Visual text with the title removed, so titles never trigger rules."""
    text = (raw.get("text") or "").strip()
    title = (raw.get("title") or "").strip()
    if title and text.startswith(title):
        text = text[len(title):].strip()
    return text


def clean_details(text: str, cfg: dict[str, Any]) -> str:
    """Drop dialog chrome lines (headings, buttons) from captured error details."""
    chrome = {c.lower() for c in cfg["phrases"].get("dialog_chrome", [])}
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln and ln.lower() not in chrome)


def classify(raw: dict[str, Any], cfg: dict[str, Any], timed_out: bool = False) -> tuple[str, str]:
    """Return (status, error_text) for one extracted visual."""
    text = body_text(raw)
    phrases = cfg["phrases"]

    if _contains_any(text, phrases.get("error", [])):
        return ERROR, text
    if raw.get("spinner") and timed_out:
        return TIMEOUT, text or "Visual was still loading when the render timeout expired."
    if _contains_any(text, phrases.get("blank", [])):
        return BLANK, text
    # A titled visual that drew nothing and says nothing.
    if raw.get("title") and not text and not raw.get("graphics"):
        return BLANK, "Visual rendered with no content."
    return OK, ""
