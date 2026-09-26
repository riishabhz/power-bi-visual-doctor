"""Decision backends. All speak the System One request/response format.

- JevBackend / HttpBackend: POST {base_url}/v1/systemone (TypeSafe Jev, or a
  self-hosted laya-serve, which uses the same wire format).
- LayaLocalBackend: runs Laya in-process with laya.Router (no server, no key).
- RulesBackend: keyword baseline for offline demos and tests. Not a model.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Optional

import httpx

from .triage import CATEGORIES, SEVERITY_LEVELS

RETRY_STATUS = {429, 500, 502, 503, 504, 529}


class BackendError(RuntimeError):
    pass


class HttpBackend:
    """Any server implementing POST /v1/systemone."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        name: str = "http",
        concurrency: int = 4,
        max_retries: int = 5,
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.transport = transport
        self.backoff = 1.0  # set to 0 in tests
        self.url = base_url.rstrip("/") + "/v1/systemone"
        self.api_key = api_key
        self.model = model
        self.name = name
        self.concurrency = concurrency
        self.max_retries = max_retries
        self.timeout = timeout

    async def _one(self, client: httpx.AsyncClient, request: dict[str, Any]) -> dict[str, Any]:
        body = dict(request)
        if self.model:
            body["model"] = self.model
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.post(self.url, json=body)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise BackendError(f"{self.name}: network error: {exc}") from exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code not in RETRY_STATUS or attempt == self.max_retries:
                    raise BackendError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:300]}")
            await asyncio.sleep(self.backoff * delay + random.random() * 0.5 * self.backoff)
            delay = min(delay * 2, 30)
        raise BackendError(f"{self.name}: retries exhausted")

    async def evaluate(self, requests: list[dict[str, Any]]) -> list[Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        sem = asyncio.Semaphore(self.concurrency)
        async with httpx.AsyncClient(headers=headers, timeout=self.timeout, transport=self.transport) as client:

            async def guarded(req: dict[str, Any]) -> Any:
                async with sem:
                    try:
                        return await self._one(client, req)
                    except BackendError as exc:
                        print(f"  ! {exc}")
                        return exc

            return await asyncio.gather(*(guarded(r) for r in requests))


class JevBackend(HttpBackend):
    """TypeSafe's hosted Jev. Pin the model version so results are repeatable."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None, **kw: Any) -> None:
        key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise BackendError("Set TYPESAFE_API_KEY to use the Jev backend.")
        super().__init__(
            base_url=base_url or os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai"),
            api_key=key,
            model=model or os.environ.get("JEV_MODEL", "jev-1.13.0"),
            name="jev",
            **kw,
        )


class LayaHttpBackend(HttpBackend):
    """A self-hosted laya-serve instance (pip install 'laya[serve]'; laya-serve)."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, model: Optional[str] = None, **kw: Any) -> None:
        super().__init__(
            base_url=base_url or os.environ.get("LAYA_BASE_URL", "http://localhost:8000"),
            api_key=api_key or os.environ.get("LAYA_API_KEY"),
            model=model or os.environ.get("LAYA_MODEL"),
            name="laya-http",
            **kw,
        )


class LayaLocalBackend:
    """Laya in-process. Needs: pip install 'pbi-visual-doctor[laya]'."""

    name = "laya"

    def __init__(self, model: Optional[str] = None, batch_size: int = 16, device: Optional[str] = None) -> None:
        try:
            from laya import Router  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise BackendError("Laya is not installed. Run: pip install 'pbi-visual-doctor[laya]'") from exc
        kwargs: dict[str, Any] = {}
        if device:
            kwargs["device"] = device
        self.router = Router(**kwargs)
        # Error messages from Power BI are English; pin the English checkpoint
        # unless told otherwise (the multilingual one has a score-position bias).
        self.model = model or os.environ.get("LAYA_MODEL", "english")
        self.batch_size = batch_size

    async def evaluate(self, requests: list[dict[str, Any]]) -> list[Any]:
        items = [dict(r, model=self.model) for r in requests]
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None, lambda: self.router.predict_batch(items, batch_size=self.batch_size)
        )
        out = []
        for res in results:
            res = dict(res)
            res.setdefault("model", f"laya/{res.get('routing', {}).get('model', self.model)}")
            out.append(res)
        return out


