"""Generate Pydantic models and source metadata from a loaded schema."""

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from datamodel_code_generator import (
    DataModelType,
    DatetimeClassType,
    InputFileType,
    OpenAPIScope,
    generate,
)
from datamodel_code_generator.format import Formatter

from .base import GeneratorBase

logger = logging.getLogger(__name__)

GENERAL_OPTIONS: dict[str, Any] = {
    "output_model_type": DataModelType.PydanticV2BaseModel,
    "output_datetime_class": DatetimeClassType.Datetime,
    "formatters": [Formatter.BUILTIN],
    "use_schema_description": True,
    "reuse_model": False,
    "field_constraints": True,
    "snake_case_field": True,
    "deserialize_default_values": ["enum"],
}


class ModelGenerator(GeneratorBase):
    def __init__(
        self,
        *,
        output_dir: Path = Path("./generated"),
        models_filename: str = "models.py",
        metadata_filename: str = "metadata/model_metadata.json",
        options: Mapping[str, Any] | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_path = self.output_dir / models_filename
        registry_filename = (
            "registry.py" if self.output_path.name == "models.py"
            else f"{self.output_path.stem}_registry.py"
        )
        self.registry_path = self.output_path.with_name(registry_filename)
        if self.registry_path == self.output_path:
            raise ValueError("The model filename must differ from registry.py.")
        self.metadata_path = self.output_dir / metadata_filename
        self.options = deepcopy(dict(options)) if options is not None else {}

    def supplement_schema(self, schema: dict) -> None:
        """Customize the schema in place before model generation."""

    def postprocess_models(self, output: Path) -> None:
        """Customize the generated model file after generation."""

    def generate_registry(self) -> None:
        """Write a sibling registry module with explicit imports of generated classes."""
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        names = dict.fromkeys(entry["class_name"] for entry in metadata["models"])
        lines = [
            self.generated_file_header().rstrip(),
            "",
            self.generated_file_metadata().rstrip(),
            "",
        ]
        if names:
            lines.append(f"from .{self.output_path.stem} import (")
            lines.extend(f"    {name}," for name in names)
            lines.extend([")", ""])
        lines.append("MODEL_REGISTRY = {")
        lines.extend(f"    {name!r}: {name}," for name in names)
        lines.append("}")
        self.registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def openapi_scopes(schema):
        components = schema.get("components", {})
        paths = schema.get("paths", {})
        operations = [
            operation for path in paths.values()
            if isinstance(path, dict)
            for method, operation in path.items() if method.lower() in {
                "get", "post", "put", "delete", "patch", "options", "head", "trace"
            }
            and isinstance(operation, dict)
        ]
        scopes = []
        if components.get("schemas"):
            scopes.append(OpenAPIScope.Schemas)
        if operations:
            scopes.append(OpenAPIScope.Paths)
            if any(path.get("parameters") for path in paths.values()
                   if isinstance(path, dict)) or any(
                       operation.get("parameters") for operation in operations
                ):
                logger.debug("OpenAPI specification contains parameters.")
                scopes.append(OpenAPIScope.Parameters)
            if any(operation.get("tags") for operation in operations):
                scopes.append(OpenAPIScope.Tags)
        if schema.get("webhooks"):
            scopes.append(OpenAPIScope.Webhooks)
        if components.get("requestBodies"):
            scopes.append(OpenAPIScope.RequestBodies)
        return scopes

    def generate_models(self, schema: dict, input_type: InputFileType) -> dict:
        """Generate files and return the prepared schema for endpoint generation.

        The caller's schema is copied before supplementation. Fetching, environment
        loading, and persistence of the prepared schema belong to the caller.
        """
        prepared = deepcopy(schema)
        self.supplement_schema(prepared)
        options = deepcopy(GENERAL_OPTIONS)
        if input_type == InputFileType.OpenAPI:
            options.update(
                openapi_scopes=self.openapi_scopes(prepared),
                include_path_parameters=True,
            )
        options.update(deepcopy(self.options))
        options["custom_file_header"] = self.generated_file_header()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        generate(
            input_=json.dumps(prepared, indent=2),
            output=self.output_path,
            input_file_type=input_type,
            emit_model_metadata=self.metadata_path,
            **options,
        )
        self.postprocess_models(self.output_path)
        self.generate_registry()
        with self.output_path.open("a", encoding="utf-8") as output:
            output.write("\n\n" + self.generated_file_metadata())
        logger.info("Models generated successfully in %s", self.output_path)
        return prepared
