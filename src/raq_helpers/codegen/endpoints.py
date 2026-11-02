"""Map OpenAPI operations to generated request and response models."""

import inspect
import json
import keyword
import logging
from pathlib import Path
from string import Formatter as PathFormatter
from typing import Mapping

from ..client.client import Client
from ..models.endpoints import Endpoint, EndpointOperation, ModelReference, ResponseCollection
from .base import GeneratorBase


logger = logging.getLogger(__name__)


class EndpointGenerator(GeneratorBase):
    HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
    RESPONSE_COLLECTION = ResponseCollection
    RESERVED_WRAPPER_NAMES = frozenset(dir(Client)) | {
        "url", "storage", "policy", "transport", "client", "factory", "endpoint_factory",
    }

    def __init__(
        self,
        *,
        schema: dict,
        metadata: dict,
        registry: Mapping[str, type],
        output_dir: Path = Path("./generated"),
        endpoints_filename: str = "metadata/endpoints.json",
        wrappers_filename: str = "endpoints.py",
    ):
        self.schema = schema
        self.registry = registry
        self.output_path = Path(output_dir) / endpoints_filename
        self.wrappers_path = Path(output_dir) / wrappers_filename
        self._models_by_source: dict[tuple[str, ...], list[str]] = {}
        for entry in metadata["models"]:
            source = tuple(entry.get("source_path") or [])
            self._models_by_source.setdefault(source, []).append(entry["class_name"])

    def excluded_wrapper_parameters(self) -> tuple[str, ...]:
        """Names omitted from wrapper documentation, without affecting requests."""
        return ()

    def _resolve(self, value: dict, location: tuple[str, ...]) -> tuple[dict, tuple[str, ...]]:
        visited = set()
        while "$ref" in value:
            reference = value["$ref"]
            if not reference.startswith("#/"):
                raise ValueError(f"External references are not supported: {reference!r}")
            location = tuple(
                part.replace("~1", "/").replace("~0", "~")
                for part in reference[2:].split("/")
            )
            if location in visited:
                raise ValueError(f"Cyclic reference: {reference!r}")
            visited.add(location)
            value = self.schema
            for part in location:
                value = value[part]
        return value, location

    def _model_reference(self, location: tuple[str, ...], ref: str | None = None) -> ModelReference:
        names = self._models_by_source.get(location, [])
        if len(names) != 1:
            raise ValueError(f"Expected one generated model at {location!r}, found {len(names)}")
        name = names[0]
        return ModelReference.model_validate(
            {"ref": ref if ref is not None else name, "model": name},
            context={"models": self.registry},
        )

    def _build_endpoint(self, path: str, method: str, operation: dict) -> Endpoint:
        # This first mapping stage preserves the existing path/query request shape.
        # Body serialization will be added together with its runtime contract.
        if "requestBody" in operation:
            raise ValueError("Request bodies are not supported by this mapping stage yet.")
        path_item = self.schema["paths"][path]
        parameters = [*path_item.get("parameters", []), *operation.get("parameters", [])]
        for parameter in parameters:
            parameter, _ = self._resolve(parameter, ())
            if parameter["in"] not in {"path", "query"}:
                raise ValueError(f"Unsupported parameter location: {parameter['in']!r}")

        location = ("paths", path, method)
        if not parameters and not self._models_by_source.get(location):
            request = ModelReference(ref="", model=None)
        else:
            request = self._model_reference(location)
        definitions = {}
        for status, original in operation["responses"].items():
            response, location = self._resolve(
                original, ("paths", path, method, "responses", status)
            )
            content = response.get("content", {})
            if not content:
                definitions[status] = ModelReference(
                    ref=original.get("$ref", "").split("/")[-1], no_content=True
                )
                continue
            if "application/json" not in content:
                raise ValueError(f"Response {status} has no supported application/json schema.")
            body = content["application/json"]["schema"]
            _, location = self._resolve(body, location + ("content", "application/json", "schema"))
            ref = original["$ref"].split("/")[-1] if "$ref" in original else None
            definition = self._model_reference(location, ref)
            definitions[status] = definition

        responses = self.RESPONSE_COLLECTION.model_validate(definitions)
        normalized = EndpointOperation[self.RESPONSE_COLLECTION](
            method=method.upper(),
            **{
                key: operation[key]
                for key in ("operationId", "summary", "description", "tags")
                if key in operation
            },
            request=request,
            responses=responses,
            payload="params",
        )
        return Endpoint[self.RESPONSE_COLLECTION](endpoint=path, method=normalized)

    def generate_mapping(self) -> list[Endpoint]:
        """Write endpoint definitions only after every operation maps successfully."""
        endpoints = []
        for path, path_item in self.schema.get("paths", {}).items():
            if "$ref" in path_item:
                raise ValueError(f"Referenced path items are not supported yet: {path!r}")
            for method, operation in path_item.items():
                if method.lower() not in self.HTTP_METHODS:
                    continue
                try:
                    endpoints.append(self._build_endpoint(path, method.lower(), operation))
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"Failed to map {method.upper()} {path}: {error}") from error
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(json.dumps([item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in endpoints], indent=4) + "\n", encoding="utf-8")
        return endpoints

    def _group_routes(self, endpoints):
        """Group list/detail routes under valid Python method names."""
        groups = {}
        for item in endpoints:
            path = item.endpoint
            parts = list(PathFormatter().parse(path))

            fixed = "".join(literal for literal, _, _, _ in parts)
            name = "_".join(fixed.strip("/").split("/")).replace("-", "_")

            if keyword.iskeyword(name):
                name += "_"
            if not name.isidentifier():
                raise ValueError(f"Cannot generate a method name for {path!r}")
            if name in self.RESERVED_WRAPPER_NAMES:
                raise ValueError(f"Wrapper name {name!r} for {path!r} conflicts with a client member.")

            placeholders = tuple(
                field for _, field, _, _ in parts
                if field is not None
            )
            for field in placeholders:
                if (
                    not field.isidentifier()
                    or keyword.iskeyword(field)
                    or field in {"self", "params"}
                ):
                    raise ValueError(f"Unsupported path parameter {field!r}")

            groups.setdefault(name, []).append((path, placeholders, item))

        return groups

    def _wrapper_details(self, variants):
        parameter_details = []
        endpoint_details = []

        for path, _, item in variants:
            request_model = item.method.request.model
            request_name = request_model.__name__ if request_model is not None else None

            endpoint_details.append(f"{path} -> {request_name}")
            parameter_details.append(f"{path}:")

            visible_fields = {
                field_name: field
                for field_name, field in (request_model.model_fields if request_model is not None else {}).items()
                if field_name not in self.excluded_wrapper_parameters()
            }

            if not visible_fields:
                parameter_details.append("  No caller-supplied parameters.")

            for field_name, field in visible_fields.items():
                annotation = inspect.formatannotation(field.annotation)
                default = (
                    " (required)"
                    if field.is_required()
                    else f" = {field.default!r}"
                )
                parameter_details.append(
                    f"  {field_name}: {annotation}{default}"
                )

            parameter_details.append("")

        parameters = "\n".join(parameter_details).rstrip()
        endpoint_summary = "\n".join(endpoint_details)

        return parameters, endpoint_summary

    def _render_wrapper(self, name, variants):
        lines = []
        logger.debug("Generating endpoint wrapper for %s", name)
        variants = sorted(variants, key=lambda variant: (len(variant[1]), variant[0]))

        fields = list(dict.fromkeys(
            field
            for _, placeholders, _ in variants
            for field in placeholders
        ))
        for path, placeholders, item in variants:
            request_model = item.method.request.model
            if "http_method" in placeholders or any(
                field_name == "http_method" or field.alias == "http_method"
                for field_name, field in (request_model.model_fields if request_model is not None else {}).items()
            ):
                raise ValueError(f"API input on {path!r} conflicts with wrapper selector 'http_method'")

        routes = {}
        for path, placeholders, item in variants:
            key = tuple(field for field in fields if field in placeholders)
            method = item.method.method
            methods = routes.setdefault(key, {})
            if method in methods:
                raise ValueError(
                    f"Ambiguous routes for {name!r}: {method} {methods[method]!r}, {path!r}"
                )
            methods[method] = path

        args = ", ".join([
            "self",
            *(f"{field}=None" for field in fields),
            "*",
            "http_method=None",
            "**params",
        ])
        lines.append(f"    async def {name}({args}):")
        values = ", ".join(f"{field!r}: {field}" for field in fields)
        lines.extend([
            f"        path_params = {{{values}}}",
            "        supplied = {",
            "            key: value for key, value in path_params.items()",
            "            if value is not None",
            "        }",
            f"        methods = {routes!r}.get(tuple(supplied))",
            "        if methods is None:",
            "            raise TypeError(",
            f'                "Unsupported path-parameter combination for {name}: "',
            '                + ", ".join(supplied)',
            "            )",
            "        if http_method is None:",
            "            if len(methods) != 1:",
            "                raise TypeError(",
            f'                    "Ambiguous HTTP method for {name}; specify http_method from: "',
            '                    + ", ".join(sorted(methods))',
            "                )",
            "            method = next(iter(methods))",
            "        else:",
            "            method = http_method.upper()",
            "        if method not in methods:",
            "            raise ValueError(",
            f'                "Unsupported HTTP method for {name}: " + repr(method)',
            '                + "; available methods: " + ", ".join(sorted(methods))',
            "            )",
            "        path = methods[method]",
            "        params.update(supplied)",
            "        return await self.endpoint_factory(path, method=method)(**params)",
        ])

        parameters, endpoint_summary = self._wrapper_details(variants)
        lines.extend([
            f"    {name}.parameters = {parameters!r}",
            f"    {name}.endpoints = {endpoint_summary!r}",
            "",
        ])
        return lines

    def generate_wrappers(self, endpoints: list[Endpoint]) -> Path:
        """Generate client wrappers from mapped endpoint definitions."""
        lines = [
            self.generated_file_header().rstrip(),
            "",
            self.generated_file_metadata().rstrip(),
            "",
            "class GeneratedEndpoints:",
        ]
        for name, variants in sorted(self._group_routes(endpoints).items()):
            lines.extend(self._render_wrapper(name, variants))
        if lines[-1] == "class GeneratedEndpoints:":
            lines.append("    pass")
        self.wrappers_path.parent.mkdir(parents=True, exist_ok=True)
        self.wrappers_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("Endpoint wrappers generated in %s", self.wrappers_path)
        return self.wrappers_path
