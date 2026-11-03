import re

import pytest
from hishel import Headers, Request, Response

from raq_helpers.client.cache_filters import (
    ContentTypeFilter,
    HeaderFilter,
    MethodFilter,
    StatusCodeFilter,
    URLPatternFilter,
)


@pytest.mark.parametrize("pattern,url,expected", [
    (r"/items/", "https://example.com/items/123", True),
    (r"/items/", "https://example.com/users/123", False),
    (r"/users/", "https://example.com/users/123", True),
    (r"/items/\d+$", "https://example.com/items/123", True),
    (r"/items/\d+$", "https://example.com/items/name", False),
])
def test_url_filter_uses_supplied_pattern(pattern, url, expected):
    assert URLPatternFilter(pattern).apply(Request("GET", url), None) is expected


def test_invalid_url_regex():
    with pytest.raises(re.error):
        URLPatternFilter("[")


@pytest.mark.parametrize("method,expected", [("GET", True), ("get", True), ("POST", True), ("post", True), ("DELETE", False), ("HEAD", False)])
def test_method_matching_is_case_insensitive(method, expected):
    assert MethodFilter(["get", "Post"]).apply(Request(method, "https://example.com/items/123"), None) is expected


def test_empty_method_list_rejects_requests():
    assert not MethodFilter([]).apply(Request("GET", "https://example.com/items/123"), None)


@pytest.mark.parametrize("status,expected", [(200, True), (201, True), (202, True), (204, True), (301, False), (400, False), (429, False), (500, False)])
def test_status_code_matching(status, expected):
    assert StatusCodeFilter([200, 201, 202, 204]).apply(Response(status), None) is expected


def test_empty_status_list_rejects_responses():
    assert not StatusCodeFilter([]).apply(Response(200), None)


@pytest.mark.parametrize("headers,expected", [
    ({"content-type": "application/json"}, True),
    ({"Content-Type": "application/json; charset=utf-8"}, True),
    ({"content-type": "text/plain"}, True),
    ({"content-type": "image/png"}, False),
    ({}, False),
])
def test_content_type_matching(headers, expected):
    assert ContentTypeFilter(["application/json", "text/plain"]).apply(Response(200, headers=Headers(headers)), None) is expected


def test_empty_content_type_list_rejects_responses():
    assert not ContentTypeFilter([]).apply(Response(200, headers={"content-type": "application/json"}), None)


@pytest.mark.parametrize("headers,expected", [
    ({"x-cache": "enabled"}, True),
    ({"X-Cache": "enabled"}, True),
    ({"x-cache": "disabled"}, False),
    ({"x-cache": "Enabled"}, False),
    ({}, False),
])
def test_header_name_is_case_insensitive_and_value_is_exact(headers, expected):
    assert HeaderFilter("x-cache", "enabled").apply(Response(200, headers=Headers(headers)), None) is expected


@pytest.mark.parametrize("filter_,item", [
    (URLPatternFilter(r"/items/"), Request("GET", "https://example.com/items/123")),
    (MethodFilter(["GET"]), Request("GET", "https://example.com/items/123")),
    (StatusCodeFilter([200]), Response(200)),
    (ContentTypeFilter(["application/json"]), Response(200, headers={"content-type": "application/json"})),
    (HeaderFilter("x-cache", "enabled"), Response(200, headers={"x-cache": "enabled"})),
])
def test_filters_do_not_need_or_depend_on_body(filter_, item):
    assert filter_.needs_body() is False
    assert filter_.apply(item, None) == filter_.apply(item, b"arbitrary body")


@pytest.mark.parametrize("filter_,criterion", [
    (URLPatternFilter(r"/items/"), "/items/"),
    (MethodFilter(["post"]), "POST"),
    (StatusCodeFilter([201]), "201"),
    (ContentTypeFilter(["application/json"]), "application/json"),
    (HeaderFilter("x-mode", "enabled"), "enabled"),
])
def test_filter_diagnostics_identify_configured_criteria(filter_, criterion):
    assert type(filter_).__name__ in repr(filter_)
    assert criterion in repr(filter_) and criterion in str(filter_)
