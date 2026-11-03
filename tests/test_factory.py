"""Typed loading, operation identity, request building, hooks, and execution."""

import json
from copy import deepcopy
from functools import wraps
from unittest.mock import AsyncMock

import httpx2
import pytest
from pydantic import BaseModel, Field, ValidationError

from raq_helpers.client import Client, EndpointFactory
from raq_helpers.models import Endpoint, ModelReference, PreparedRequest, ResponseCollection


def changed(original, **updates):
    return original.model_copy(update=updates, deep=True)


def test_identity_normalization_and_ambiguity(endpoint):
    post = endpoint.model_copy(deep=True)
    post.method.method = "POST"
    factory = EndpointFactory([endpoint, post])
    assert factory.get_endpoint(endpoint.endpoint, "get") is endpoint
    assert factory.get_endpoint(endpoint.endpoint, "post") is post
    with pytest.raises(ValueError, match="Ambiguous.*GET, POST"):
        factory.get_endpoint(endpoint.endpoint)
    with pytest.raises(ValueError, match="available methods: GET, POST"):
        factory.get_endpoint(endpoint.endpoint, "DELETE")
    with pytest.raises(ValueError, match="not found"):
        factory.get_endpoint("/missing")
    assert EndpointFactory([endpoint]).get_endpoint(endpoint.endpoint) is endpoint


def test_duplicate_definitions_and_raw_dicts_rejected(endpoint):
    with pytest.raises(ValueError, match="Duplicate"):
        EndpointFactory([endpoint, endpoint.model_copy(deep=True)])
    with pytest.raises(TypeError, match="from_json"):
        EndpointFactory([endpoint.model_dump()])


def test_json_loading_and_registry_resolution(endpoint_data, registry):
    factory = EndpointFactory.from_json(json.dumps([endpoint_data]).encode(), registry=registry)
    endpoint = factory.get_endpoint("/items/{id}")
    assert isinstance(endpoint, Endpoint)
    assert endpoint.method.request.model is registry["ItemInput"]
    assert factory.get_endpoint_responses(endpoint) is endpoint.method.responses


@pytest.mark.parametrize("data", ["{}", "null", '"string"', "1"])
def test_json_top_level_must_be_list(data):
    with pytest.raises(ValueError, match="list"):
        EndpointFactory.from_json(data, registry={})


def test_invalid_json_and_model_names_rejected(endpoint_data):
    with pytest.raises(json.JSONDecodeError):
        EndpointFactory.from_json("{", registry={})
    with pytest.raises(ValidationError, match="Unknown model name"):
        EndpointFactory.from_json(json.dumps([endpoint_data]), registry={})


def test_custom_responses_survive_json_loading(endpoint_data, registry):
    class CustomResponses(ResponseCollection):
        unusual: ModelReference | None = Field(None, alias="299")
    class CustomFactory(EndpointFactory):
        RESPONSE_COLLECTION = CustomResponses
    endpoint_data["method"]["responses"] = {"299": {"ref": "Item", "model": "Item"}}
    factory = CustomFactory.from_json(json.dumps([endpoint_data]), registry=registry)
    assert isinstance(factory.get_endpoint("/items/{id}").method.responses, CustomResponses)
    assert factory.get_endpoint_responses(factory.get_endpoint("/items/{id}")).unusual is not None


def test_prepared_request_validation_encoding_and_aliases(endpoint, registry):
    params = {"id": "https://example.test/a?b=1#part", "search.term": "text", "limit": "2"}
    before = deepcopy(params)
    request = EndpointFactory([endpoint]).build_endpoint_request(endpoint, params)
    assert isinstance(request, PreparedRequest)
    assert isinstance(request.validated_input, registry["ItemInput"])
    assert request.url == "/items/https%3A%2F%2Fexample.test%2Fa%3Fb%3D1%23part"
    assert request.params == {"search.term": "text", "limit": 2}
    assert request.method == "GET" and params == before


