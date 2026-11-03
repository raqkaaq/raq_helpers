"""Finalization is explicit, validates names, and preserves prior output on failure."""

import importlib
import sys

import pytest

from raq_helpers.codegen import PackageGenerator


def test_generated_package_exports_and_aliases(tmp_path, monkeypatch):
    output = tmp_path / "exports_fixture"
    output.mkdir()
    (output / "one.py").write_text("VALUE = object()\n")
    (output / "two.py").write_text("VALUE = object()\n")
    path = PackageGenerator(output_dir=output).generate(exports={"one": (("VALUE", "FIRST"),), "two": (("VALUE", "SECOND"),)})
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        package = importlib.import_module("exports_fixture")
        assert package.FIRST is not package.SECOND
        assert package.__all__ == ["FIRST", "SECOND"]
        assert package.__generator_version__
    finally:
        for name in list(sys.modules):
            if name == "exports_fixture" or name.startswith("exports_fixture."):
                del sys.modules[name]
    assert "Generated at:" in path.read_text()


@pytest.mark.parametrize("exports", [
    {"missing": ("VALUE",)}, {"../one": ("VALUE",)}, {"": ("VALUE",)},
    {"one.class": ("VALUE",)}, {"__init__": ("VALUE",)},
    {"one": ("bad-name",)}, {"one": (("VALUE", "class"),)},
    {"one": (("VALUE", "__all__"),)}, {"one": (("VALUE", "__generator_version__"),)},
    {"one": ("VALUE", ("VALUE", "VALUE"))},
    {"one": ("VALUE",), "two": ("VALUE",)},
])
def test_invalid_exports_do_not_overwrite_initializer(tmp_path, exports):
    (tmp_path / "one.py").write_text("VALUE = 1\n")
    (tmp_path / "two.py").write_text("VALUE = 2\n")
    initializer = tmp_path / "__init__.py"
    initializer.write_text("# keep existing output\n")
    with pytest.raises(ValueError):
        PackageGenerator(output_dir=tmp_path).generate(exports=exports)
    assert initializer.read_text() == "# keep existing output\n"


def test_nested_module_and_empty_exports(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "__init__.py").write_text("VALUE = 1\n")
    initializer = PackageGenerator(output_dir=tmp_path).generate(exports={"nested": ("VALUE",)})
    assert "from .nested import VALUE" in initializer.read_text()
    initializer = PackageGenerator(output_dir=tmp_path).generate(exports={})
    compile(initializer.read_text(), str(initializer), "exec")


def test_custom_header_and_regeneration_replace_exports(tmp_path):
    class Custom(PackageGenerator):
        def generated_file_header(self):
            return "# Custom package header.\n"
    (tmp_path / "one.py").write_text("VALUE = 1\n")
    generator = Custom(output_dir=tmp_path)
    generator.generate(exports={"one": ("VALUE",)})
    output = generator.generate(exports={})
    assert output.read_text().startswith(generator.generated_file_header())
    assert "from .one" not in output.read_text()
