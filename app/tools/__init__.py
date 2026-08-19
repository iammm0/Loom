from __future__ import annotations

from app.tools.base import FunctionTool, Tool, ToolContext, ToolResult
from app.tools.edit import EDIT_TOOLS
from app.tools.production import PRODUCTION_TOOLS

_REGISTRY: dict[str, FunctionTool] = {
    tool.name: tool for tool in [*PRODUCTION_TOOLS, *EDIT_TOOLS]
}


def get_tool(name: str) -> Tool:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown tool: {name}") from exc


def run_tool(name: str, ctx: ToolContext) -> ToolResult:
    return get_tool(name).run(ctx)


def list_tools() -> list[FunctionTool]:
    return list(_REGISTRY.values())
