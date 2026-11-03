"""Declared status dispatch, body semantics, and precise error-hook boundaries."""

from unittest.mock import AsyncMock

import httpx2
import pytest
from pydantic import ValidationError

from raq_helpers.client import Client


def response(status=200, *, method="GET", payload=None, content=None):
    kwargs = {"content": content} if content is not None else {"json": payload or {"id": 1}}
    return httpx2.Response(status, request=httpx2.Request(method, "https://example.test/items?token=secret"), **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201, 202])
async def test_declared_success_and_extra_fields(status, endpoint, registry, bare_client):
    from raq_helpers.models import ResponseCollection
    definitions = ResponseCollection.model_validate({str(status): {"ref": "Item", "model": "Item"}}, context={"models": registry})
    call = AsyncMock(return_value=response(status, payload={"id": 1, "future": True}))
    parsed = await Client.parse_as(definitions)(call)(bare_client, value=2)
    assert isinstance(parsed, registry["Item"])
    assert parsed.model_extra == {"future": True}
    call.assert_awaited_once_with(bare_client, value=2)


@pytest.mark.asyncio
async def test_success_postprocessing_hook(endpoint, bare_client):
    seen = []
    def process(parsed, http_response):
        seen.append((parsed, http_response))
        return "processed"
    bare_client.process_parsed_response = process
    http_response = response()
    parsed = await Client.parse_as(endpoint.method.responses)(AsyncMock(return_value=http_response))(bare_client)
    assert parsed == "processed" and seen[0][1] is http_response


@pytest.mark.asyncio
async def test_declared_http_error_is_parsed_without_success_postprocessing(endpoint, registry, bare_client):
    http_response = response(400, payload={"message": "bad"})
    with pytest.raises(httpx2.HTTPStatusError) as caught:
        http_response.raise_for_status()
    bare_client.process_parsed_response = lambda *args: pytest.fail("Success hook called for HTTP error")
    parsed = await Client.parse_as(endpoint.method.responses)(AsyncMock(side_effect=caught.value))(bare_client)
    assert isinstance(parsed, registry["Error"]) and parsed.message == "bad"


@pytest.mark.asyncio
async def test_undeclared_http_error_is_warned_and_preserved(endpoint, bare_client, caplog):
    with pytest.raises(httpx2.HTTPStatusError) as original:
        response(502).raise_for_status()
    with pytest.raises(httpx2.HTTPStatusError) as caught:
        await Client.parse_as(endpoint.method.responses)(AsyncMock(side_effect=original.value))(bare_client)
    assert caught.value is original.value
    assert "Undeclared response status 502" in caplog.text


@pytest.mark.asyncio
async def test_undeclared_success_has_safe_descriptive_error(endpoint, bare_client):
    with pytest.raises(ValueError, match="Unexpected response status 201 for GET /items") as error:
        await Client.parse_as(endpoint.method.responses)(AsyncMock(return_value=response(201)))(bare_client)
    assert "200, 400" in str(error.value) and "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_validation_hook_handles_only_wrapped_call_errors(endpoint, registry, bare_client):
    with pytest.raises(ValidationError) as original:
        registry["Item"](id="bad")
    errors = []
    bare_client.handle_validation_error = lambda error: errors.append(error) or "handled"
    parse = Client.parse_as(endpoint.method.responses)
    assert await parse(AsyncMock(side_effect=original.value))(bare_client) == "handled"
    assert errors == [original.value]
    with pytest.raises(ValidationError):
        await parse(AsyncMock(return_value=response(payload={"id": "bad"})))(bare_client)
    assert errors == [original.value]


@pytest.mark.asyncio
async def test_default_validation_hook_preserves_error(endpoint, registry, bare_client):
    with pytest.raises(ValidationError) as original:
        registry["Item"](id="bad")
    with pytest.raises(ValidationError) as caught:
        await Client.parse_as(endpoint.method.responses)(AsyncMock(side_effect=original.value))(bare_client)
    assert caught.value is original.value


@pytest.mark.asyncio
async def test_invalid_error_payload_propagates_validation_error(endpoint, bare_client):
    http_response = response(400, payload={"message": ["invalid"]})
    with pytest.raises(httpx2.HTTPStatusError) as original:
        http_response.raise_for_status()
    with pytest.raises(ValidationError):
        await Client.parse_as(endpoint.method.responses)(AsyncMock(side_effect=original.value))(bare_client)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"unexpected"])
async def test_declared_empty_body_contract(body, bare_client):
    from raq_helpers.models import ResponseCollection
    definitions = ResponseCollection.model_validate({"204": {"ref": "", "no_content": True}})
    call = Client.parse_as(definitions)(AsyncMock(return_value=response(204, content=body)))
    if body:
        with pytest.raises(ValueError, match="Expected an empty response"):
            await call(bare_client)
    else:
        assert await call(bare_client) is None


@pytest.mark.asyncio
async def test_missing_model_is_not_empty_content(bare_client):
    from raq_helpers.models import ResponseCollection
    definitions = ResponseCollection.model_validate({"200": {"ref": "Missing"}})
    with pytest.raises(ValueError, match="No response model"):
        await Client.parse_as(definitions)(AsyncMock(return_value=response()))(bare_client)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201])
async def test_head_success_returns_none(status, registry, bare_client):
    from raq_helpers.models import ResponseCollection
    definitions = ResponseCollection.model_validate({str(status): {"ref": "Item", "model": "Item"}}, context={"models": registry})
    assert await Client.parse_as(definitions)(AsyncMock(return_value=response(status, method="HEAD", content=b"")))(bare_client) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method,definition", [("HEAD", {"ref": "Error", "model": None}), ("GET", {"ref": "", "no_content": True}), ("GET", {"ref": "Unknown"})])
async def test_head_or_unmodeled_http_errors_are_preserved(method, definition, bare_client):
    from raq_helpers.models import ResponseCollection
    definitions = ResponseCollection.model_validate({"400": definition})
    with pytest.raises(httpx2.HTTPStatusError) as original:
        response(400, method=method, content=b"").raise_for_status()
    with pytest.raises(httpx2.HTTPStatusError) as caught:
        await Client.parse_as(definitions)(AsyncMock(side_effect=original.value))(bare_client)
    assert caught.value is original.value
