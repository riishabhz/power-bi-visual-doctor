"""Plain data classes shared by the crawler, triage and report stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Visual status values produced by the crawler.
OK = "ok"
ERROR = "error"
BLANK = "blank"
TIMEOUT = "timeout"

PROBLEM_STATUSES = (ERROR, BLANK, TIMEOUT)


@dataclass
class VisualRecord:
    """Everything the crawler could read from one visual, as text."""

    report_name: str
    page_name: str
    page_url: str
    visual_index: int
    title: str = ""
    visual_type: str = ""
    status: str = OK
    error_text: str = ""
    details_text: str = ""
    aria_label: str = ""
    text_sample: str = ""
    page_hidden: bool = False  # not in the report's page tabs, so viewers cannot click to it

    @property
    def is_problem(self) -> bool:
        return self.status in PROBLEM_STATUSES

    @property
    def key(self) -> str:
        return f"{self.report_name} / {self.page_name} / #{self.visual_index}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VisualRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Triage:
    """Typed answers returned by Jev, Laya or the rules backend."""

    category: str
    category_confidence: float
    owner: str
    owner_confidence: float
    severity: float
    severity_label: str
    broken_probability: Optional[float] = None
    backend: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    """A problem visual plus its triage result."""

    visual: VisualRecord
    triage: Optional[Triage] = None
    fingerprint: str = ""
    names: list[str] = field(default_factory=list)

    @property
    def is_confirmed_broken(self) -> bool:
        """Blank visuals only count as broken when the model says so."""
        if self.triage is None:
            return self.visual.status != BLANK
        if self.triage.category == "not_broken":
            return False
        if self.visual.status == BLANK:
            p = self.triage.broken_probability
            return p is not None and p >= 0.5
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "visual": self.visual.to_dict(),
            "triage": self.triage.to_dict() if self.triage else None,
            "fingerprint": self.fingerprint,
            "names": self.names,
            "confirmed_broken": self.is_confirmed_broken,
        }


@dataclass
class ScanResult:
    """Output of a crawl: every visual seen, plus page-level problems."""

    visuals: list[VisualRecord] = field(default_factory=list)
    page_errors: list[dict[str, str]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "page_errors": self.page_errors,
            "visuals": [v.to_dict() for v in self.visuals],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScanResult":
        return cls(
            visuals=[VisualRecord.from_dict(v) for v in data.get("visuals", [])],
            page_errors=list(data.get("page_errors", [])),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
        )
