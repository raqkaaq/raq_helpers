"""Index and resolve endpoint definitions by path and HTTP method."""

import json
from collections.abc import Iterable, Mapping
from string import Formatter
from urllib.parse import quote

from ..models.endpoints import Endpoint, ResponseCollection
from ..models.requests import PreparedRequest


class EndpointFactory:
    RESPONSE_COLLECTION = ResponseCollection

    def __init__(
        self,
        endpoints: Iterable[Endpoint],
        *,
        registry: Mapping[str, type] | None = None,
    ):
        self.registry = registry if registry is not None else {}
        self._endpoints: dict[tuple[str, str], Endpoint] = {}
        for endpoint in endpoints:
            if not isinstance(endpoint, Endpoint):
                raise TypeError("EndpointFactory requires Endpoint models; use from_json() to load JSON.")
            path = endpoint.endpoint
            method = endpoint.method.method
            key = (path, method)
            if key in self._endpoints:
                raise ValueError(f"Duplicate endpoint definition: {method} {path}")
            self._endpoints[key] = endpoint

    @classmethod
    def from_json(cls, data: str | bytes, *, registry: Mapping[str, type]):
        """Validate serialized definitions into typed endpoints before indexing."""
        definitions = json.loads(data)
        if not isinstance(definitions, list):
            raise ValueError("Endpoint JSON must contain a list of definitions.")
        endpoint_type = Endpoint[cls.RESPONSE_COLLECTION]
        endpoints = [
            endpoint_type.model_validate(item, context={"models": registry})
            for item in definitions
        ]
        return cls(endpoints, registry=registry)

    def prepare_request_values(self, endpoint: Endpoint, params: dict) -> dict:
        """Prepare inputs before validation; subclasses may inject API defaults."""
        return dict(params)

    def build_endpoint_request(
        self, endpoint: Endpoint, params: dict
    ) -> PreparedRequest:
        """Validate inputs and build a path/query request without sending it."""
        if endpoint.method.payload != "params":
            raise ValueError("Only path/query requests are supported by this builder yet.")
        definition = endpoint.method.request
        values = self.prepare_request_values(endpoint, params)
        if definition.model is None:
            if values:
                raise ValueError("This operation declares no request inputs.")
            if any(name is not None for _, name, _, _ in Formatter().parse(endpoint.endpoint)):
                raise ValueError("Path parameters require a request model.")
            return PreparedRequest(method=endpoint.method.method, url=endpoint.endpoint)
        validated = definition.model.model_validate(values, extra="forbid")
        query = validated.model_dump(mode="json", by_alias=True, exclude_none=True)
        path_values = {
            name: quote(str(query.pop(name)), safe="")
            for name in dict.fromkeys(
                name for _, name, _, _ in Formatter().parse(endpoint.endpoint)
                if name is not None
            )
        }
        url = endpoint.endpoint.format(**path_values)
        return PreparedRequest(
            method=endpoint.method.method,
            url=url,
            validated_input=validated,
            params=query,
        )

    def decorate_api_call(self, client, call):
        """Add API decorators outside retries; the default leaves the call unchanged."""
        return call

    def decorate_endpoint_call(self, client, call, responses):
        """Compose shared client behavior; subclasses may add API-specific decorators."""
        return client.pagination(
            client.parse_as(responses)(
                self.decorate_api_call(
                    client,
                    client.retry(
                        client.cache(call)
                    ),
                )
            )
        )

    def process_endpoint_response(self, response, request: PreparedRequest):
        """Return the HTTP response unchanged; subclasses may attach request metadata."""
        return response

    def get_endpoint_responses(self, endpoint: Endpoint) -> ResponseCollection:
        """Resolve declared response models; subclasses may customize selection."""
        return endpoint.method.responses

    def create_endpoint(self, client, path: str, method: str | None = None):
        """Build a decorated async callable for the selected operation."""
        endpoint = self.get_endpoint(path, method)
        responses = self.get_endpoint_responses(endpoint)

        async def call(owner, **params):
            request = self.build_endpoint_request(endpoint, params)
            response = await owner.client.request(
                method=request.method,
                url=request.url,
                params=request.params,
                follow_redirects=True,
            )
            return self.process_endpoint_response(response, request)

        decorated = self.decorate_endpoint_call(client, call, responses)
        return decorated.__get__(client, type(client))

    def get_endpoint(self, path: str, method: str | None = None) -> Endpoint:
        """Resolve an operation, requiring a method when a path is ambiguous."""
        methods = {
            endpoint_method: definition
            for (endpoint_path, endpoint_method), definition in self._endpoints.items()
            if endpoint_path == path
        }
        if not methods:
            raise ValueError(f"Endpoint {path!r} not found.")
        if method is None:
            if len(methods) != 1:
                raise ValueError(
                    f"Ambiguous HTTP method for {path!r}; specify a method from: "
                    + ", ".join(sorted(methods))
                )
            return next(iter(methods.values()))
        normalized = method.upper()
        if normalized not in methods:
            raise ValueError(
                f"Endpoint {normalized} {path} not found; available methods: "
                + ", ".join(sorted(methods))
            )
        return methods[normalized]
