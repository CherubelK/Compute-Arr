"""Unit tests for LambdaAdapter. All HTTP calls mocked."""
import json

import httpx
import pytest

from app.providers.base import JobSpec, ProviderError
from app.providers.lambda_labs import LambdaAdapter


_INSTANCE_TYPES = {
    "gpu_1x_h100_sxm5": {
        "instance_type": {"name": "gpu_1x_h100_sxm5", "price_cents_per_hour": 249},
        "regions_with_capacity_available": [{"name": "us-east-1"}],
    },
    "gpu_1x_a100_sxm4": {
        "instance_type": {"name": "gpu_1x_a100_sxm4", "price_cents_per_hour": 199},
        "regions_with_capacity_available": [],  # no capacity
    },
    "gpu_8x_h100_sxm5": {  # 8-GPU — should be excluded
        "instance_type": {"name": "gpu_8x_h100_sxm5", "price_cents_per_hour": 1999},
        "regions_with_capacity_available": [{"name": "us-east-1"}],
    },
}


def _mock_client(responses: list) -> httpx.Client:
    idx = {"i": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        resp = responses[idx["i"]]
        idx["i"] += 1
        if isinstance(resp, dict):
            return httpx.Response(200, json=resp)
        return resp
    return httpx.Client(transport=httpx.MockTransport(handler), auth=("test", ""))


# ---------------------------------------------------------------------------
# get_offers
# ---------------------------------------------------------------------------

def test_get_offers_returns_h100_only():
    adapter = LambdaAdapter(api_key="test", http_client=_mock_client([{"data": _INSTANCE_TYPES}]))
    offers = adapter.get_offers("H100")
    assert len(offers) == 1
    assert offers[0].price_per_hr == 2.49
    assert offers[0].available is True
    assert offers[0].region == "us-east-1"
    assert offers[0].raw_offer_id == "gpu_1x_h100_sxm5"


def test_get_offers_excludes_8x_instances():
    adapter = LambdaAdapter(api_key="test", http_client=_mock_client([{"data": _INSTANCE_TYPES}]))
    offers = adapter.get_offers("H100")
    assert all("8x" not in (o.raw_offer_id or "") for o in offers)


def test_get_offers_no_capacity_still_returned_but_unavailable():
    adapter = LambdaAdapter(api_key="test", http_client=_mock_client([{"data": _INSTANCE_TYPES}]))
    offers = adapter.get_offers("A100")
    assert len(offers) == 1
    assert offers[0].available is False


def test_get_offers_http_error_raises():
    def handler(req):
        return httpx.Response(403, text="Forbidden")
    adapter = LambdaAdapter(api_key="bad", http_client=httpx.Client(transport=httpx.MockTransport(handler), auth=("bad", "")))
    with pytest.raises(ProviderError, match="HTTP 403"):
        adapter.get_offers("H100")


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def test_submit_launches_instance():
    launch_resp = {"data": {"instance_ids": ["inst-abc"]}}
    captured = {}

    def handler(req: httpx.Request):
        if req.method == "POST":
            captured["body"] = json.loads(req.content)
            return httpx.Response(200, json=launch_resp)
        return httpx.Response(200, json={"data": _INSTANCE_TYPES})

    adapter = LambdaAdapter(api_key="test", ssh_key_names=["mykey"], http_client=httpx.Client(transport=httpx.MockTransport(handler), auth=("test", "")))
    spec = JobSpec(gpu_class="H100", preference="cheapest", image="pytorch/pytorch:latest", command="python train.py")
    instance_id = adapter.submit(spec)

    assert instance_id == "inst-abc"
    assert captured["body"]["instance_type_name"] == "gpu_1x_h100_sxm5"
    assert "mykey" in captured["body"]["ssh_key_names"]


def test_submit_no_capacity_raises():
    def handler(req):
        return httpx.Response(200, json={"data": _INSTANCE_TYPES})

    adapter = LambdaAdapter(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler), auth=("test", "")))
    spec = JobSpec(gpu_class="A100", preference="cheapest", image="nginx")
    with pytest.raises(ProviderError, match="No available"):
        adapter.submit(spec)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_active():
    payload = {"data": {"id": "inst-abc", "status": "active", "instance_type": {"price_cents_per_hour": 249}}}
    adapter = LambdaAdapter(api_key="test", http_client=_mock_client([payload]))
    s = adapter.status("inst-abc")
    assert s.state == "running"


def test_status_terminated_maps_to_cancelled():
    payload = {"data": {"id": "inst-abc", "status": "terminated"}}
    adapter = LambdaAdapter(api_key="test", http_client=_mock_client([payload]))
    assert adapter.status("inst-abc").state == "cancelled"


def test_status_not_found_raises():
    payload = {"data": None}
    adapter = LambdaAdapter(api_key="test", http_client=_mock_client([payload]))
    with pytest.raises(ProviderError, match="not found"):
        adapter.status("missing")


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

def test_cancel_sends_terminate():
    captured = {}
    def handler(req: httpx.Request):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": {}})
    adapter = LambdaAdapter(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler), auth=("test", "")))
    adapter.cancel("inst-abc")
    assert "inst-abc" in captured["body"]["instance_ids"]


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

def test_logs_returns_empty_string():
    adapter = LambdaAdapter(api_key="test", http_client=_mock_client([]))
    assert adapter.logs("inst-abc") == ""
