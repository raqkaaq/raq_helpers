"""Hishel Redis storage exercised offline; SQLite remains the default backend."""

import importlib
from unittest.mock import AsyncMock, Mock

import fakeredis
import fakeredis.aioredis
import httpx2
import pytest
from hishel import AsyncRedisStorage, AsyncSqliteStorage
from redis.exceptions import ConnectionError as RedisConnectionError

from raq_helpers.client import Client

module = importlib.import_module("raq_helpers.client.client")


@pytest.fixture
def redis_setup(monkeypatch):
    server = fakeredis.FakeServer()
    connections = []
    def connect(url, **kwargs):
        assert kwargs == {"decode_responses": False}
        client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
        client.aclose = AsyncMock(wraps=client.aclose)
        connections.append(client)
        return client
    factory = Mock(side_effect=connect)
    monkeypatch.setattr(module.Redis, "from_url", factory)
    return server, connections, factory


def mock_origin(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json={"origin_call": len(calls)})
    monkeypatch.setattr(module.httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handler))
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("redis_url", [None, "", "   "])
async def test_no_redis_url_preserves_sqlite(redis_url, tmp_path, monkeypatch):
    redis_factory = Mock(side_effect=AssertionError("Redis must not be created"))
    monkeypatch.setattr(module.Redis, "from_url", redis_factory)
    async with Client(url="https://example.test", cache_db_path=tmp_path / "cache.db", cache_ttl=60, cache_redis_url=redis_url) as client:
        assert isinstance(client.storage, AsyncSqliteStorage)
    redis_factory.assert_not_called()


def test_sqlite_path_required_without_redis():
    with pytest.raises(ValueError, match="cache_db_path is required"):
        Client(url="https://example.test", cache_ttl=60)


@pytest.mark.asyncio
async def test_redis_precedence_configuration_and_connection_cleanup(redis_setup, monkeypatch, tmp_path):
    _, connections, connection_factory = redis_setup
    storage_factory = Mock(wraps=module.AsyncRedisStorage)
    monkeypatch.setattr(module, "AsyncRedisStorage", storage_factory)
    sqlite_factory = Mock(side_effect=AssertionError("SQLite must not be created"))
    monkeypatch.setattr(module, "AsyncSqliteStorage", sqlite_factory)
    cache_file = tmp_path / "unused.db"
    async with Client(url="https://example.test", cache_db_path=cache_file, cache_ttl=123, cache_redis_url=" redis://localhost:6379/0 ", cache_key_prefix="raqindex", cache_max_response_bytes=1024) as client:
        storage_factory.assert_called_once_with(client=connections[0], ttl=123, key_prefix="raqindex", max_stream_size=1024)
        connection_factory.assert_called_once_with("redis://localhost:6379/0", decode_responses=False)
        assert isinstance(client.storage, AsyncRedisStorage)
        assert not client.client.is_closed
    assert client.client.is_closed
    connections[0].aclose.assert_awaited_once()
    sqlite_factory.assert_not_called()
    assert not cache_file.exists()