class RulesBackend:
    """Keyword baseline. Useful offline and as a sanity check for the models.

    It returns answers in the same shape as Jev and Laya, so the rest of the
    pipeline cannot tell the difference. It is not a model: use it to try the
    tool, not to make decisions.
    """

    name = "rules"

    # Order matters: the first matching category wins.
    KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
        ("missing_field", ("can't find", "cannot find", "doesn't exist", "does not exist", "renamed or deleted", "one or more fields")),
        ("permissions", ("don't have permission", "do not have permission", "not authorized", "unauthorized", "access denied", "row-level", "sensitivity label", "build permission")),
        ("credentials_gateway", ("credential", "gateway", "sign in to the data source", "data source", "odbc", "oauth", "token expired", "directquery")),
        ("resource_limit", ("exceeded", "timeout", "timed out", "too many", "memory", "resources", "capacity", "row limit")),
        ("custom_visual", ("custom visual", "not certified", "this visual type", "admin has disabled", "visual failed to load", "appsource")),
        ("refresh_failure", ("refresh", "not been processed", "processing", "stale")),
        ("relationship", ("relationship", "ambiguous", "many-to-many", "cross filter", "cross-filter")),
        ("dax_error", ("dax", "syntax", "function", "argument", "circular", "cannot convert", "calculation", "expression", "divide")),
        ("missing_field", ("renamed", "deleted", "missing")),
    ]

    OWNER = {
        "missing_field": "report_author",
        "dax_error": "model_owner",
        "relationship": "model_owner",
        "refresh_failure": "model_owner",
        "permissions": "platform_admin",
        "credentials_gateway": "platform_admin",
        "resource_limit": "model_owner",
        "custom_visual": "report_author",
        "not_broken": "report_author",
        "other": "report_author",
    }

    @staticmethod
    def _choice(options: list[str], picked: str, confidence: float) -> dict[str, Any]:
        rest = (1.0 - confidence) / max(1, len(options) - 1)
        probs = {o: (confidence if o == picked else rest) for o in options}
        return {"type": "choice", "choice": picked, "probabilities": probs, "confidence": confidence}

    def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        state = request["state"]
        status = state.get("status", "error")
        text = f"{state.get('message', '')} {state.get('details', '')}".lower()

        category, conf = "other", 0.4
        for cat, words in self.KEYWORDS:
            if any(w in text for w in words):
                category, conf = cat, 0.7
                break
        if status == "timeout":
            category, conf = "resource_limit", 0.6

        broken_p = None
        if status == "blank":
            if "no content" in text:
                broken_p = 0.8
            elif "(blank)" in text:
                broken_p = 0.55
            else:
                broken_p = 0.25
            if broken_p < 0.5:
                category, conf = "not_broken", 0.6
            elif category == "other":
                category = "refresh_failure"

        severity = {"error": 2.0, "timeout": 1.5, "blank": 1.0}.get(status, 1.0)
        if category == "not_broken":
            severity = 0.0
        title = state.get("visual_title", "").lower()
        if any(k in title for k in ("kpi", "total", "revenue")) and category != "not_broken":
            severity = min(3.0, severity + 1)

        answers: dict[str, Any] = {
            "category": self._choice(list(CATEGORIES), category, conf),
            "owner": self._choice(["report_author", "model_owner", "platform_admin"], self.OWNER[category], conf),
            "severity": {
                "type": "score",
                "score": severity,
                "legend": {str(i): lvl for i, lvl in enumerate(SEVERITY_LEVELS)},
                "probabilities": {str(i): (1.0 if i == int(round(severity)) else 0.0) for i in range(len(SEVERITY_LEVELS))},
                "confidence": conf,
            },
        }
        if broken_p is not None:
            answers["is_broken"] = {"type": "noul", "noul": broken_p}
        return {"model": "rules-baseline", "answers": answers, "usage": {"input_tokens": 0, "output_tokens": 0}}

    async def evaluate(self, requests: list[dict[str, Any]]) -> list[Any]:
        return [self._answer(r) for r in requests]


def make_backend(name: str, **kwargs: Any) -> Any:
    name = (name or "rules").lower()
    if name == "jev":
        return JevBackend(**kwargs)
    if name in ("laya", "laya-local"):
        return LayaLocalBackend(**kwargs)
    if name in ("laya-http", "laya-serve"):
        return LayaHttpBackend(**kwargs)
    if name == "rules":
        return RulesBackend()
    raise BackendError(f"Unknown backend '{name}'. Use one of: jev, laya, laya-http, rules.")
