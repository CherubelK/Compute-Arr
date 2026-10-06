"""Unit tests for VastAdapter. All HTTP calls mocked."""
import json

import httpx
import pytest

from app.providers.base import JobSpec, ProviderError
from app.providers.vast import VastAdapter


def _mock_client(responses: list[dict]) -> httpx.Client:
    idx = {"i": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        body = responses[idx["i"]]
        idx["i"] += 1
        return httpx.Response(200, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# get_offers
# ---------------------------------------------------------------------------

def test_get_offers_filters_by_gpu_class():
    payload = {"offers": [
        {"id": 1, "gpu_name": "RTX 4090", "dph_total": 0.35, "rentable": True, "geolocation": "US, TX"},
        {"id": 2, "gpu_name": "RTX 3090", "dph_total": 0.20, "rentable": True, "geolocation": "EU"},
    ]}
    adapter = VastAdapter(api_key="test", http_client=_mock_client([payload]))
    offers = adapter.get_offers("RTX4090")
    assert len(offers) == 1
    assert offers[0].price_per_hr == 0.35
    assert offers[0].raw_offer_id == "1"


def test_get_offers_unknown_class_raises():
    adapter = VastAdapter(api_key="test", http_client=_mock_client([]))
    with pytest.raises(ProviderError, match="Unknown GPU class"):
        adapter.get_offers("UNKNOWNGPU")


def test_get_offers_http_error_raises():
    def handler(req):
        return httpx.Response(401, text="Unauthorized")
    adapter = VastAdapter(api_key="bad", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="HTTP 401"):
        adapter.get_offers("H100")


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def test_submit_rents_cheapest_offer():
    search_payload = {"offers": [
        {"id": 10, "gpu_name": "H100", "dph_total": 3.0, "rentable": True},
        {"id": 20, "gpu_name": "H100", "dph_total": 2.5, "rentable": True},
    ]}
    rent_payload = {"success": True, "new_contract": 99}
    captured = {}

    def handler(req: httpx.Request):
        if req.method == "PUT":
            captured["url"] = str(req.url)
            body = json.loads(req.content)
            captured["body"] = body
            return httpx.Response(200, json=rent_payload)
        return httpx.Response(200, json=search_payload)

    adapter = VastAdapter(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    spec = JobSpec(gpu_class="H100", preference="cheapest", image="pytorch/pytorch:latest")
    contract_id = adapter.submit(spec)
    assert contract_id == "99"
    assert "/asks/20/" in captured["url"]  # cheapest offer (id=20) was rented


def test_submit_no_offers_raises():
    payload = {"offers": []}
    adapter = VastAdapter(api_key="test", http_client=_mock_client([payload]))
    spec = JobSpec(gpu_class="H100", preference="cheapest", image="nginx")
    with pytest.raises(ProviderError, match="No rentable"):
        adapter.submit(spec)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_running():
    payload = {"instances": [{"actual_status": "running", "cost_per_hr": 0.35, "duration": 7200.0}]}
    adapter = VastAdapter(api_key="test", http_client=_mock_client([payload]))
    s = adapter.status("99")
    assert s.state == "running"
    assert s.cost_so_far == pytest.approx(0.35 * 7200 / 3600, rel=1e-3)


def test_status_null_while_provisioning_is_pending():
    payload = {"instances": [{"actual_status": None, "cost_per_hr": 0.35, "duration": None}]}
    adapter = VastAdapter(api_key="test", http_client=_mock_client([payload]))
    assert adapter.status("99").state == "pending"


def test_status_not_found_raises():
    payload = {"instances": []}
    adapter = VastAdapter(api_key="test", http_client=_mock_client([payload]))
    with pytest.raises(ProviderError, match="not found"):
        adapter.status("missing")


# ---------------------------------------------------------------------------
# cancel / logs
# ---------------------------------------------------------------------------

def test_cancel_sends_delete():
    captured = {}
    def handler(req: httpx.Request):
        captured["method"] = req.method
        return httpx.Response(200, json={"success": True})
    adapter = VastAdapter(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.cancel("99")
    assert captured["method"] == "DELETE"


def test_logs_returns_text():
    payload = {"instances": [{"logs": "training started\n", "actual_status": "running"}]}
    adapter = VastAdapter(api_key="test", http_client=_mock_client([payload]))
    assert "training started" in adapter.logs("99")
