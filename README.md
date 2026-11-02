# raq_helpers

Shared async HTTP client and OpenAPI generation tools. API-specific configuration
belongs to the caller; generators accept a loaded schema and explicit output paths.

```python
import json
from pathlib import Path
from datamodel_code_generator import InputFileType
from raq_helpers.codegen import ModelGenerator, EndpointGenerator

models = ModelGenerator(output_dir=Path("generated"))
prepared = models.generate_models(schema, InputFileType.OpenAPI)

from generated.registry import MODEL_REGISTRY

endpoints = EndpointGenerator(
    schema=prepared,
    metadata=json.loads(models.metadata_path.read_text()),
    registry=MODEL_REGISTRY,
)
definitions = endpoints.generate_mapping()
endpoints.generate_wrappers(definitions)
```

After all generation finishes, write the package initializer with explicit exports:

```python
from raq_helpers.codegen import PackageGenerator

PackageGenerator().generate(exports={
    "models": ("Work", "WorksListResponse"),
    "registry": ("MODEL_REGISTRY",),
    "endpoints": ("GeneratedEndpoints",),
})
```

An export tuple such as `("MODEL_REGISTRY", "OPENALEX_MODEL_REGISTRY")` aliases a
name when multiple modules expose the same symbol. Duplicate public names and
missing modules are rejected before writing `__init__.py`.

Outputs are `generated/models.py`, `registry.py`, `endpoints.py`, and the JSON
intermediates in `generated/metadata/`. The registry uses relative imports: import
it as `generated.registry`. Generated Python exposes `__generator_version__` and
has a generator/version/UTC timestamp header. Use a fresh process or reload model
and registry modules after regenerating already-imported files.
Custom model filenames get separate registries: `openalex.py` produces
`openalex_registry.py`, while `models.py` retains `registry.py`. Supply distinct
metadata filenames when generating multiple model modules in one directory.

For runtime use, compose the generated wrapper mixin with `Client`, construct
`EndpointFactory(definitions, registry=MODEL_REGISTRY)`, and provide this adapter:

```python
def endpoint_factory(self, path, method=None):
    return self.factory.create_endpoint(self, path, method)
```

`generate_mapping()` returns typed `Endpoint` objects containing
`EndpointOperation`, `ModelReference`, and `ResponseCollection` models. Generator
and factory code use their attributes; JSON is a serialization boundary. To load
saved definitions, validate them through the factory loader:

```python
from raq_helpers.client import EndpointFactory

factory = EndpointFactory.from_json(
    Path("generated/metadata/endpoints.json").read_text(),
    registry=MODEL_REGISTRY,
)
```

The factory constructor accepts endpoint models, not raw dictionaries. Custom
response types are preserved using `Endpoint[CustomResponses]`; generator/factory
subclasses select their loader type through `RESPONSE_COLLECTION`. Endpoint repr
shows the path, operation ID, HTTP method, and populated response codes.

`build_endpoint_request()` returns `PreparedRequest` with `method`, `url`,
`validated_input`, and serialized query `params`. Execution and
`process_endpoint_response(response, request)` receive this model. An OpenAlex
metadata override should attach `request.validated_input` as `openalex_request`.
Schema documents and upstream generator metadata remain dictionaries.

Lookup uses path plus HTTP method. Generated wrappers accept keyword-only
`http_method`; omitted methods must be unambiguous. Definitions without request
models accept no inputs. Explicit no-content responses return `None`; populated
status definitions determine allowed responses, not the full collection of fields.

Model-generator overrides: `supplement_schema`, `postprocess_models`, and
`generated_file_header`. Endpoint-generator overrides: `excluded_wrapper_parameters`
and `RESPONSE_COLLECTION`. Factory overrides: `prepare_request_values`,
`get_endpoint_responses`, `process_endpoint_response`, and `decorate_api_call`.
Override `decorate_endpoint_call` only when the whole decorator order must change.

OpenAlex integration is left to its caller: environment/authentication, credit
checks in `should_retry_response`, cost decoration, request metadata, parsed-response
processing, and pagination hooks. `handle_validation_error` handles validation
errors from the wrapped call; subsequent response parsing errors propagate.

Known integration gap: the builder serializes every validated non-null field.
OpenAlex injects `api_key` for validation but excludes it from serialized parameters;
its factory must customize request building accordingly. Python field names with
aliases are not automatically accepted; use wire aliases such as `search.exact`.

Current generation supports local references, path/query inputs, modeled JSON
responses, and explicit no-content responses. Request bodies, header/cookie inputs,
external references, OpenAPI response ranges/default, and advanced parameter
serialization are not implemented. Unsupported declared input/content shapes fail
generation. OpenAlex mock checks do not establish support for every OpenAPI feature.

Wrapper names that conflict with shared client members are rejected. Subclasses
can extend `RESERVED_WRAPPER_NAMES` for their own members. Successful declared HEAD
responses return `None`; HEAD errors preserve their HTTP exceptions.
