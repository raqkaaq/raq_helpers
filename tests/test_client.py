"""Transport defaults, per-instance configuration, lifecycle, and real cache I/O."""

import importlib
import re
from unittest.mock import AsyncMock

import httpx2
import pytest
from hishel import Request, Response

from raq_helpers.client import Client
from raq_helpers.client.cache_filters import MethodFilter, StatusCodeFilter, URLPatternFilter

module = importlib.import_module("raq_helpers.client.client")


def options(tmp_path):
    return dict(url="https://example.test", cache_db_path=tmp_path / "cache.db", cache_ttl=60)


@pytest.mark.asyncio
async def test_http_defaults_and_lifecycle(tmp_path):
    client = Client(**options(tmp_path), headers={"X-Test": "header"}, params={"label": "default"})
    async with client as entered:
        assert entered is client
        assert not client.client.is_closed
        assert client.client.headers["X-Test"] == "header"
        assert client.client.params["label"] == "default"
        assert client.client.max_redirects == 10
        assert client.client.timeout.connect == 60
        assert client.client.timeout.read == client.client.timeout.write == client.client.timeout.pool == 10
        assert isinstance(client.policy.request_filters[0], URLPatternFilter)
        assert isinstance(client.policy.request_filters[1], MethodFilter)
        assert isinstance(client.policy.response_filters[0], StatusCodeFilter)
    assert client.client.is_closed
    await client.aclose()


@pytest.mark.asyncio
async def test_context_exception_closes_and_preserves_error(tmp_path):
    client = Client(**options(tmp_path))
    failure = RuntimeError("body failure")
    with pytest.raises(RuntimeError) as error:
        async with client:
            raise failure
    assert error.value is failure and client.client.is_closed


@pytest.mark.asyncio
async def test_subclass_defaults_and_explicit_overrides(tmp_path):
    class Custom(Client):
        TIMEOUT = httpx2.Timeout(3, connect=8)
        MAX_REDIRECTS = 2
        CACHE_METHODS = ("GET", "POST")
        CACHE_STATUS_CODES = (201,)
        CACHE_EXCLUDE_PATTERNS = ("private",)
    async with Custom(**options(tmp_path)) as client:
        assert client.client.timeout.read == 3 and client.client.timeout.connect == 8
        assert client.client.max_redirects == 2
        assert client.policy.request_filters[1].apply(Request("POST", "https://example.test/items"), None)
        assert not client.policy.request_filters[0].apply(Request("GET", "https://example.test/private"), None)
        assert client.policy.response_filters[0].apply(Response(201), None)
        assert not client.policy.response_filters[0].apply(Response(200), None)
    async with Custom(**options(tmp_path), timeout=None, max_redirects=0, request_filters=[], response_filters=[]) as client:
        assert client.client.timeout.read is None and client.client.max_redirects == 0
        assert client.policy.request_filters == client.policy.response_filters == []
    async with Custom(**options(tmp_path), timeout=1.5) as client:
        assert client.client.timeout.connect == 1.5
    assert Client.CACHE_METHODS == ("GET",)


@pytest.mark.asyncio
async def test_instances_do_not_share_filters_transport_or_timeouts(tmp_path):
    async with Client(**options(tmp_path)) as first, Client(**options(tmp_path)) as second:
        assert first.storage is not second.storage
        assert first.transport is not second.transport
        assert first.client is not second.client
        first.policy.request_filters[1].methods.append("DELETE")
        first.client.timeout.read = 99
        assert second.policy.request_filters[1].methods == ["GET"]
        assert second.client.timeout.read == Client.TIMEOUT.read == 10
        await second.aclose()
        assert not first.client.is_closed


@pytest.mark.asyncio
async def test_explicit_filters_take_precedence(tmp_path):
    requests, responses = [MethodFilter(["HEAD"])], [StatusCodeFilter([202])]
    async with Client(**options(tmp_path), request_filters=requests, response_filters=responses) as client:
        assert client.policy.request_filters == requests
        assert client.policy.response_filters == responses


@pytest.mark.parametrize("base", ["https://example.test", "https://example.test/"])
@pytest.mark.parametrize("path,allowed", [("/query", False), ("/query?x=1", False), ("/query/sub", False), ("/query#fragment", False), ("/querying", True), ("/items", True), ("/", True)])
def test_exclusion_boundaries(base, path, allowed):
    class Custom(Client):
        CACHE_EXCLUDE_PATTERNS = ("query",)
    assert bool(re.match(Custom.cache_url_pattern(base), base.rstrip("/") + path)) is allowed


