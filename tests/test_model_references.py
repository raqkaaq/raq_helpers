"""Registry boundaries, strict status collections, typed endpoints and requests."""

from http import HTTPStatus

import pytest
from pydantic import ValidationError

from raq_helpers.models import Endpoint, ModelReference, PreparedRequest, ResponseCollection


@pytest.mark.parametrize("name", ["Item", "Error", "ItemInput"])
def test_registry_resolution_and_json_roundtrip(name, registry):
    reference = ModelReference.model_validate({"ref": name, "model": name}, context={"models": registry})
    assert reference.model is registry[name]
    restored = ModelReference.model_validate_json(reference.model_dump_json(), context={"models": registry})
    assert restored.model is registry[name]
    assert reference.model_dump()["model"] is registry[name]


@pytest.mark.parametrize("value", ["Unknown", str, 42, {}])
def test_invalid_model_reference(value):
    with pytest.raises(ValidationError):
        ModelReference.model_validate({"ref": "Unknown", "model": value}, context={"models": {}})


def test_string_reference_requires_context():
    with pytest.raises(ValidationError, match="Unknown model name"):
        ModelReference(ref="Item", model="Item")


def test_none_reference_and_empty_body_marker():
    assert ModelReference(ref="empty").model is None
    assert ModelReference(ref="empty").model_dump(mode="json") == {"ref": "empty", "model": None}
    assert ModelReference(ref="empty", no_content=True).model_dump(mode="json")["no_content"] is True


@pytest.mark.parametrize("status", sorted({int(status) for status in HTTPStatus} | {104, 306}))
def test_all_registered_statuses_supported_but_unpopulated_by_default(status, registry):
    assert ResponseCollection().model_dump(mode="json", by_alias=True, exclude_none=True) == {}
    collection = ResponseCollection.model_validate({str(status): {"ref": "Item", "model": "Item"}}, context={"models": registry})
    assert collection.model_dump(mode="json", by_alias=True, exclude_none=True) == {str(status): {"ref": "Item", "model": "Item"}}


@pytest.mark.parametrize("code", ["999", "default", "2XX"])
def test_undeclared_collection_keys_rejected(code):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ResponseCollection.model_validate({code: {"ref": "empty"}})


def test_endpoint_rejects_top_level_extras(endpoint_data, registry):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Endpoint.model_validate({**endpoint_data, "unexpected": 1}, context={"models": registry})


def test_prepared_request_strictness_and_independent_defaults():
    first = PreparedRequest(method="GET", url="/items")
    second = PreparedRequest(method="GET", url="/items")
    first.params["q"] = "x"
    assert second.params == {}
    with pytest.raises(ValidationError):
        PreparedRequest(method="INVALID", url="/items")
    with pytest.raises(ValidationError):
        PreparedRequest(method="GET", url="/items", unexpected=True)