def test_repeated_path_placeholders_extracted_once(endpoint):
    endpoint = changed(endpoint, endpoint="/compare/{id}/{id}")
    request = EndpointFactory([endpoint]).build_endpoint_request(endpoint, {"id": "a/b"})
    assert request.url == "/compare/a%2Fb/a%2Fb"
    assert request.params == {"limit": 10}


@pytest.mark.parametrize("params", [{}, {"id": "x", "unknown": 1}, {"id": "x", "limit": 0}])
def test_bad_inputs_rejected(endpoint, params):
    with pytest.raises(ValidationError):
        EndpointFactory([endpoint]).build_endpoint_request(endpoint, params)


def test_empty_request_and_unsupported_bodies(endpoint):
    endpoint = changed(endpoint, endpoint="/health")
    endpoint.method.request = ModelReference(ref="")
    factory = EndpointFactory([endpoint])
    empty = factory.build_endpoint_request(endpoint, {})
    assert empty.validated_input is None and empty.params == {}
    with pytest.raises(ValueError, match="no request inputs"):
        factory.build_endpoint_request(endpoint, {"unknown": 1})
    endpoint.endpoint = "/items/{id}"
    with pytest.raises(ValueError, match="Path parameters require"):
        factory.build_endpoint_request(endpoint, {})
    endpoint.method.payload = "json"
    with pytest.raises(ValueError, match="Only path/query"):
        factory.build_endpoint_request(endpoint, {})


def test_preparation_hook_injects_without_mutating_inputs(endpoint):
    class CustomFactory(EndpointFactory):
        def prepare_request_values(self, endpoint, params):
            values = super().prepare_request_values(endpoint, params)
            values["limit"] = 3
            return values
    params = {"id": "x"}
    request = CustomFactory([endpoint]).build_endpoint_request(endpoint, params)
    assert request.params == {"limit": 3} and params == {"id": "x"}


@pytest.mark.asyncio
async def test_decorator_order_and_multiple_api_decorators():
    events = []
    def decorator(name):
        def apply(call):
            @wraps(call)
            async def wrapped(*args, **kwargs):
                events.append(name + ":enter")
                result = await call(*args, **kwargs)
                events.append(name + ":exit")
                return result
            return wrapped
        return apply
    class RecordingClient:
        pagination = staticmethod(decorator("pagination"))
        retry = staticmethod(decorator("retry"))
        cache = staticmethod(decorator("cache"))
        @staticmethod
        def parse_as(responses):
            return decorator("parse")
    class CustomFactory(EndpointFactory):
        def decorate_api_call(self, client, call):
            return decorator("cost")(decorator("audit")(call))
    async def call(owner, value):
        events.append("request")
        return value
    client = RecordingClient()
    assert EndpointFactory([]).decorate_api_call(client, call) is call
    wrapped = CustomFactory([]).decorate_endpoint_call(client, call, None)
    assert await wrapped(client, 42) == 42
    names = ["pagination", "parse", "cost", "audit", "retry", "cache"]
    assert events == [name + ":enter" for name in names] + ["request"] + [name + ":exit" for name in reversed(names)]


@pytest.mark.asyncio
async def test_full_factory_call_hooks_and_pre_send_validation(endpoint, registry, bare_client):
    class CustomFactory(EndpointFactory):
        def process_endpoint_response(self, response, request):
            assert isinstance(request, PreparedRequest)
            response.extensions["input"] = request.validated_input
            self.last_response = response
            return response
    factory = CustomFactory([endpoint])
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.raw_path == b"/items/a%2Fb?limit=2"
        return httpx2.Response(200, json={"id": 1})
    async with httpx2.AsyncClient(base_url="https://example.test", transport=httpx2.MockTransport(handler)) as transport:
        bare_client.client = transport
        call = factory.create_endpoint(bare_client, endpoint.endpoint, "get")
        assert (await call(id="a/b", limit="2")).id == 1
        assert factory.last_response.extensions["input"].limit == 2
        with pytest.raises(ValidationError):
            await call(id="x", unknown=1)
        assert len(calls) == 1


def test_response_hook_identity():
    response = object()
    assert EndpointFactory([]).process_endpoint_response(response, None) is response
