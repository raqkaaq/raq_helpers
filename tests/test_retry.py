import importlib
from unittest.mock import AsyncMock

import httpx2
import pytest
from raq_helpers.client import Client

module = importlib.import_module("raq_helpers.client.client")
pytestmark = pytest.mark.asyncio


@pytest.fixture
def retry_setup(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr(module.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(Client, "MAX_RETRIES", 3)
    return sleep


def response(status, headers=None):
    return httpx2.Response(
        status,
        headers=headers,
        request=httpx2.Request("GET", "https://api.openalex.org/works"),
    )


async def test_success_does_not_retry(retry_setup):
    operation = AsyncMock(return_value=response(200))
    assert (
        await Client.retry(operation)(Client.__new__(Client))
    ).status_code == 200
    operation.assert_awaited_once()
    retry_setup.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ReadTimeout("timeout"),
        httpx2.ConnectError("offline"),
        response(503),
        response(429),
    ],
)
async def test_transient_failure_recovers(retry_setup, failure):
    operation = AsyncMock(side_effect=[failure, response(200)])
    assert (
        await Client.retry(operation)(Client.__new__(Client))
    ).status_code == 200
    assert operation.await_count == 2
    retry_setup.assert_awaited_once()
    assert 1 <= retry_setup.await_args.args[0] <= Client.CAP_SINGLE_RETRY_DELAY


@pytest.mark.parametrize(
    "status,headers",
    [
        (400, {}),
        (401, {}),
        (404, {}),
    ],
)
async def test_permanent_failure_does_not_retry(retry_setup, status, headers):
    operation = AsyncMock(return_value=response(status, headers))
    with pytest.raises(httpx2.HTTPStatusError):
        await Client.retry(operation)(Client.__new__(Client))
    operation.assert_awaited_once()
    retry_setup.assert_not_awaited()


@pytest.mark.parametrize("failure", [httpx2.ConnectError("offline"), response(503)])
async def test_retry_exhaustion(retry_setup, failure):
    operation = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, Exception)
        else AsyncMock(return_value=failure)
    )
    with pytest.raises((httpx2.ConnectError, httpx2.HTTPStatusError)):
        await Client.retry(operation)(Client.__new__(Client))
    assert operation.await_count == 3
    assert retry_setup.await_count == Client.MAX_RETRIES - 1
    assert all(
        1 <= call.args[0] <= Client.CAP_SINGLE_RETRY_DELAY
        for call in retry_setup.await_args_list
    )


@pytest.mark.parametrize(
    "header,delay", [("12", 12), ("invalid", 2), ("Wed, 01 Jan 2020 00:00:00 GMT", 2)]
)
async def test_retry_after(retry_setup, header, delay):
    operation = AsyncMock(
        side_effect=[response(503, {"Retry-After": header}), response(200)]
    )
    await Client.retry(operation)(Client.__new__(Client))
    retry_setup.assert_awaited_once()
    assert retry_setup.await_args.args[0] >= delay
    assert retry_setup.await_args.args[0] <= max(
        delay, Client.CAP_SINGLE_RETRY_DELAY
    )


async def test_unexpected_error_propagates(retry_setup):
    operation = AsyncMock(side_effect=ValueError("bad input"))
    with pytest.raises(ValueError, match="bad input"):
        await Client.retry(operation)(Client.__new__(Client))
    operation.assert_awaited_once()
    retry_setup.assert_not_awaited()


async def test_backoff_cap_and_last_attempt_have_no_extra_sleep(
    retry_setup, monkeypatch
):
    monkeypatch.setattr(Client, "MAX_RETRIES", 8)
    operation = AsyncMock(return_value=response(503))
    with pytest.raises(httpx2.HTTPStatusError):
        await Client.retry(operation)(Client.__new__(Client))
    assert operation.await_count == 8
    waits = [call.args[0] for call in retry_setup.await_args_list]
    assert len(waits) == 7
    assert all(1 <= delay <= Client.CAP_SINGLE_RETRY_DELAY for delay in waits)
    # The deterministic upper-bound sampler should grow to, then respect, the cap.
    assert waits == sorted(waits)
    assert waits[0] < waits[-1] == Client.CAP_SINGLE_RETRY_DELAY




