from __future__ import annotations

from app.agent.graph import get_graph
from app.agent.runtime import clear_runtime_bag
from app.models.schema import VideoParams


def run_agent(task_id: str, params: VideoParams, stop_at: str = "video"):
    graph = get_graph()
    try:
        state = graph.invoke(
            {
                "task_id": task_id,
                "stop_at": stop_at or "video",
                "params": params.model_dump(mode="json", warnings=False),
                "halt": False,
            }
        )
    finally:
        clear_runtime_bag(task_id)
    if isinstance(state, dict):
        return state.get("result")
    return state
