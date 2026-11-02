"""Shared async HTTP transport, persistent caching, and lifecycle management."""

import asyncio
import logging
import random
import re
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from typing import Mapping

import httpx2
from hishel import AsyncSqliteStorage, BaseFilter, FilterPolicy, Request, Response
from hishel.httpx2 import AsyncCacheClient, AsyncCacheTransport
from pydantic import ValidationError

from .cache_filters import MethodFilter, StatusCodeFilter, URLPatternFilter

logger = logging.getLogger(__name__)


class _UseClassDefault:
    """Distinguish an omitted timeout from an explicit None (no timeout)."""


_USE_CLASS_DEFAULT = _UseClassDefault()


class Client:
    CACHE_EXCLUDE_PATTERNS: tuple[str, ...] = ()
    CACHE_METHODS: tuple[str, ...] = ("GET",)
    CACHE_STATUS_CODES: tuple[int, ...] = (200, 201, 202, 204)
    MAX_REDIRECTS = 10
    TIMEOUT = httpx2.Timeout(10.0, connect=60.0)

    MAX_RETRIES = 10
    CAP_SINGLE_RETRY_DELAY = 30.0
    MAX_RETRY_DELAY = 100.0  # Total retry deadline in seconds.

    @classmethod
    def cache_url_pattern(cls, base_url: str) -> str:
        """Match URLs under the base URL, excluding configured endpoint paths."""
        base = rf"^{re.escape(base_url.rstrip('/'))}/"
        if not cls.CACHE_EXCLUDE_PATTERNS:
            return base + r".*$"
        excluded = "|".join(re.escape(path) for path in cls.CACHE_EXCLUDE_PATTERNS)
        return base + rf"(?!(?:{excluded})(?:[/?#]|$)).*$"

    def __init__(
        self,
        *,
        url: str,
        cache_db_path: str | Path,
        cache_ttl: int,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        request_filters: list[BaseFilter[Request]] | None = None,
        response_filters: list[BaseFilter[Response]] | None = None,
        timeout: httpx2.Timeout | float | None | _UseClassDefault = _USE_CLASS_DEFAULT,
        max_redirects: int | None = None,
    ):
        self.url = url
        self.storage = AsyncSqliteStorage(
            database_path=cache_db_path,
            default_ttl=cache_ttl,
        )
        self.policy = FilterPolicy(
            request_filters=(
                request_filters
                if request_filters is not None
                else [
                    URLPatternFilter(self.cache_url_pattern(url)),
                    MethodFilter(list(self.CACHE_METHODS)),
                ]
            ),
            response_filters=(
                response_filters
                if response_filters is not None
                else [StatusCodeFilter(list(self.CACHE_STATUS_CODES))]
            ),
        )
        self.transport = AsyncCacheTransport(
            next_transport=httpx2.AsyncHTTPTransport(),
            storage=self.storage,
            policy=self.policy,
        )
        self.client = AsyncCacheClient(
            base_url=url,
            headers=headers,
            params=params,
            transport=self.transport,
            max_redirects=self.MAX_REDIRECTS if max_redirects is None else max_redirects,
            timeout=self.TIMEOUT if isinstance(timeout, _UseClassDefault) else timeout,
        )
        logger.debug("%s initialized with URL: %s", type(self).__name__, url)

    async def __aenter__(self):
        return self

    async def aclose(self):
        await self.client.aclose()
        logger.debug("%s closed.", type(self).__name__)

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.aclose()

    def get_next_cursor(self, page):
        """Return the continuation cursor, or None."""
        return None

    def supports_pagination(self, page):
        """Whether this response supports cursor continuation."""
        return False

    @staticmethod
    def pagination(func):
        """Retain an explicit cursor continuation without fetching ahead."""
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            parameters = deepcopy(kwargs) if kwargs.get("cursor") else None
            result = await func(self, *args, **kwargs)
            cursor = self.get_next_cursor(result)
            if (
                parameters is not None
                and cursor
                and self.supports_pagination(result)
            ):
                parameters.pop("page", None)
                parameters["cursor"] = cursor

                async def continuation():
                    return await wrapper(self, *args, **deepcopy(parameters))

                # Private runtime state stays out of response model fields/extras.
                object.__setattr__(result, "_raq_helpers_next_page", continuation)
            return result
        return wrapper

    async def next_page(self, page):
        """Fetch one continuation page, or return None when none is available."""
        continuation = getattr(page, "_raq_helpers_next_page", None)
        return await continuation() if continuation is not None else None

    def handle_validation_error(self, error):
        """Propagate validation errors unless a subclass supplies API error handling."""
        raise error

    def process_parsed_response(self, parsed, response):
        """Return a parsed response unchanged unless a subclass customizes it."""
        return parsed

    @staticmethod
    def parse_as(responses):
        models = {
            field.alias: getattr(responses, name)
            for name, field in type(responses).model_fields.items()
            if getattr(responses, name) is not None
        }

        def decorator(func):
            @wraps(func)
            async def wrapper(self, *args, **kwargs):
                try:
                    response = await func(self, *args, **kwargs)
                except httpx2.HTTPStatusError as error:
                    definition = models.get(str(error.response.status_code))
                    logger.error("HTTP error %s: %s", error.response.status_code, error.response.text)
                    if definition is None:
                        logger.warning(
                            "Undeclared response status %s; declared statuses: %s",
                            error.response.status_code,
                            ", ".join(models) or "none",
                        )
                        raise
                    if error.response.request.method == "HEAD" or definition.no_content or definition.model is None:
                        raise
                    return definition.model.model_validate_json(
                        error.response.content,
                        extra="allow",
                    )
                except ValidationError as error:
                    logger.error("Response validation error: %s", error)
                    return self.handle_validation_error(error)

                definition = models.get(str(response.status_code))
                if definition is None:
                    raise ValueError(
                        f"Unexpected response status {response.status_code} for "
                        f"{response.request.method} {response.request.url.path}; "
                        f"declared statuses: {', '.join(models) or 'none'}"
                    )
                if response.request.method == "HEAD" and 200 <= response.status_code < 300:
                    return None
                if definition.no_content:
                    if response.content:
                        raise ValueError(f"Expected an empty response for status {response.status_code}.")
                    return None
                if definition.model is None:
                    raise ValueError(f"No response model declared for status {response.status_code}.")
                parsed = definition.model.model_validate_json(
                    response.content,
                    extra="allow",
                )
                return self.process_parsed_response(parsed, response)

            return wrapper
        return decorator

    def should_retry_response(self, response: httpx2.Response) -> bool:
        """Whether an HTTP error response is eligible for retry."""
        return response.status_code == 429 or 500 <= response.status_code < 600

    @staticmethod
    def retry(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            if self.MAX_RETRIES < 1:
                raise ValueError("MAX_RETRIES must be at least 1.")

            async with asyncio.timeout(self.MAX_RETRY_DELAY):
                for attempt in range(1, self.MAX_RETRIES + 1):
                    retry_after = 0.0

                    try:
                        response = await func(self, *args, **kwargs)
                        response.raise_for_status()
                        return response
                    except (httpx2.TimeoutException, httpx2.NetworkError):
                        if attempt == self.MAX_RETRIES:
                            raise

                    except httpx2.HTTPStatusError as error:
                        response = error.response
                        if (
                            not self.should_retry_response(response)
                            or attempt == self.MAX_RETRIES
                        ):
                            raise

                        header = response.headers.get("Retry-After", "").strip()
                        try:
                            if header.isdigit():
                                retry_after = int(header)
                            else:
                                retry_at = parsedate_to_datetime(header)
                                if retry_at.tzinfo is None:
                                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                                retry_after = max(
                                    0.0,
                                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                                )
                        except (TypeError, ValueError, OverflowError):
                            retry_after = 0.0

                    delay = max(
                        random.uniform(
                            1, min(self.CAP_SINGLE_RETRY_DELAY, 2 ** attempt)
                        ),
                        retry_after,
                    )
                    logger.info(
                        "Attempt %s failed. Retrying in %.2f seconds.",
                        attempt,
                        delay,
                    )
                    await asyncio.sleep(delay)

        return wrapper

    @staticmethod
    def cache(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            response = await func(self, *args, **kwargs)
            logger.debug(
                "Cache metadata for %s: %s",
                response.request.url.copy_with(query=None),
                {
                    key: value
                    for key, value in response.extensions.items()
                    if key.startswith("hishel_")
                },
            )
            return response
        return wrapper
