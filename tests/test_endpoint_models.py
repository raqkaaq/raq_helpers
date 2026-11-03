"""Regression coverage for the shared endpoint contract and its consumers."""

import unittest

from pydantic import BaseModel, Field, ValidationError

from raq_helpers.client import EndpointFactory
from raq_helpers.codegen import EndpointGenerator, ModelGenerator, PackageGenerator
from raq_helpers.models import Endpoint, EndpointOperation, ModelReference, PreparedRequest, ResponseCollection
from raq_helpers.models.endpoints import HTTPMethod


class Item(BaseModel):
    id: int


class CustomResponses(ResponseCollection):
    custom_success: ModelReference | None = Field(None, alias="299")


class EndpointModelTests(unittest.TestCase):
    def definition(self):
        return {
            "endpoint": "/items",
            "method": {
                "method": "get",
                "operationId": "listItems",
                "request": {"ref": "", "model": None},
                "responses": {"200": {"ref": "Item", "model": "Item"}},
            },
        }

    def test_typed_loading_round_trip_and_compact_repr(self):
        context = {"models": {"Item": Item}}
        endpoint = Endpoint.model_validate(self.definition(), context=context)
        self.assertIsInstance(endpoint.method, EndpointOperation)
        self.assertEqual(endpoint.method.method, "GET")
        success = endpoint.method.responses.success
        assert success is not None
        self.assertIs(success.model, Item)
        self.assertEqual(
            repr(endpoint),
            "Endpoint(endpoint='/items', id='listItems', method='GET', codes=['200'])",
        )
        restored = Endpoint.model_validate_json(endpoint.model_dump_json(by_alias=True), context=context)
        self.assertEqual(restored, endpoint)
        factory = EndpointFactory([restored])
        self.assertIs(factory.get_endpoint("/items", "get"), restored)
        self.assertIsInstance(factory.build_endpoint_request(restored, {}), PreparedRequest)

    def test_custom_response_collection_preserved_during_loading(self):
        definition = self.definition()
        definition["method"]["responses"] = {"299": {"ref": "Item", "model": "Item"}}
        endpoint = Endpoint[CustomResponses].model_validate(
            definition, context={"models": {"Item": Item}}
        )
        self.assertIsInstance(endpoint.method.responses, CustomResponses)
        success = endpoint.method.responses.custom_success
        assert success is not None
        self.assertIs(success.model, Item)

    def test_invalid_method_and_extra_fields_are_rejected(self):
        for change in ({"method": "INVALID"}, {"unexpected": True}):
            definition = self.definition()
            definition["method"].update(change)
            with self.assertRaises(ValidationError):
                Endpoint.model_validate(definition, context={"models": {"Item": Item}})


if __name__ == "__main__":
    unittest.main()
