"""Operation-first mapping, generated wrappers, and clear unsupported-feature errors."""

import json
from copy import deepcopy

import pytest
from pydantic import BaseModel, Field

from raq_helpers.codegen import EndpointGenerator
from raq_helpers.models import Endpoint, ModelReference, ResponseCollection


def wrapper_type(path):
    namespace = {}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace["GeneratedEndpoints"]


def test_typed_mapping_inline_reference_and_empty_responses(generated_api):
    artifacts = generated_api
    endpoints = {(item.endpoint, item.method.method): item for item in artifacts.definitions}
    assert len(endpoints) == 6 and all(isinstance(item, Endpoint) for item in endpoints.values())
    assert endpoints[("/items", "GET")].method.request.model is None
    assert endpoints[("/items", "POST")].method.responses.created is not None
    assert endpoints[("/items", "DELETE")].method.responses.no_content.no_content
    assert endpoints[("/inline", "GET")].method.responses.success.model is not None
    error = endpoints[("/items/{id}", "GET")].method.responses.bad_request
    assert error is not None and error.ref == "Error" and error.model is not None
    serialized = json.loads(artifacts.endpoints.output_path.read_text())
    assert len(serialized) == len(endpoints)


@pytest.mark.asyncio
async def test_wrappers_choose_method_and_list_detail_without_changing_inputs(generated_api):
    calls = []
    class Recording(generated_api.package.GeneratedEndpoints):
        def endpoint_factory(self, path, *, method):
            async def call(**params):
                calls.append((path, method, params))
                return "sentinel"
            return call
    client = Recording()
    params = {"id": "a/b", "search.term": "example"}
    assert await client.items(**params) == "sentinel"
    assert calls[-1] == ("/items/{id}", "GET", params)
    assert await client.items(http_method="post", label="new") == "sentinel"
    assert calls[-1] == ("/items", "POST", {"label": "new"})
    count = len(calls)
    with pytest.raises(TypeError, match="Ambiguous HTTP method.*DELETE, GET, HEAD, POST"):
        await client.items()
    with pytest.raises(ValueError, match="available methods"):
        await client.items(http_method="patch")
    with pytest.raises(ValueError, match="available methods: GET"):
        await client.items(id="one", http_method="POST")
    assert len(calls) == count and params == {"id": "a/b", "search.term": "example"}


@pytest.mark.asyncio
async def test_single_method_wrapper_omits_method_unambiguously(generated_api):
    calls = []
    class Recording(generated_api.package.GeneratedEndpoints):
        def endpoint_factory(self, path, *, method):
            async def call(**params):
                calls.append((path, method, params))
            return call
    await Recording().inline()
    assert calls == [("/inline", "GET", {})]


@pytest.mark.parametrize("path", ["/invalid/{self}", "/invalid/{bad-name}", "/{value}", "/invalid/{class}"])
def test_invalid_python_names_fail_before_writing(path, generated_api):
    artifacts = generated_api
    before = artifacts.endpoints.wrappers_path.read_text()
    endpoint = artifacts.definitions[0].model_copy(update={"endpoint": path})
    with pytest.raises(ValueError):
        artifacts.endpoints.generate_wrappers([endpoint])
    assert artifacts.endpoints.wrappers_path.read_text() == before


@pytest.mark.parametrize("path", ["/aclose", "/next-page", "/client", "/storage", "/endpoint-factory", "/retry"])
def test_client_member_names_rejected(path, generated_api):
    endpoint = generated_api.definitions[0].model_copy(update={"endpoint": path})
    with pytest.raises(ValueError, match="conflicts with a client member"):
        generated_api.endpoints.generate_wrappers([endpoint])


def test_custom_reserved_names(generated_api):
    generator = generated_api.endpoints
    generator.RESERVED_WRAPPER_NAMES = generator.RESERVED_WRAPPER_NAMES | {"custom"}
    endpoint = generated_api.definitions[0].model_copy(update={"endpoint": "/custom"})
    with pytest.raises(ValueError, match="conflicts"):
        generator.generate_wrappers([endpoint])


@pytest.mark.parametrize("use_alias", [False, True])
def test_http_method_selector_input_collisions(use_alias, generated_api):
    class Input(BaseModel):
        http_method: str
    class AliasInput(BaseModel):
        value: str = Field(alias="http_method")
    endpoint = generated_api.definitions[0].model_copy(deep=True)
    endpoint.method.request = ModelReference(ref="Input", model=AliasInput if use_alias else Input)
    with pytest.raises(ValueError, match="conflicts with wrapper selector"):
        generated_api.endpoints.generate_wrappers([endpoint])


