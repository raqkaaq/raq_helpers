"""Real generated package -> typed factory -> decorators -> cached mock HTTP."""

import importlib

import httpx2
import pytest
from pydantic import ValidationError

from raq_helpers.client import Client, EndpointFactory
from raq_helpers.models import PreparedRequest


@pytest.mark.asyncio
async def test_full_offline_pipeline_with_method_identity_caching_and_empty_responses(generated_api, tmp_path, monkeypatch):
    artifacts = generated_api
    factory = EndpointFactory.from_json(artifacts.endpoints.output_path.read_text(), registry=artifacts.registry)
    class GeneratedClient(Client, artifacts.package.GeneratedEndpoints):
        def endpoint_factory(self, path, method=None):
            return factory.create_endpoint(self, path, method)
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "DELETE":
            return httpx2.Response(204)
        if request.method == "HEAD":
            return httpx2.Response(200)
        if request.url.path == "/inline":
            return httpx2.Response(200, json={"ok": True})
        if request.url.path == "/items/failure":
            return httpx2.Response(400, json={"message": "failure"})
        return httpx2.Response(201 if request.method == "POST" else 200, json={"id": 1}, headers={"Cache-Control": "public, max-age=3600"})
    module = importlib.import_module("raq_helpers.client.client")
    monkeypatch.setattr(module.httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handler))
    async with GeneratedClient(url="https://example.test", cache_db_path=tmp_path / "cache.db", cache_ttl=60) as client:
        with pytest.raises(TypeError, match="Ambiguous"):
            await client.items()
        first = await client.items(http_method="get")
        second = await client.items(http_method="GET")
        assert isinstance(first, artifacts.package.Item) and first == second
        assert len(calls) == 1
        assert (await client.items(http_method="post", label="new")).id == 1
        assert calls[-1].url.params["label"] == "new"
        assert await client.items(http_method="delete") is None
        assert await client.items(http_method="head") is None
        assert (await client.items(id="a/b", **{"search.term": "example"})).id == 1
        assert calls[-1].url.raw_path == b"/items/a%2Fb?search.term=example"
        error = await client.items(id="failure")
        assert error.message == "failure"
        assert (await client.inline()).ok is True
        count = len(calls)
        with pytest.raises(ValueError, match="no request inputs"):
            await client.items(http_method="get", unknown=True)
        with pytest.raises(ValidationError):
            await client.items(http_method="post")
        assert len(calls) == count
    assert client.client.is_closed


@pytest.mark.asyncio
async def test_custom_factory_hooks_use_typed_models_and_preserve_metadata(generated_api, bare_client):
    artifacts = generated_api
    seen = []
    class CustomFactory(EndpointFactory):
        def prepare_request_values(self, endpoint, params):
            assert endpoint.method.method == "POST"
            values = super().prepare_request_values(endpoint, params)
            values.setdefault("label", "injected")
            return values
        def process_endpoint_response(self, response, request):
            assert isinstance(request, PreparedRequest)
            seen.append(request)
            response.extensions["input"] = request.validated_input
            return response
    factory = CustomFactory(artifacts.definitions)
    def handler(request):
        assert request.url.params["label"] == "injected"
        return httpx2.Response(201, json={"id": 2})
    async with httpx2.AsyncClient(base_url="https://example.test", transport=httpx2.MockTransport(handler)) as transport:
        bare_client.client = transport
        result = await factory.create_endpoint(bare_client, "/items", method="POST")()
        assert result.id == 2 and seen[0].validated_input.label == "injected"
