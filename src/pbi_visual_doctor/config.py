"""Load default settings and merge user overrides."""

from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path
from typing import Any, Optional

import yaml


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_defaults() -> dict[str, Any]:
    text = resources.files("pbi_visual_doctor").joinpath("defaults.yaml").read_text("utf-8")
    return yaml.safe_load(text)


def load_config(path: Optional[str | Path] = None, overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Defaults, then the user's YAML file, then programmatic overrides."""
    config = load_defaults()
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            config = _deep_merge(config, yaml.safe_load(fh) or {})
    if overrides:
        config = _deep_merge(config, overrides)
    return config


def load_targets(path: str | Path) -> dict[str, Any]:
    """Read the targets file (reports, pages and workspaces to scan)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    base = Path(path).resolve().parent
    for report in data.get("reports", []) or []:
        report["url"] = _resolve_url(report.get("url", ""), base)
        pages = []
        for page in report.get("pages", []) or []:
            if isinstance(page, dict):
                page = dict(page)
                if page.get("url"):
                    page["url"] = _resolve_url(page["url"], base)
            pages.append(page)
        report["pages"] = pages
    return data


def _resolve_url(url: str, base: Path) -> str:
    """Allow relative file paths in targets files (used by the demo)."""
    if not url or "://" in url:
        return url
    return (base / url).resolve().as_uri()
