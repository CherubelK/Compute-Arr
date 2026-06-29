"""
Unit tests for RunPodAdapter. All HTTP calls are mocked — no real API hits.
"""
import json

import httpx
import pytest

from app.providers.base import JobSpec, ProviderError
from app.providers.runpod import RunPodAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_client(responses: list[dict]) -> httpx.Client:
    """Return an httpx.Client whose transport replays `responses` in order."""
    idx = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = responses[idx["i"]]
        idx["i"] += 1
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def _gql_ok(data: dict) -> dict:
    return {"data": data}


def _gql_error(msg: str) -> dict:
    return {"errors": [{"message": msg}]}


# ---------------------------------------------------------------------------
# get_offers
# ---------------------------------------------------------------------------

def test_get_offers_returns_offer_for_matching_gpu():
    payload = _gql_ok({
        "gpuTypes": [
            {
                "id": "NVIDIA H100 SXM",
                "displayName": "H100 SXM",
                "lowestPrice": {"uninterruptablePrice": 3.49, "minimumBidPrice": 2.80},
            },
            {
                "id": "NVIDIA A100-SXM4-80GB",
                "displayName": "A100",
                "lowestPrice": {"uninterruptablePrice": 1.99, "minimumBidPrice": 1.50},
            },
        ]
    })
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([payload]))
    offers = adapter.get_offers("H100")

    assert len(offers) == 1
    assert offers[0].provider == "runpod"
    assert offers[0].gpu_class == "H100"
    assert offers[0].price_per_hr == 3.49
    assert offers[0].available is True


def test_get_offers_no_match_returns_empty():
    payload = _gql_ok({"gpuTypes": []})
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([payload]))
    offers = adapter.get_offers("H100")
    assert offers == []


def test_get_offers_unknown_gpu_class_raises():
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([]))
    with pytest.raises(ProviderError, match="Unknown GPU class"):
        adapter.get_offers("UNKNOWNGPU")


def test_get_offers_graphql_error_raises():
    payload = _gql_error("Unauthorized")
    adapter = RunPodAdapter(api_key="bad", http_client=_mock_client([payload]))
    with pytest.raises(ProviderError, match="GraphQL error"):
        adapter.get_offers("H100")


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def test_submit_returns_pod_id():
    payload = _gql_ok({"podFindAndDeployOnDemand": {"id": "abc123"}})
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([payload]))
    spec = JobSpec(gpu_class="H100", preference="cheapest", image="pytorch/pytorch:latest")
    pod_id = adapter.submit(spec)
    assert pod_id == "abc123"


def test_submit_passes_env_vars():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["env"] = body["variables"]["input"]["env"]
        return httpx.Response(200, json=_gql_ok({"podFindAndDeployOnDemand": {"id": "xyz"}}))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = RunPodAdapter(api_key="test", http_client=client)
    spec = JobSpec(
        gpu_class="A100",
        preference="cheapest",
        image="nvcr.io/nvidia/pytorch:23.10-py3",
        env_vars={"MY_VAR": "hello"},
    )
    adapter.submit(spec)
    assert {"key": "MY_VAR", "value": "hello"} in captured["env"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_running():
    payload = _gql_ok({
        "pod": {
            "id": "abc123",
            "desiredStatus": "RUNNING",
            "costPerHr": 3.49,
            "runtime": {"uptimeInSeconds": 3600},
        }
    })
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([payload]))
    s = adapter.status("abc123")
    assert s.state == "running"
    assert s.cost_so_far == pytest.approx(3.49, rel=1e-3)


def test_status_not_found_raises():
    payload = _gql_ok({"pod": None})
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([payload]))
    with pytest.raises(ProviderError, match="not found"):
        adapter.status("missing")


def test_status_exited_maps_to_succeeded():
    payload = _gql_ok({
        "pod": {"id": "p1", "desiredStatus": "EXITED", "costPerHr": None, "runtime": None}
    })
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([payload]))
    s = adapter.status("p1")
    assert s.state == "succeeded"
    assert s.cost_so_far is None


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

def test_cancel_calls_stop_mutation():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["query"] = body["query"]
        captured["vars"] = body["variables"]
        return httpx.Response(200, json=_gql_ok({"podStop": {"id": "abc", "desiredStatus": "TERMINATED"}}))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = RunPodAdapter(api_key="test", http_client=client)
    adapter.cancel("abc")
    assert "podStop" in captured["query"]
    assert captured["vars"]["podId"] == "abc"


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

def test_logs_returns_string():
    payload = _gql_ok({"pod": {"logs": "epoch 1/10 loss=0.42\n"}})
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([payload]))
    logs = adapter.logs("abc123")
    assert "epoch 1/10" in logs


def test_logs_missing_pod_returns_empty():
    payload = _gql_ok({"pod": None})
    adapter = RunPodAdapter(api_key="test", http_client=_mock_client([payload]))
    assert adapter.logs("gone") == ""
