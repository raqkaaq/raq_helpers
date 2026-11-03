"""Shared cursor continuation mechanics, adapted from raqindex's pagination tests."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from raq_helpers.client import Client


class Page(BaseModel):
    results: list[int]
    cursor: str | None = None


class CursorClient(Client):
    def get_next_cursor(self, page):
        return page.cursor
    def supports_pagination(self, page):
        return isinstance(page.results, list)


@pytest.mark.asyncio
async def test_lazy_pages_preserve_parameters_and_do_not_serialize_state():
    client = CursorClient.__new__(CursorClient)
    operation = AsyncMock(side_effect=[Page(results=[1], cursor="next"), Page(results=[2], cursor=None)])
    wrapped = Client.pagination(operation)
    first = await wrapped(client, cursor="*", page=5, filters={"tags": ["original"]})
    operation.assert_awaited_once()
    assert "_raq_helpers_next_page" not in first.model_dump_json()
    second = await client.next_page(first)
    assert second.results == [2]
    assert operation.await_args.kwargs == {"cursor": "next", "filters": {"tags": ["original"]}}
    assert await client.next_page(second) is None
    assert operation.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, "", False])
async def test_no_cursor_means_no_continuation(cursor):
    client = CursorClient.__new__(CursorClient)
    operation = AsyncMock(return_value=Page(results=[1], cursor="next"))
    page = await Client.pagination(operation)(client, cursor=cursor)
    assert await client.next_page(page) is None
    operation.assert_awaited_once()


@pytest.mark.asyncio
async def test_saved_cursor_nested_inputs_and_independent_continuations():
    client = CursorClient.__new__(CursorClient)
    calls = []
    async def operation(owner, **params):
        calls.append(deepcopy(params))
        return Page(results=[1], cursor=params["filters"]["tags"][0] + "-next")
    wrapped = Client.pagination(operation)
    original = {"tags": ["one"]}
    first = await wrapped(client, cursor="saved", filters=original)
    second = await wrapped(client, cursor="*", filters={"tags": ["two"]})
    original["tags"].append("edited")
    await client.next_page(first)
    assert calls[-1] == {"cursor": "one-next", "filters": {"tags": ["one"]}}
    await client.next_page(second)
    assert calls[-1] == {"cursor": "two-next", "filters": {"tags": ["two"]}}
    await client.next_page(first)
    assert calls[-1] == {"cursor": "one-next", "filters": {"tags": ["one"]}}


@pytest.mark.asyncio
async def test_continuation_propagates_failures():
    client = CursorClient.__new__(CursorClient)
    failure = RuntimeError("continuation failed")
    operation = AsyncMock(side_effect=[Page(results=[], cursor="next"), failure])
    page = await Client.pagination(operation)(client, cursor="*")
    with pytest.raises(RuntimeError) as caught:
        await client.next_page(page)
    assert caught.value is failure


@pytest.mark.asyncio
async def test_default_hooks_disable_pagination(bare_client):
    operation = AsyncMock(return_value=Page(results=[], cursor="next"))
    page = await Client.pagination(operation)(bare_client, cursor="*")
    assert bare_client.get_next_cursor(page) is None
    assert bare_client.supports_pagination(page) is False
    assert await bare_client.next_page(page) is None
    assert await bare_client.next_page(object()) is None


@pytest.mark.asyncio
async def test_subclass_can_decline_specific_page():
    class Disabled(CursorClient):
        def supports_pagination(self, page):
            return False
    client = Disabled.__new__(Disabled)
    page = await Client.pagination(AsyncMock(return_value=Page(results=[], cursor="next")))(client, cursor="*")
    assert await client.next_page(page) is None
