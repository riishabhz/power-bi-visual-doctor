import json

import httpx

from pbi_visual_doctor.backends import HttpBackend, RulesBackend
from pbi_visual_doctor.models import ERROR, VisualRecord
from pbi_visual_doctor.triage import build_request, triage_visuals

OK_BODY = {
    "model": "jev-1.13.0",
    "answers": {
        "category": {"type": "choice", "choice": "dax_error", "probabilities": {}, "confidence": 0.9},
        "owner": {"type": "choice", "choice": "model_owner", "probabilities": {}, "confidence": 0.8},
        "severity": {"type": "score", "score": 2.0, "legend": {}, "probabilities": {}, "confidence": 0.7},
    },
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def visual():
    return VisualRecord("Sales", "Pipeline", "x", 0, title="Rate", status=ERROR,
                        error_text="Can't display this visual.", details_text="Calculation error in measure 'Sales'[Rate]")


async def test_http_backend_sends_system_one_request_and_retries_429():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json=OK_BODY)

    backend = HttpBackend("https://api.example.test", api_key="k", model="jev-1.13.0", transport=httpx.MockTransport(handler))
    backend.backoff = 0
    [result] = await backend.evaluate([build_request(visual())])

    assert result["answers"]["category"]["choice"] == "dax_error"
    assert len(calls) == 2
    sent = json.loads(calls[-1].content)
    assert calls[-1].url.path == "/v1/systemone"
    assert calls[-1].headers["Authorization"] == "Bearer k"
    assert sent["model"] == "jev-1.13.0"
    assert set(sent) == {"state", "questions", "model"}


async def test_http_backend_does_not_retry_validation_errors():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(422, json={"detail": "bad question"})

    backend = HttpBackend("https://api.example.test", transport=httpx.MockTransport(handler))
    backend.backoff = 0
    findings = await triage_visuals([visual()], backend, {}, progress=lambda *_: None)
    assert len(calls) == 1
    assert findings[0].triage is None
    assert findings[0].fingerprint.startswith("untriaged")


async def test_rules_backend_matches_answer_shape():
    [res] = await RulesBackend().evaluate([build_request(visual())])
    assert res["answers"]["category"]["choice"] == "dax_error"
    assert res["answers"]["severity"]["type"] == "score"