def test_duplicate_routes_rejected(generated_api):
    endpoint = generated_api.definitions[0]
    with pytest.raises(ValueError, match="Ambiguous routes"):
        generated_api.endpoints.generate_wrappers([endpoint, endpoint])


def test_keyword_paths_are_escaped_and_empty_wrapper_class_valid(generated_api):
    generator = generated_api.endpoints
    endpoint = generated_api.definitions[0].model_copy(update={"endpoint": "/class"})
    assert hasattr(wrapper_type(generator.generate_wrappers([endpoint])), "class_")
    cls = wrapper_type(generator.generate_wrappers([]))
    assert not [name for name in vars(cls) if not name.startswith("__")]


def test_documentation_exclusion_hook_is_not_input_validation(generated_api):
    class Input(BaseModel):
        hidden: str
        shown: int = 3
    endpoint = generated_api.definitions[0].model_copy(deep=True)
    endpoint.method.request = ModelReference(ref="Input", model=Input)
    generator = generated_api.endpoints
    assert "hidden:" in wrapper_type(generator.generate_wrappers([endpoint])).items.parameters
    generator.excluded_wrapper_parameters = lambda: ("hidden",)
    method = wrapper_type(generator.generate_wrappers([endpoint])).items
    assert "hidden:" not in method.parameters and "shown:" in method.parameters
    assert endpoint.method.request.model is Input


@pytest.mark.parametrize("kind", ["body", "header", "cookie", "text", "external", "cycle", "path_ref", "missing_model", "duplicate_model", "missing_registry"])
def test_unsupported_or_unresolvable_operations_fail_without_partial_output(kind, generated_api):
    generator = generated_api.endpoints
    generator.schema = deepcopy(generated_api.schema)
    operation = generator.schema["paths"]["/items"]["get"]
    if kind == "body":
        operation["requestBody"] = {}
    elif kind in {"header", "cookie"}:
        operation["parameters"] = [{"name": "value", "in": kind, "schema": {"type": "string"}}]
    elif kind == "text":
        operation["responses"]["200"]["content"] = {"text/plain": {"schema": {"type": "string"}}}
    elif kind == "external":
        operation["responses"]["200"] = {"$ref": "https://example.test/schema.json"}
    elif kind == "cycle":
        operation["responses"]["200"] = {"$ref": "#/paths/~1items/get/responses/200"}
    elif kind == "path_ref":
        generator.schema["paths"]["/items"]["$ref"] = "#/paths/other"
    elif kind == "missing_model":
        generator._models_by_source.pop(("components", "schemas", "Item"))
    elif kind == "duplicate_model":
        generator._models_by_source[("components", "schemas", "Item")].append("Other")
    else:
        generator.registry = {}
    before = generator.output_path.read_text()
    with pytest.raises(ValueError) as caught:
        generator.generate_mapping()
    assert "/items" in str(caught.value)
    assert generator.output_path.read_text() == before


def test_custom_response_collection_maps_and_loads(generated_api):
    from raq_helpers.client import EndpointFactory
    class CustomResponses(ResponseCollection):
        custom: ModelReference | None = Field(None, alias="299")
    class CustomGenerator(EndpointGenerator):
        RESPONSE_COLLECTION = CustomResponses
    class CustomFactory(EndpointFactory):
        RESPONSE_COLLECTION = CustomResponses
    schema = deepcopy(generated_api.schema)
    schema["paths"] = {"/items": {"get": {"responses": {"299": schema["paths"]["/items"]["get"]["responses"]["200"]}}}}
    generator = CustomGenerator(schema=schema, metadata=json.loads(generated_api.models.metadata_path.read_text()), registry=generated_api.registry, output_dir=generated_api.output)
    definitions = generator.generate_mapping()
    assert isinstance(definitions[0].method.responses, CustomResponses)
    factory = CustomFactory.from_json(generator.output_path.read_text(), registry=generated_api.registry)
    assert isinstance(factory.get_endpoint("/items").method.responses, CustomResponses)


def test_local_json_pointer_escaping_for_slashes_and_tildes(generated_api):
    generator = generated_api.endpoints
    generator.schema = {"components": {"responses": {"a/b~c": {"description": "resolved"}}}}
    resolved, location = generator._resolve({"$ref": "#/components/responses/a~1b~0c"}, ())
    assert resolved == {"description": "resolved"}
    assert location == ("components", "responses", "a/b~c")
