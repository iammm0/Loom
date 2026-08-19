from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field

from app.models.schema import VideoParams


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None


@dataclass
class ToolContext:
    task_id: str
    params: VideoParams
    extras: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str
    description: str

    def run(self, ctx: ToolContext) -> ToolResult:
        ...


@dataclass
class FunctionTool:
    name: str
    description: str
    handler: Callable[[ToolContext], ToolResult]

    def run(self, ctx: ToolContext) -> ToolResult:
        return self.handler(ctx)
