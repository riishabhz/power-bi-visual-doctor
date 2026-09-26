"""Aggregate findings and write the HTML, JSON and CSV health report."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

from .models import OK, TIMEOUT, Finding, ScanResult
from .triage import CATEGORIES, OWNERS

CATEGORY_LABELS = {
    "missing_field": "Missing field or measure",
    "dax_error": "DAX error",
    "relationship": "Relationship issue",
    "permissions": "Permissions / RLS",
    "credentials_gateway": "Credentials / gateway",
    "refresh_failure": "Refresh failure",
    "resource_limit": "Timeout / resource limit",
    "custom_visual": "Custom visual",
    "not_broken": "Not broken",
    "other": "Other",
    "untriaged": "Untriaged",
}
OWNER_LABELS = {"report_author": "Report author", "model_owner": "Model owner", "platform_admin": "Platform admin"}


def summarise(scan: ScanResult, findings: list[Finding], review_threshold: float = 0.6) -> dict[str, Any]:
    pages = {(v.report_name, v.page_name) for v in scan.visuals}
    reports = {v.report_name for v in scan.visuals} | {e["report"] for e in scan.page_errors}
    broken = [f for f in findings if f.is_confirmed_broken]
    cleared = [f for f in findings if not f.is_confirmed_broken]

    def needs_review(f: Finding) -> bool:
        return f.triage is None or f.triage.category_confidence < review_threshold

    by_category = Counter((f.triage.category if f.triage else "untriaged") for f in broken)
    by_owner = Counter((f.triage.owner if f.triage else "unknown") for f in broken)

    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in broken:
        groups[f.fingerprint].append(f)
    top_fixes = []
    for fp, items in groups.items():
        first = items[0]
        cat = first.triage.category if first.triage else "untriaged"
        owners = Counter(f.triage.owner for f in items if f.triage)
        top_fixes.append({
            "fingerprint": fp,
            "category": cat,
            "category_label": CATEGORY_LABELS.get(cat, cat),
            "owner": OWNER_LABELS.get(owners.most_common(1)[0][0], "") if owners else "",
            "names": first.names,
            "visuals": len(items),
            "reports": len({f.visual.report_name for f in items}),
            "pages": len({(f.visual.report_name, f.visual.page_name) for f in items}),
            "max_severity": max((f.triage.severity for f in items if f.triage), default=0.0),
            "sample": (first.visual.details_text or first.visual.error_text)[:300],
            "where": sorted({f"{f.visual.report_name} / {f.visual.page_name}" for f in items})[:6],
        })
    top_fixes.sort(key=lambda t: (-t["visuals"], -t["max_severity"]))

    per_report: dict[str, dict[str, Any]] = {}
    for v in scan.visuals:
        rep = per_report.setdefault(v.report_name, {"name": v.report_name, "pages": {}, "broken": 0, "visuals": 0})
        page = rep["pages"].setdefault(v.page_name, {"name": v.page_name, "url": v.page_url, "visuals": 0, "ok": 0, "findings": []})
        page["visuals"] += 1
        rep["visuals"] += 1
        if v.status == OK:
            page["ok"] += 1
    for f in findings:
        rep = per_report[f.visual.report_name]
        rep["pages"][f.visual.page_name]["findings"].append(f)
        if f.is_confirmed_broken:
            rep["broken"] += 1
    report_list = []
    for rep in per_report.values():
        rep["pages"] = list(rep["pages"].values())
        for page in rep["pages"]:
            page["broken"] = sum(1 for f in page["findings"] if f.is_confirmed_broken)
            page["findings"].sort(key=lambda f: (not f.is_confirmed_broken, -(f.triage.severity if f.triage else 0)))
        report_list.append(rep)
    report_list.sort(key=lambda r: (-r["broken"], r["name"]))

    return {
        "totals": {
            "reports": len(reports),
            "pages": len(pages),
            "visuals": len(scan.visuals),
            "ok": sum(1 for v in scan.visuals if v.status == OK),
            "triaged": len(findings),
            "broken": len(broken),
            "cleared": len(cleared),
            "timeouts": sum(1 for f in broken if f.visual.status == TIMEOUT),
            "needs_review": sum(1 for f in broken if needs_review(f)),
            "page_errors": len(scan.page_errors),
            "reports_affected": len({f.visual.report_name for f in broken}),
        },
        "by_category": [
            {"key": k, "label": CATEGORY_LABELS.get(k, k), "count": c} for k, c in by_category.most_common()
        ],
        "by_owner": [{"key": k, "label": OWNER_LABELS.get(k, k), "count": c} for k, c in by_owner.most_common()],
        "top_fixes": top_fixes,
        "reports": report_list,
        "page_errors": scan.page_errors,
        "review_threshold": review_threshold,
    }


def _env() -> Environment:
    env = Environment(autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.globals.update(CATEGORY_LABELS=CATEGORY_LABELS, OWNER_LABELS=OWNER_LABELS)
    return env


def render_html(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    template_text = resources.files("pbi_visual_doctor").joinpath("templates/report.html").read_text("utf-8")
    return _env().from_string(template_text).render(s=summary, meta=meta)


def write_outputs(out_dir: str | Path, scan: ScanResult, findings: list[Finding], backend_name: str, review_threshold: float = 0.6) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarise(scan, findings, review_threshold)
    meta = {
        "backend": backend_name,
        "models": sorted({f.triage.model for f in findings if f.triage and f.triage.model}),
        "started_at": scan.started_at,
        "finished_at": scan.finished_at,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    paths = {
        "html": out / "report.html",
        "json": out / "findings.json",
        "csv": out / "findings.csv",
    }
    paths["html"].write_text(render_html(summary, meta), encoding="utf-8")

    json_summary = {k: v for k, v in summary.items() if k != "reports"}
    paths["json"].write_text(
        json.dumps({"meta": meta, "summary": json_summary, "findings": [f.to_dict() for f in findings]}, indent=2),
        encoding="utf-8",
    )

    with paths["csv"].open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "report", "page", "visual_index", "visual_title", "visual_type", "status", "confirmed_broken",
            "category", "category_confidence", "owner", "severity", "severity_label", "broken_probability",
            "fingerprint", "message", "details", "page_url", "backend", "model",
        ])
        for f in findings:
            v, t = f.visual, f.triage
            writer.writerow([
                v.report_name, v.page_name, v.visual_index, v.title, v.visual_type, v.status, f.is_confirmed_broken,
                t.category if t else "", t.category_confidence if t else "", t.owner if t else "",
                t.severity if t else "", t.severity_label if t else "",
                "" if not t or t.broken_probability is None else t.broken_probability,
                f.fingerprint, v.error_text[:500], v.details_text[:1000], v.page_url,
                t.backend if t else "", t.model if t else "",
            ])
    return paths