@pytest.mark.parametrize("header", ["0", "1", "40"])
async def test_retry_after_minimum_is_honored(retry_setup, header):
    operation = AsyncMock(
        side_effect=[response(503, {"Retry-After": header}), response(200)]
    )
    await Client.retry(operation)(Client.__new__(Client))
    retry_setup.assert_awaited_once()
    assert retry_setup.await_args.args[0] >= max(1, int(header))
    assert retry_setup.await_args.args[0] <= max(
        int(header), Client.CAP_SINGLE_RETRY_DELAY
    )


async def test_future_retry_after_date_uses_utc_difference(retry_setup, monkeypatch):
    from datetime import datetime, timezone

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", Clock)
    operation = AsyncMock(
        side_effect=[
            response(503, {"Retry-After": "Thu, 01 Jan 2026 00:00:12 GMT"}),
            response(200),
        ]
    )
    await Client.retry(operation)(Client.__new__(Client))
    retry_setup.assert_awaited_once()
    assert retry_setup.await_args.args[0] >= 12


async def test_cancellation_is_propagated_without_retry(retry_setup):
    import asyncio

    operation = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await Client.retry(operation)(Client.__new__(Client))
    operation.assert_awaited_once()
    retry_setup.assert_not_awaited()


async def test_total_retry_deadline_cancels_inflight_operation(monkeypatch):
    import asyncio

    monkeypatch.setattr(Client, "MAX_RETRY_DELAY", 0.02)

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    operation = AsyncMock(side_effect=blocked)
    with pytest.raises(TimeoutError):
        await Client.retry(operation)(Client.__new__(Client))
    operation.assert_awaited_once()


@pytest.mark.parametrize("attempts", [0, -1])
async def test_invalid_attempt_budget_rejected_before_call(attempts):
    owner = Client.__new__(Client)
    owner.MAX_RETRIES = attempts
    operation = AsyncMock()
    with pytest.raises(ValueError, match="MAX_RETRIES"):
        await Client.retry(operation)(owner)
    operation.assert_not_awaited()


async def test_response_hook_can_veto_retry(retry_setup):
    class Custom(Client):
        def should_retry_response(self, response):
            return False
    operation = AsyncMock(return_value=response(503))
    with pytest.raises(httpx2.HTTPStatusError):
        await Client.retry(operation)(Custom.__new__(Custom))
    operation.assert_awaited_once()
    retry_setup.assert_not_awaited()


async def test_retry_budget_is_fixed_for_the_inflight_call(retry_setup):
    owner = Client.__new__(Client)
    owner.MAX_RETRIES = 2
    calls = 0
    async def operation(client):
        nonlocal calls
        calls += 1
        # A later configuration change must not accidentally extend this call.
        client.MAX_RETRIES = 10
        return response(503)
    with pytest.raises(httpx2.HTTPStatusError):
        await Client.retry(operation)(owner)
    assert calls == 2
    retry_setup.assert_awaited_once()


async def test_naive_retry_after_dates_are_treated_as_utc(retry_setup, monkeypatch):
    from datetime import datetime, timezone
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "parsedate_to_datetime", lambda value: datetime(2026, 1, 1, 0, 0, 12))
    operation = AsyncMock(side_effect=[response(503, {"Retry-After": "date"}), response(200)])
    await Client.retry(operation)(Client.__new__(Client))
    retry_setup.assert_awaited_once_with(12)


@pytest.mark.parametrize("status,expected", [(200, False), (400, False), (429, True), (500, True), (599, True), (600, False)])
async def test_default_response_retry_policy(status, expected):
    assert Client.__new__(Client).should_retry_response(response(status)) is expected
