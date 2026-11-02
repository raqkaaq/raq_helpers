"""Finalize a generated Python package with explicit public exports."""

import keyword
from collections.abc import Mapping, Sequence
from pathlib import Path

from .base import GeneratorBase


class PackageGenerator(GeneratorBase):
    def __init__(self, *, output_dir: Path = Path("./generated")):
        self.output_dir = Path(output_dir)

    def generate(
        self, *, exports: Mapping[str, Sequence[str | tuple[str, str]]]
    ) -> Path:
        """Write __init__.py after generation; tuples specify (name, alias)."""
        lines = [
            self.generated_file_header().rstrip(),
            "",
            self.generated_file_metadata().rstrip(),
            "",
        ]
        names: list[str] = []
        reserved = {"__all__", "__generator_version__"}
        for module, symbols in exports.items():
            if not module or module == "__init__" or any(
                not part.isidentifier() or keyword.iskeyword(part)
                for part in module.split(".")
            ):
                raise ValueError(f"Invalid export module: {module!r}")
            module_path = self.output_dir.joinpath(*module.split("."))
            if not module_path.with_suffix(".py").is_file() and not (
                module_path / "__init__.py"
            ).is_file():
                raise ValueError(f"Generate module {module!r} before finalizing the package.")
            for symbol in symbols:
                name, alias = (symbol, symbol) if isinstance(symbol, str) else symbol
                for identifier in (name, alias):
                    if not identifier.isidentifier() or keyword.iskeyword(identifier):
                        raise ValueError(f"Invalid export name: {identifier!r}")
                if alias in reserved or alias in names:
                    raise ValueError(f"Conflicting package export: {alias!r}")
                names.append(alias)
                suffix = f" as {alias}" if alias != name else ""
                lines.append(f"from .{module} import {name}{suffix}")
        lines.extend(["", "__all__ = ["])
        lines.extend(f"    {name!r}," for name in names)
        lines.append("]")
        output = self.output_dir / "__init__.py"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output
