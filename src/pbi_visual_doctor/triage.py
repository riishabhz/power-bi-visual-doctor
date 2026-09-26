"""The typed questions asked about each problem visual, and answer parsing.

Every backend (Jev, Laya, rules) receives the same System One request:
{"state": ..., "questions": ...} and returns {"model": ..., "answers": ...}.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from .models import BLANK, Finding, Triage, VisualRecord

CATEGORIES: dict[str, str] = {
    "missing_field": "The message says a field, column, measure or table doesn't exist, cannot be found or has been renamed or deleted, so the visual cannot use it",
    "dax_error": "A DAX calculation fails while evaluating: syntax error, invalid function or argument, type mismatch such as cannot convert a value of type Text to Number, or circular dependency",
    "relationship": "Missing, inactive or ambiguous relationships between tables, many-to-many or cross-filter direction problems",
    "permissions": "The viewer lacks access: row-level security, workspace or semantic model permissions, build permission or sensitivity labels",
    "credentials_gateway": "Data source credentials expired or invalid, gateway offline or not configured, DirectQuery or live source unreachable",
    "refresh_failure": "The semantic model refresh failed or the model has not been processed, so data is missing or stale",
    "resource_limit": "The visual exceeded the available resources: the query timed out, used too much memory or capacity, or has too many rows or data points to display",
    "custom_visual": "A custom or third-party visual from AppSource or a .pbiviz file failed to load, is not certified, or has a rendering bug",
    "tenant_setting": "A tenant or admin setting turns this feature off for the whole organization, for example map visuals, R or Python visuals, or export are not enabled for your org; the tenant admin must enable it",
    "not_broken": "The visual works as intended: it is legitimately empty for the current filters or shows a normal informational message",
    "other": "Any other problem that does not fit the categories above",
}

OWNERS: dict[str, str] = {
    "report_author": "The report author can fix it in the report: field wells, visual settings, filters or bookmarks",
    "model_owner": "The semantic model owner must fix it: measures, columns, relationships, RLS roles or refresh",
    "platform_admin": "A Power BI or data platform admin must fix it: gateways, credentials, capacity, tenant settings or access",
}

SEVERITY_LEVELS: list[str] = [
    "Cosmetic: minor display issue, the data is still readable and correct",
    "Partial: some data is missing or could be misleading",
    "Unusable visual: the visual shows no usable information",
    "Critical: a headline KPI or most of the page is unusable",
]


def build_questions(status: str) -> dict[str, dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {
        "category": {
            "type": "choice",
            "instructions": "What is the most likely root cause of this Power BI visual's problem, based on the error message and details?",
            "criteria": CATEGORIES,
        },
        "owner": {
            "type": "choice",
            "instructions": "Who needs to act to fix this visual?",
            "criteria": OWNERS,
        },
        "severity": {
            "type": "score",
            "instructions": "How severe is this problem for someone reading the report?",
            "criteria": SEVERITY_LEVELS,
        },
    }
    if status == BLANK:
        questions["is_broken"] = {
            "type": "noul",
            "instructions": (
                "This visual shows blank or no data. Is it most likely broken, "
                "rather than legitimately empty for the current filters?"
            ),
            "criteria": {
                "true": "Broken: the visual should show data, for example a headline card or chart with an error-like empty state",
                "false": "Legitimately empty: nothing matches the current filters or the value is genuinely blank",
            },
        }
    return questions


def build_state(visual: VisualRecord, max_chars: int = 1500) -> dict[str, str]:
    state = {
        "report": visual.report_name,
        "page": visual.page_name,
        "visual_title": visual.title or "(untitled)",
        "visual_type": visual.visual_type or visual.aria_label[:80] or "(unknown)",
        "status": visual.status,
        "message": visual.error_text[:max_chars],
    }
    if visual.details_text:
        state["details"] = visual.details_text[:max_chars]
    return state


def build_request(visual: VisualRecord, max_chars: int = 1500) -> dict[str, Any]:
    return {"state": build_state(visual, max_chars), "questions": build_questions(visual.status)}


def _level_label(score: float) -> str:
    idx = min(len(SEVERITY_LEVELS) - 1, max(0, int(round(score))))
    return SEVERITY_LEVELS[idx].split(":")[0]


def parse_response(response: dict[str, Any], backend: str) -> Triage:
    answers = response.get("answers", {})
    category = answers.get("category", {})
    owner = answers.get("owner", {})
    severity = answers.get("severity", {})
    broken = answers.get("is_broken")
    score = float(severity.get("score", 0.0))
    return Triage(
        category=category.get("choice", "other"),
        category_confidence=float(category.get("confidence", 0.0)),
        owner=owner.get("choice", "report_author"),
        owner_confidence=float(owner.get("confidence", 0.0)),
        severity=round(score, 2),
        severity_label=_level_label(score),
        broken_probability=round(float(broken["noul"]), 3) if broken else None,
        backend=backend,
        model=str(response.get("model", "")),
    )


# ------------------------------------------------------------- fingerprints
# Curly quotes built from code points so this file stays plain ASCII.
_OPEN_Q = "'\"" + chr(0x2018) + chr(0x201C)
_CLOSE_Q = "'\"" + chr(0x2019) + chr(0x201D)

# 'Table'[Column], Table[Column] or [Measure]
_QUALIFIED = re.compile(r"(?:'([^'\n]{1,60})'|\b([A-Za-z_]\w{0,60}))?\[([^\]\[\n]{1,80})\]")
# 'Name' or "Name", but not the apostrophe in words like couldn't
_QUOTED = re.compile(
    r"(?<![\w])[" + re.escape(_OPEN_Q) + r"]([^\n" + re.escape(_CLOSE_Q) + r"]{1,80}?)["
    + re.escape(_CLOSE_Q) + r"](?![\w])"
)
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NOISE = re.compile(
    r"(can'?t display this visual\.?|cannot display this visual\.?|something went wrong\.?|"
    r"error details|see details|fix this|copy details to clipboard|\bclose\s*$|"
    r"activity id:.*|request id:.*|correlation id:.*|time:.*)",
    re.I,
)


def extract_names(text: str) -> list[str]:
    """Field, measure and table names mentioned in an error message."""
    names: list[str] = []
    taken: list[tuple[int, int]] = []
    for match in _QUALIFIED.finditer(text):
        table = (match.group(1) or match.group(2) or "").strip()
        col = match.group(3).strip()
        names.append(f"{table}[{col}]" if table else f"[{col}]")
        taken.append(match.span())
    for match in _QUOTED.finditer(text):
        if any(start <= match.start() < end for start, end in taken):
            continue
        names.append(match.group(1).strip())
    seen: set[str] = set()
    out = []
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out[:5]


def normalise_message(text: str) -> str:
    text = _GUID.sub("<id>", text)
    text = _NOISE.sub(" ", text)
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text[:120]


def fingerprint(visual: VisualRecord, triage: Optional[Triage]) -> tuple[str, list[str]]:
    category = triage.category if triage else "untriaged"
    text = f"{visual.details_text}\n{visual.error_text}"
    names = extract_names(text)
    if names:
        return f"{category}: {', '.join(names[:3])}", names
    msg = normalise_message(visual.details_text or visual.error_text) or visual.status
    return f"{category}: {msg}", names


# ------------------------------------------------------------------ runner
async def triage_visuals(
    visuals: list[VisualRecord],
    backend: Any,
    cfg: dict[str, Any],
    progress: Callable[[str], None] = print,
) -> list[Finding]:
    """Ask the backend about every problem visual and return findings."""
    problems = [v for v in visuals if v.is_problem]
    if not problems:
        return []
    max_chars = int(cfg.get("triage", {}).get("max_text_chars", 1500))
    requests = [build_request(v, max_chars) for v in problems]
    progress(f"Triaging {len(problems)} problem visuals with backend '{backend.name}'...")
    responses = await backend.evaluate(requests)

    findings: list[Finding] = []
    failed = 0
    for visual, response in zip(problems, responses):
        triage = None
        if isinstance(response, dict) and "answers" in response:
            triage = parse_response(response, backend.name)
        else:
            failed += 1
        fp, names = fingerprint(visual, triage)
        findings.append(Finding(visual=visual, triage=triage, fingerprint=fp, names=names))
    if failed:
        progress(f"  ! {failed} visuals could not be triaged (see errors above); they are listed as 'untriaged'.")
    return findings