@pytest.mark.asyncio
async def test_redis_does_not_require_sqlite_path(redis_setup):
    async with Client(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0") as client:
        assert isinstance(client.storage, AsyncRedisStorage)


@pytest.mark.asyncio
async def test_redis_cache_reuse_and_ttl(redis_setup, monkeypatch):
    _, connections, _ = redis_setup
    calls = mock_origin(monkeypatch)
    async with Client(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0", cache_key_prefix="raqindex") as client:
        first = await client.client.get("/items")
        second = await client.client.get("/items")
        assert first.json() == second.json() == {"origin_call": 1}
        assert second.extensions["hishel_from_cache"]
        keys = await connections[0].keys("*")
        assert keys and all(key.startswith(b"raqindex:") for key in keys)
        for key in keys:
            remaining = await connections[0].pttl(key)
            # Native Hishel retains entries for TTL + its soft-delete buffer.
            assert 0 < remaining <= 240_000
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_prefixes_isolate_keys_and_share_same_prefix_cache(redis_setup, monkeypatch):
    calls = mock_origin(monkeypatch)
    options = dict(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0")
    async with Client(**options, cache_key_prefix="raqindex") as first, Client(**options, cache_key_prefix="another-api") as second, Client(**options, cache_key_prefix="raqindex") as shared:
        assert (await first.client.get("/items")).json() == {"origin_call": 1}
        assert (await second.client.get("/items")).json() == {"origin_call": 2}
        result = await shared.client.get("/items")
        assert result.json() == {"origin_call": 1} and result.extensions["hishel_from_cache"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_entries_fetch_again(redis_setup, monkeypatch):
    calls = mock_origin(monkeypatch)
    clock_module = importlib.import_module("hishel._core._storages._async_redis")
    clock = [clock_module.time()]
    monkeypatch.setattr(clock_module, "time", lambda: clock[0])
    async with Client(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0") as client:
        assert (await client.client.get("/items")).json()["origin_call"] == 1
        clock[0] += 61
        assert (await client.client.get("/items")).json()["origin_call"] == 2
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,origin_calls", [(1, 2), (1024, 1), (None, 1)])
async def test_response_limit_controls_storage_not_delivery(limit, origin_calls, redis_setup, monkeypatch):
    calls = mock_origin(monkeypatch)
    async with Client(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0", cache_max_response_bytes=limit) as client:
        for _ in range(2):
            response = await client.client.get("/items")
            assert response.status_code == 200 and "origin_call" in response.json()
    assert len(calls) == origin_calls


@pytest.mark.asyncio
async def test_redis_failure_never_falls_back_to_sqlite(redis_setup, monkeypatch, tmp_path):
    server, connections, _ = redis_setup
    server.connected = False
    sqlite = Mock(side_effect=AssertionError("Unexpected SQLite fallback"))
    monkeypatch.setattr(module, "AsyncSqliteStorage", sqlite)
    async with Client(url="https://example.test", cache_ttl=60, cache_db_path=tmp_path / "unused.db", cache_redis_url="redis://localhost:6379/0") as client:
        with pytest.raises(RedisConnectionError):
            await client.client.get("/items")
    sqlite.assert_not_called()
    connections[0].aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_context_exception_closes_connection(redis_setup):
    _, connections, _ = redis_setup
    with pytest.raises(RuntimeError, match="body error"):
        async with Client(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0"):
            raise RuntimeError("body error")
    connections[0].aclose.assert_awaited_once()


@pytest.mark.parametrize("kwargs", [{"cache_key_prefix": ""}, {"cache_key_prefix": "   "}, {"cache_max_response_bytes": 0}, {"cache_max_response_bytes": -1}])
def test_invalid_redis_options_fail_before_connection(kwargs, redis_setup):
    _, _, connection_factory = redis_setup
    with pytest.raises(ValueError):
        Client(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0", **kwargs)
    connection_factory.assert_not_called()


def test_bad_url_fails_without_sqlite_fallback(monkeypatch):
    sqlite = Mock()
    monkeypatch.setattr(module, "AsyncSqliteStorage", sqlite)
    with pytest.raises(ValueError):
        Client(url="https://example.test", cache_ttl=60, cache_redis_url="https://not-a-redis-url")
    sqlite.assert_not_called()


@pytest.mark.asyncio
async def test_class_defaults_subclass_overrides_and_explicit_arguments(redis_setup, monkeypatch):
    class Custom(Client):
        CACHE_KEY_PREFIX = "custom-api"
        CACHE_MAX_RESPONSE_BYTES = 2048
    _, connections, _ = redis_setup
    storage_factory = Mock(wraps=module.AsyncRedisStorage)
    monkeypatch.setattr(module, "AsyncRedisStorage", storage_factory)
    options = dict(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0")
    async with Custom(**options):
        storage_factory.assert_called_with(client=connections[-1], ttl=60, key_prefix="custom-api", max_stream_size=2048)
    async with Custom(**options, cache_key_prefix="instance", cache_max_response_bytes=None):
        storage_factory.assert_called_with(client=connections[-1], ttl=60, key_prefix="instance", max_stream_size=None)
    async with Custom(**options, cache_max_response_bytes=4096):
        assert storage_factory.call_args.kwargs["max_stream_size"] == 4096
    async with Client(**options):
        assert storage_factory.call_args.kwargs["key_prefix"] == Client.CACHE_KEY_PREFIX == "raq_helpers"
        assert storage_factory.call_args.kwargs["max_stream_size"] == Client.CACHE_MAX_RESPONSE_BYTES == 10 * 1024 * 1024


@pytest.mark.asyncio
async def test_subclass_can_disable_default_response_limit(redis_setup, monkeypatch):
    class Unlimited(Client):
        CACHE_MAX_RESPONSE_BYTES = None
    storage_factory = Mock(wraps=module.AsyncRedisStorage)
    monkeypatch.setattr(module, "AsyncRedisStorage", storage_factory)
    async with Unlimited(url="https://example.test", cache_ttl=60, cache_redis_url="redis://localhost:6379/0"):
        assert storage_factory.call_args.kwargs["max_stream_size"] is None
