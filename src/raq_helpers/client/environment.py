from datetime import timedelta
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from pathlib import Path
from dotenv import load_dotenv
import os
import pytimeparse2

logger = logging.getLogger(__name__)
def parse_file(value: str) -> Path:
    if not value:
        raise ValueError("File path cannot be empty.")
    return Path(value).expanduser()

def parse_ttl(value: str):
    parsed = pytimeparse2.parse(value)
    if parsed is None:
        raise ValueError(f"Invalid TTL format: {value}")
    seconds = (parsed.total_seconds() if isinstance(parsed, timedelta) else parsed)
    ttl = int(seconds)
    if ttl <= 0:
        raise ValueError(f"TTL must be a positive integer, got: {ttl}")
    return ttl

@dataclass(frozen=True)
class EnvironmentVariable:
    env_name: str
    default: Any = None
    parse: Callable[[str], Any] = str
    required: bool = False


class Environment:
    variables: dict[str, EnvironmentVariable] = {}
    _instances = {}
    env_file: Path | None = None
    #singleton pattern to ensure only one instance of Environment exists per subclass of it

    def __new__(cls, env_file: Path | None = None):
        if cls not in cls._instances:
            instance = super().__new__(cls)
            instance.env_file = (
                env_file.expanduser().resolve()
                if env_file is not None else None
            )
            instance.load_from_env()
            cls._instances[cls] = instance
        return cls._instances[cls]

    def load_from_env(self):
        if self.env_file is None:
            logger.error("env_file not set on class initialization.")
            raise FileNotFoundError("Please ensure to set the env_file: Path")

        load_dotenv(dotenv_path=self.env_file)
        for name, variable in self.variables.items():
            raw = os.getenv(variable.env_name, variable.default)
            if raw is None:
                value = variable.default
            else:
                try:
                    value = variable.parse(raw)
                except (ValueError, TypeError) as e:
                    raise ValueError(
                        f"Invalid Valie for {variable.env_name}"
                    ) from e

            if variable.required and value is None:
                raise ValueError(
                    f"{variable.env_name} environment variable is not set."
                )
            setattr(self, name, value)
        logger.debug(f"Loaded configuration for {type(self).__name__}")
