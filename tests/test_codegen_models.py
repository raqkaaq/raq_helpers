"""Actual model/registry output and source-independent generation configuration."""

import ast
import importlib
import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
from datamodel_code_generator import InputFileType, OpenAPIScope
from pydantic import ValidationError

from raq_helpers.codegen import GeneratorBase, ModelGenerator
import raq_helpers.codegen.base as base
import raq_helpers.codegen.models as module


def test_real_generated_models_registry_and_header(generated_api):
    artifacts = generated_api
    metadata = json.loads(artifacts.models.metadata_path.read_text())
    assert set(artifacts.registry) == {entry["class_name"] for entry in metadata["models"]}
    assert artifacts.package.MODEL_REGISTRY["Item"] is artifacts.package.Item
    assert artifacts.package.Item(id="3").id == 3
    with pytest.raises(ValidationError):
        artifacts.package.Item(id="bad")
    source = artifacts.models.output_path.read_text()
    assert "MODEL_REGISTRY =" not in source
    assert "from __future__ import annotations" in source
    assert artifacts.models.registry_path.read_text().count("MODEL_REGISTRY =") == 1
    assert artifacts.package.__generator_version__ == GeneratorBase.generator_version()
    for path in artifacts.output.glob("*.py"):
        compile(path.read_text(), str(path), "exec")
        assert "# Generated at:" in path.read_text()


def test_schema_isolation_and_hooks_run_in_order(tmp_path, api_schema):
    original = deepcopy(api_schema)
    events = []
    class Custom(ModelGenerator):
        def supplement_schema(self, schema):
            events.append("supplement")
            schema["components"]["schemas"]["Item"]["properties"]["label"] = {"type": "string"}
        def postprocess_models(self, output):
            events.append("postprocess")
            assert output.exists() and not self.registry_path.exists()
            output.write_text(output.read_text() + "\n# project customization\n")
        def generated_file_header(self):
            return "# Project-specific generated file.\n"
    generator = Custom(output_dir=tmp_path)
    prepared = generator.generate_models(api_schema, InputFileType.OpenAPI)
    assert api_schema == original and prepared is not api_schema
    assert "label" in prepared["components"]["schemas"]["Item"]["properties"]
    assert events == ["supplement", "postprocess"]
    assert "project customization" in generator.output_path.read_text()
    assert generator.output_path.read_text().startswith(generator.generated_file_header())
    assert generator.registry_path.read_text().startswith(generator.generated_file_header())


def test_custom_paths_options_and_original_defaults(tmp_path, monkeypatch):
    actual_generate = module.generate
    observed = []
    def record(**kwargs):
        observed.append(kwargs)
        return actual_generate(**kwargs)
    monkeypatch.setattr(module, "generate", record)
    options = {"field_constraints": False}
    generator = ModelGenerator(output_dir=tmp_path / "custom", models_filename="custom.py", metadata_filename="intermediates/custom.json", options=options)
    options["field_constraints"] = True
    generator.generate_models({"title": "Example", "type": "object", "properties": {"id": {"type": "integer"}}}, InputFileType.JsonSchema)
    assert generator.output_path.is_file() and generator.metadata_path.is_file()
    assert generator.registry_path.name == "custom_registry.py"
    assert observed[0]["field_constraints"] is False
    assert module.GENERAL_OPTIONS["field_constraints"] is True
    assert observed[0]["snake_case_field"] is True


def test_multiple_model_modules_have_independent_registries(tmp_path, monkeypatch):
    output = tmp_path / "multiple_models"
    for filename, title in (("one.py", "First"), ("two.py", "Second")):
        generator = ModelGenerator(output_dir=output, models_filename=filename, metadata_filename=f"metadata/{title}.json")
        generator.generate_models({"title": title, "type": "object", "properties": {"id": {"type": "integer"}}}, InputFileType.JsonSchema)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        importlib.invalidate_caches()
        first = importlib.import_module("multiple_models.one_registry").MODEL_REGISTRY
        second = importlib.import_module("multiple_models.two_registry").MODEL_REGISTRY
        assert set(first) == {"First"} and set(second) == {"Second"}
        assert first["First"] is not second["Second"]
    finally:
        for name in list(sys.modules):
            if name == "multiple_models" or name.startswith("multiple_models."):
                del sys.modules[name]


def test_regeneration_replaces_registry_without_duplicate_metadata(tmp_path):
    generator = ModelGenerator(output_dir=tmp_path)
    for title in ("First", "Second"):
        generator.generate_models({"title": title, "type": "object"}, InputFileType.JsonSchema)
    assert "'Second': Second" in generator.registry_path.read_text()
    assert "First" not in generator.registry_path.read_text()
    assert generator.registry_path.read_text().count("__generator_version__ =") == 1
    assert generator.output_path.read_text().count("__generator_version__ =") == 1


def test_default_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    generator = ModelGenerator()
    assert generator.output_path == Path("generated/models.py")
    assert generator.metadata_path == Path("generated/metadata/model_metadata.json")
    assert generator.registry_path == Path("generated/registry.py")


def test_empty_registry_is_importable_and_has_version_metadata(tmp_path):
    import runpy
    generator = ModelGenerator(output_dir=tmp_path)
    generator.metadata_path.parent.mkdir(parents=True)
    generator.metadata_path.write_text(json.dumps({"models": []}))
    generator.generate_registry()
    namespace = runpy.run_path(str(generator.registry_path))
    assert namespace["MODEL_REGISTRY"] == {}
    assert namespace["__generator_version__"] == GeneratorBase.generator_version()
    assert "from .models import" not in generator.registry_path.read_text()


@pytest.mark.parametrize("schema,expected", [
    ({}, []),
    ({"components": {"schemas": {"Item": {}}}}, [OpenAPIScope.Schemas]),
    ({"paths": {"/items": {"get": {"responses": {}}}}}, [OpenAPIScope.Paths]),
    ({"paths": {"/items": {"parameters": [{}], "get": {"responses": {}}}}}, [OpenAPIScope.Paths, OpenAPIScope.Parameters]),
    ({"paths": {"/items": {"post": {"parameters": [{}], "tags": ["items"]}}}}, [OpenAPIScope.Paths, OpenAPIScope.Parameters, OpenAPIScope.Tags]),
    ({"webhooks": {"event": {}}}, [OpenAPIScope.Webhooks]),
    ({"components": {"requestBodies": {"Body": {}}}}, [OpenAPIScope.RequestBodies]),
])
def test_openapi_scope_detection(schema, expected):
    assert ModelGenerator.openapi_scopes(schema) == expected


def test_default_and_custom_generator_headers(monkeypatch):
    header = GeneratorBase().generated_file_header()
    assert "raq_helpers.codegen.base.GeneratorBase" in header
    timestamp = next(line.removeprefix("# Generated at: ") for line in header.splitlines() if line.startswith("# Generated at:"))
    assert datetime.fromisoformat(timestamp).utcoffset() == timedelta(0)
    monkeypatch.setattr(base, "version", lambda name: "9.8.7")
    assert "9.8.7" in GeneratorBase().generated_file_header()
    assert GeneratorBase().generated_file_metadata() == "__generator_version__ = '9.8.7'\n"
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(base, "version", missing)
    assert GeneratorBase.generator_version() == "unknown"