@pytest.mark.parametrize("url", ["https://exampleXtest/items", "https://example.test.other/items", "https://other.test/items"])
def test_patterns_escape_host_and_reject_other_hosts(url):
    assert not re.match(Client.cache_url_pattern("https://example.test"), url)


def test_exclusions_are_literal_paths_and_respect_base_path():
    class Custom(Client):
        CACHE_EXCLUDE_PATTERNS = ("a.b",)
    pattern = Custom.cache_url_pattern("https://example.test/v1/")
    assert not re.match(pattern, "https://example.test/v1/a.b")
    assert re.match(pattern, "https://example.test/v1/axb")
    assert not re.match(pattern, "https://example.test/v2/items")


@pytest.mark.asyncio
async def test_real_sqlite_cache_reuses_entries_and_persists(tmp_path, monkeypatch):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx2.Response(200, json={"query": request.url.params.get("q")}, headers={"Cache-Control": "public, max-age=3600"})
    monkeypatch.setattr(module.httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handle))
    async with Client(**options(tmp_path)) as client:
        for query in ("one", "two", "one"):
            response = await client.client.get("/items", params={"q": query})
            assert response.json() == {"query": query}
        assert response.extensions["hishel_from_cache"]
    async with Client(**options(tmp_path)) as client:
        response = await client.client.get("/items", params={"q": "one"})
        assert response.extensions["hishel_from_cache"]
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
async def test_error_statuses_are_not_cached(tmp_path, monkeypatch, status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx2.Response(status, json={"message": "error"})
    monkeypatch.setattr(module.httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handle))
    async with Client(**options(tmp_path)) as client:
        for _ in range(2):
            response = await client.client.get("/items")
            assert not response.extensions.get("hishel_from_cache", False)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", [("/private", "GET"), ("/items", "POST")])
async def test_excluded_paths_and_methods_bypass_cache(tmp_path, monkeypatch, path, method):
    class Custom(Client):
        CACHE_EXCLUDE_PATTERNS = ("private",)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx2.Response(200, json={"ok": True})
    monkeypatch.setattr(module.httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handle))
    async with Custom(**options(tmp_path)) as client:
        for _ in range(2):
            response = await client.client.request(method, path)
            assert not response.extensions.get("hishel_from_cache", False)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_post_body_cache_keys_separate_body_path_and_method(tmp_path, monkeypatch):
    class Custom(Client):
        CACHE_METHODS = ("GET", "POST")
    calls = []
    def handle(request):
        identity = [request.method, str(request.url), request.content.decode()]
        calls.append(identity)
        return httpx2.Response(200, json=identity)
    monkeypatch.setattr(module.httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handle))
    combinations = [("POST", "/items", {"id": 1}), ("POST", "/items", {"id": 2}), ("POST", "/other", {"id": 1}), ("GET", "/items", {"id": 1})]
    async with Custom(**options(tmp_path)) as client:
        for repetition in range(2):
            for method, path, body in combinations:
                response = await client.client.request(method, path, json=body, extensions={"hishel_body_key": True})
                assert bool(response.extensions.get("hishel_from_cache")) is bool(repetition)
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_cache_logging_preserves_response_and_redacts_query(caplog, bare_client):
    response = httpx2.Response(200, request=httpx2.Request("GET", "https://example.test/items?token=secret"), extensions={"hishel_from_cache": True, "private": "not-logged"})
    call = AsyncMock(return_value=response)
    with caplog.at_level("DEBUG", logger=module.__name__):
        assert await Client.cache(call)(bare_client, 1, value=2) is response
    call.assert_awaited_once_with(bare_client, 1, value=2)
    assert "hishel_from_cache" in caplog.text
    assert "secret" not in caplog.text and "not-logged" not in caplog.text


@pytest.mark.asyncio
async def test_cache_logging_does_not_swallow_errors(bare_client):
    error = ValueError("original")
    with pytest.raises(ValueError) as caught:
        await Client.cache(AsyncMock(side_effect=error))(bare_client)
    assert caught.value is error
