"""Shared environment contract, independent of any API's variable names."""

from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path

import pytest

from raq_helpers.client import Environment, EnvironmentVariable, parse_file, parse_ttl
import raq_helpers.client.environment as module


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(Environment, "_instances", {})
    monkeypatch.setattr(module.os, "environ", module.os.environ.copy())
    for name in ("RAQ_TEST_KEY", "RAQ_TEST_COUNT", "RAQ_TEST_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config_type():
    class Config(Environment):
        variables = {
            "key": EnvironmentVariable("RAQ_TEST_KEY", required=True),
            "count": EnvironmentVariable("RAQ_TEST_COUNT", default="3", parse=int),
            "path": EnvironmentVariable("RAQ_TEST_PATH", default="~/cache", parse=parse_file),
            "optional": EnvironmentVariable("RAQ_TEST_UNSET_OPTIONAL"),
        }
    return Config


def test_loads_dotenv_and_parses_defaults(config_type, tmp_path):
    file = tmp_path / "config.env"
    file.write_text("RAQ_TEST_KEY=from-file\nRAQ_TEST_COUNT=7\n")
    config = config_type(env_file=file)
    assert (config.key, config.count) == ("from-file", 7)
    assert config.path == Path.home() / "cache"
    assert config.optional is None
    assert config.env_file == file.resolve()


def test_process_environment_precedes_dotenv(config_type, tmp_path, monkeypatch):
    file = tmp_path / "config.env"
    file.write_text("RAQ_TEST_KEY=from-file\n")
    monkeypatch.setenv("RAQ_TEST_KEY", "process")
    assert config_type(env_file=file).key == "process"


def test_requires_env_file(config_type):
    with pytest.raises(FileNotFoundError, match="env_file"):
        config_type()


def test_required_variable_failure_does_not_cache_instance(config_type, tmp_path, monkeypatch):
    file = tmp_path / "missing.env"
    with pytest.raises(ValueError, match="RAQ_TEST_KEY environment variable is not set"):
        config_type(env_file=file)
    monkeypatch.setenv("RAQ_TEST_KEY", "fixed")
    assert config_type(env_file=file).key == "fixed"


def test_parse_error_preserves_cause(config_type, tmp_path, monkeypatch):
    monkeypatch.setenv("RAQ_TEST_KEY", "present")
    monkeypatch.setenv("RAQ_TEST_COUNT", "invalid")
    with pytest.raises(ValueError, match="RAQ_TEST_COUNT") as error:
        config_type(env_file=tmp_path / "missing.env")
    assert isinstance(error.value.__cause__, ValueError)


def test_singleton_per_subclass(config_type, tmp_path, monkeypatch):
    monkeypatch.setenv("RAQ_TEST_KEY", "first")
    first = config_type(env_file=tmp_path / "one.env")
    monkeypatch.setenv("RAQ_TEST_KEY", "second")
    assert config_type(env_file=tmp_path / "two.env") is first
    assert first.key == "first"
    class Other(config_type):
        pass
    other = Other(env_file=tmp_path / "other.env")
    assert other is not first and other.key == "second"


def test_reload_reparses_values(config_type, tmp_path, monkeypatch):
    monkeypatch.setenv("RAQ_TEST_KEY", "first")
    config = config_type(env_file=tmp_path / "missing.env")
    monkeypatch.setenv("RAQ_TEST_COUNT", "9")
    config.load_from_env()
    assert config.count == 9


def test_variable_definition_is_immutable():
    variable = EnvironmentVariable("NAME")
    with pytest.raises(FrozenInstanceError):
        variable.required = True


@pytest.mark.parametrize("value,expected", [("1d", 86400), ("2h", 7200), ("30m", 1800), ("1:30", 90), ("1.9s", 1)])
def test_ttl_parsing(value, expected):
    assert parse_ttl(value) == expected


@pytest.mark.parametrize("value", ["", "garbage", "0s", "-1h", "0.1s"])
def test_invalid_ttl(value):
    with pytest.raises(ValueError):
        parse_ttl(value)


def test_timedelta_parser_result(monkeypatch):
    monkeypatch.setattr(module.pytimeparse2, "parse", lambda value: timedelta(minutes=2))
    assert parse_ttl("example") == 120


@pytest.mark.parametrize("value,expected", [("~/cache", Path.home() / "cache"), ("relative/file", Path("relative/file")), ("/tmp/file", Path("/tmp/file"))])
def test_path_parser(value, expected):
    assert parse_file(value) == expected


def test_empty_path_rejected():
    with pytest.raises(ValueError, match="empty"):
        parse_file("")
