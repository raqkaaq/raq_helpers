"""Isolated models and endpoint definitions; no developer configuration or network."""

from copy import deepcopy
import importlib
import json
import socket
import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from raq_helpers.client import Client
from raq_helpers.models import Endpoint


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch):
    """Mock transports are required; local event-loop sockets remain available."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def guard(original):
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError(f"Live network access is forbidden in helper tests: {address!r}")
            return original(sock, address)
        return connect
    monkeypatch.setattr(socket.socket, "connect", guard(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(original_connect_ex))


class ItemInput(BaseModel):
    id: str
    search_term: str | None = Field(None, alias="search.term")
    limit: int = Field(10, ge=1)


class Item(BaseModel):
    id: int


class Error(BaseModel):
    message: str


@pytest.fixture
def bare_client():
    return Client.__new__(Client)


@pytest.fixture
def registry():
    return {"ItemInput": ItemInput, "Item": Item, "Error": Error}


@pytest.fixture
def endpoint_data():
    return {
        "endpoint": "/items/{id}",
        "method": {
            "method": "GET",
            "operationId": "getItem",
            "request": {"ref": "ItemInput", "model": "ItemInput"},
            "responses": {
                "200": {"ref": "Item", "model": "Item"},
                "400": {"ref": "Error", "model": "Error"},
            },
        },
    }


@pytest.fixture
def endpoint(endpoint_data, registry):
    return Endpoint.model_validate(endpoint_data, context={"models": registry})


@pytest.fixture
def api_schema():
    """Multiple methods, parameterless inputs, inline/ref/empty responses, aliases."""
    item = {"$ref": "#/components/schemas/Item"}
    return deepcopy({
        "openapi": "3.0.3",
        "info": {"title": "Offline test API", "version": "1"},
        "components": {
            "schemas": {"Item": {"type": "object", "required": ["id"],
                "properties": {"id": {"type": "integer"}}}},
            "responses": {"Error": {"description": "Error", "content": {
                "application/json": {"schema": {"type": "object", "required": ["message"],
                    "properties": {"message": {"type": "string"}}}}}}},
        },
        "paths": {
            "/items": {
                "get": {"responses": {"200": {"description": "Item", "content": {
                    "application/json": {"schema": item}}}}},
                "post": {"parameters": [{"name": "label", "in": "query", "required": True,
                    "schema": {"type": "string"}}], "responses": {"201": {
                        "description": "Created", "content": {"application/json": {"schema": item}}}}},
                "delete": {"responses": {"204": {"description": "Deleted"}}},
                "head": {"responses": {"200": {"description": "Headers", "content": {
                    "application/json": {"schema": item}}}}},
            },
            "/items/{id}": {"parameters": [{"name": "id", "in": "path", "required": True,
                "schema": {"type": "string"}}], "get": {
                    "parameters": [{"name": "search.term", "in": "query", "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Item", "content": {"application/json": {"schema": item}}},
                        "400": {"$ref": "#/components/responses/Error"}}}},
            "/inline": {"get": {"responses": {"200": {"description": "Inline", "content": {
                "application/json": {"schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}}}}},
        },
    })


@pytest.fixture
def generated_api(tmp_path, api_schema, monkeypatch):
    """Exercise the real generator, clean up imported modules after every test."""
    from datamodel_code_generator import InputFileType
    from raq_helpers.codegen import EndpointGenerator, ModelGenerator, PackageGenerator
    output = tmp_path / "generated_fixture"
    monkeypatch.syspath_prepend(str(tmp_path))
    models = ModelGenerator(output_dir=output)
    prepared = models.generate_models(api_schema, InputFileType.OpenAPI)
    importlib.invalidate_caches()
    registry = importlib.import_module("generated_fixture.registry").MODEL_REGISTRY
    endpoints = EndpointGenerator(schema=prepared, metadata=json.loads(models.metadata_path.read_text()), registry=registry, output_dir=output)
    definitions = endpoints.generate_mapping()
    endpoints.generate_wrappers(definitions)
    PackageGenerator(output_dir=output).generate(exports={"models": tuple(registry), "registry": ("MODEL_REGISTRY",), "endpoints": ("GeneratedEndpoints",)})
    # The package was initially imported as a namespace while its modules generated.
    sys.modules.pop("generated_fixture", None)
    package = importlib.import_module("generated_fixture")
    try:
        yield SimpleNamespace(models=models, endpoints=endpoints, definitions=definitions, registry=registry, package=package, output=output, schema=prepared)
    finally:
        for name in list(sys.modules):
            if name == "generated_fixture" or name.startswith("generated_fixture."):
                del sys.modules[name]
