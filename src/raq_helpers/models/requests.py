"""Validated request data passed between endpoint building and execution."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .endpoints import HTTPMethod


class PreparedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: HTTPMethod
    url: str
    validated_input: BaseModel | None = None
    params: dict[str, Any] = Field(default_factory=dict)
