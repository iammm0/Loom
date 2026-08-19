from __future__ import annotations

from threading import Lock
from typing import Any

_lock = Lock()
_bags: dict[str, dict[str, Any]] = {}


def runtime_bag(task_id: str) -> dict[str, Any]:
    with _lock:
        return _bags.setdefault(task_id, {})


def clear_runtime_bag(task_id: str) -> None:
    with _lock:
        _bags.pop(task_id, None)
