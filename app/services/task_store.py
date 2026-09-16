from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from loguru import logger

from app.config import config
from app.controllers.manager.base_manager import TaskQueueFullError
from app.models import const
from app.models.schema import VideoParams
from app.utils import utils


SCHEMA_VERSION = 2
DEFAULT_LEASE_SECONDS = 120
_ACTIVE_STATUSES = (
    const.TASK_STATUS_QUEUED,
    const.TASK_STATUS_PROCESSING,
    const.TASK_STATUS_CANCELLATION_REQUESTED,
)
_SEEDANCE_BILLING_FAILURE_MARKERS = (
    "overdue balance",
    "insufficient balance",
    "insufficient quota",
    "insufficient_quota",
    "quota exceeded",
    "quota has been exhausted",
    "account balance is insufficient",
    "账户欠费",
    "账号欠费",
    "余额不足",
    "额度不足",
    "配额不足",
    "token 不足",
    "token不足",
    "tokens 不足",
    "tokens不足",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report_now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _conversation_title(text: str) -> str:
    first = next(
        (line.strip() for line in str(text or "").splitlines() if line.strip()),
        "",
    )
    return (first or "未命名剪辑")[:40]


def _task_turn_prompt(task: Mapping[str, Any]) -> str:
    prompt = str(task.get("user_prompt") or "").strip()
    if prompt:
        return prompt
    params = task.get("params") if isinstance(task.get("params"), Mapping) else {}
    subject = str(task.get("video_subject") or params.get("video_subject") or "").strip()
    script = str(params.get("video_script") or "").strip()
    if subject and script and script != subject:
        return f"{subject}\n{script}"
    return subject or script or str(task.get("task_id") or "")


def _legacy_state(status: str) -> int:
    if status == const.TASK_STATUS_COMPLETED:
        return const.TASK_STATE_COMPLETE
    if status in {const.TASK_STATUS_FAILED, const.TASK_STATUS_CANCELLED}:
        return const.TASK_STATE_FAILED
    return const.TASK_STATE_PROCESSING


def _status_from_state(state: int) -> str:
    if state == const.TASK_STATE_COMPLETE:
        return const.TASK_STATUS_COMPLETED
    if state == const.TASK_STATE_FAILED:
        return const.TASK_STATUS_FAILED
    return const.TASK_STATUS_PROCESSING


class TaskStoreError(RuntimeError):
    pass


class TaskStore:
    def __init__(
        self, database_path: str | os.PathLike[str], max_queued_tasks: int = 100
    ):
        self.database_path = os.path.abspath(os.fspath(database_path))
        self.max_queued_tasks = max(1, int(max_queued_tasks))
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(
            self.database_path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    batch_id TEXT,
                    conversation_id TEXT,
                    request_id TEXT,
                    subject TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    state INTEGER NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(batch_id) REFERENCES batches(batch_id) ON DELETE SET NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status_created
                    ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_batch
                    ON tasks(batch_id, created_at);

                CREATE TABLE IF NOT EXISTS task_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    UNIQUE(task_id, attempt_number),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                """
            )
            row = connection.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(row["version"]) < SCHEMA_VERSION:
                connection.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )
            self._migrate_schema(connection)

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "conversation_id" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_conversation
                ON tasks(conversation_id, created_at)
            """
        )
        self.backfill_conversations(connection)

    def backfill_conversations(
        self, connection: sqlite3.Connection | None = None
    ) -> int:
        def _run(conn: sqlite3.Connection) -> int:
            orphans = conn.execute(
                """
                SELECT task_id, subject, created_at, updated_at, payload_json, params_json
                FROM tasks
                WHERE conversation_id IS NULL OR conversation_id = ''
                ORDER BY created_at ASC
                """
            ).fetchall()
            count = 0
            for row in orphans:
                payload = _json_load(row["payload_json"], {})
                params = _json_load(row["params_json"], {})
                title = _conversation_title(
                    str(
                        payload.get("user_prompt")
                        or row["subject"]
                        or params.get("video_subject")
                        or ""
                    )
                )
                conversation_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, title, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        title,
                        row["created_at"] or _utc_now(),
                        row["updated_at"] or _utc_now(),
                    ),
                )
                conn.execute(
                    "UPDATE tasks SET conversation_id = ? WHERE task_id = ?",
                    (conversation_id, row["task_id"]),
                )
                count += 1
            return count

        if connection is not None:
            return _run(connection)
        with self.connection() as conn:
            return _run(conn)

    def ensure_conversation(
        self,
        conversation_id: str | None = None,
        *,
        title: str = "",
    ) -> str:
        conversation_id = str(conversation_id or "").strip() or str(uuid4())
        now = _utc_now()
        label = _conversation_title(title)
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT title FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, title, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (conversation_id, label, now, now),
                )
            else:
                next_title = existing["title"] or label
                connection.execute(
                    """
                    UPDATE conversations
                    SET title = ?, updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (next_title, now, conversation_id),
                )
        return conversation_id

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.conversation_id, c.title, c.created_at,
                       COALESCE(MAX(t.updated_at), c.updated_at) AS updated_at,
                       COUNT(t.task_id) AS task_count
                FROM conversations AS c
                LEFT JOIN tasks AS t ON t.conversation_id = c.conversation_id
                GROUP BY c.conversation_id, c.title, c.created_at, c.updated_at
                HAVING COUNT(t.task_id) > 0
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return None
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT conversation_id, title, created_at, updated_at
                FROM conversations WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            tasks = connection.execute(
                """
                SELECT * FROM tasks
                WHERE conversation_id = ?
                ORDER BY created_at ASC
                """,
                (conversation_id,),
            ).fetchall()
        if row is None and not tasks:
            return None
        items = [self._row_to_task(item) for item in tasks]
        title = str(row["title"] if row else "") or (
            _conversation_title(_task_turn_prompt(items[0])) if items else "未命名剪辑"
        )
        return {
            "conversation_id": conversation_id,
            "title": title,
            "created_at": row["created_at"] if row else items[0]["created_at"],
            "updated_at": row["updated_at"] if row else items[-1]["updated_at"],
            "task_count": len(items),
            "turns": [
                {
                    "task_id": item["task_id"],
                    "prompt": _task_turn_prompt(item),
                    "status": item.get("status"),
                    "created_at": item.get("created_at"),
                }
                for item in items
            ],
        }

    def rename_conversation(
        self, conversation_id: str, title: str
    ) -> dict[str, Any] | None:
        conversation_id = str(conversation_id or "").strip()
        label = str(title or "").strip()
        if not conversation_id:
            return None
        if not label:
            raise TaskStoreError("title is required")
        label = label[:40]
        now = _utc_now()
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT conversation_id FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            task_count = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["count"]
            if existing is None and not task_count:
                return None
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, title, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (conversation_id, label, now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE conversations
                    SET title = ?, updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (label, now, conversation_id),
                )
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> list[str] | None:
        """Delete a conversation and its tasks. Caller removes task directories."""
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return None
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT conversation_id FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT task_id FROM tasks WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            if existing is None and not rows:
                return None
            task_ids = [str(row["task_id"]) for row in rows]
            for task_id in task_ids:
                connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM batches WHERE batch_id NOT IN (SELECT DISTINCT batch_id FROM tasks WHERE batch_id IS NOT NULL)"
            )
        return task_ids

    def rename_task(self, task_id: str, title: str) -> dict[str, Any] | None:
        label = str(title or "").strip()
        if not label:
            raise TaskStoreError("title is required")
        label = label[:200]
        now = _utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT params_json FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            params = _json_load(row["params_json"], {})
            if not isinstance(params, dict):
                params = {}
            params["video_subject"] = label
            connection.execute(
                """
                UPDATE tasks
                SET subject = ?, params_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (label, _json_dump(params), now, task_id),
            )
        return self.get_task(task_id)

    def create_batch(self, name: str = "") -> str:
        batch_id = str(uuid4())
        now = _utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO batches(batch_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (batch_id, name.strip() or f"批次 {now[:19]}", now, now),
            )
        return batch_id

    def list_batches(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT b.batch_id, b.name, b.created_at,
                       COALESCE(MAX(t.updated_at), b.updated_at) AS updated_at,
                       COUNT(t.task_id) AS task_count
                FROM batches AS b
                LEFT JOIN tasks AS t ON t.batch_id = b.batch_id
                GROUP BY b.batch_id, b.name, b.created_at, b.updated_at
                HAVING COUNT(t.task_id) > 0
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def enqueue(
        self,
        params: VideoParams | dict[str, Any],
        *,
        task_id: str | None = None,
        batch_id: str | None = None,
        conversation_id: str | None = None,
        request_id: str | None = None,
        stop_at: str = "video",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = task_id or str(uuid4())
        params_data = (
            params.model_dump(mode="json", warnings=False)
            if isinstance(params, VideoParams)
            else dict(params)
        )
        user_prompt = str(
            (payload or {}).get("user_prompt")
            or params_data.pop("user_prompt", "")
            or ""
        ).strip()
        conversation_id = str(
            conversation_id or params_data.pop("conversation_id", "") or ""
        ).strip()
        subject = str(
            params_data.get("video_subject") or params_data.get("video_script") or ""
        )
        conversation_id = self.ensure_conversation(
            conversation_id,
            title=user_prompt or subject,
        )
        now = _utc_now()
        payload = {
            "stop_at": stop_at,
            **dict(payload or {}),
            "conversation_id": conversation_id,
        }
        if user_prompt:
            payload["user_prompt"] = user_prompt

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                queued_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = ?",
                    (const.TASK_STATUS_QUEUED,),
                ).fetchone()[0]
                if queued_count >= self.max_queued_tasks:
                    raise TaskQueueFullError(
                        "task queue is full, please try again later"
                    )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, batch_id, conversation_id, request_id, subject, status, stage, state,
                        progress, params_json, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        batch_id = excluded.batch_id,
                        conversation_id = COALESCE(excluded.conversation_id, tasks.conversation_id),
                        request_id = excluded.request_id,
                        subject = excluded.subject,
                        status = excluded.status,
                        stage = excluded.stage,
                        state = excluded.state,
                        progress = excluded.progress,
                        params_json = excluded.params_json,
                        payload_json = excluded.payload_json,
                        cancel_requested = 0,
                        lease_owner = NULL,
                        lease_until = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        task_id,
                        batch_id,
                        conversation_id,
                        request_id,
                        subject,
                        const.TASK_STATUS_QUEUED,
                        const.TASK_STATE_PROCESSING,
                        _json_dump(params_data),
                        _json_dump(payload),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE conversations
                    SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END
                    WHERE conversation_id = ?
                    """,
                    (now, _conversation_title(user_prompt or subject), conversation_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_task(task_id) or {"task_id": task_id}

    def enqueue_batch(
        self,
        subjects: Iterable[str],
        params: VideoParams | dict[str, Any],
        *,
        batch_name: str = "",
        request_id: str | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        normalized = [
            str(subject).strip() for subject in subjects if str(subject).strip()
        ]
        if not normalized or len(normalized) > 100:
            raise ValueError("subjects must contain between 1 and 100 non-empty values")
        params_data = (
            params.model_dump(mode="json", warnings=False)
            if isinstance(params, VideoParams)
            else dict(params)
        )
        batch_id = self.create_batch(batch_name)
        conversation_id = self.ensure_conversation(
            title=batch_name or (normalized[0] if normalized else "")
        )
        tasks = []
        try:
            for subject in normalized:
                item = dict(params_data)
                item["video_subject"] = subject
                item["video_script"] = ""
                tasks.append(
                    self.enqueue(
                        item,
                        batch_id=batch_id,
                        conversation_id=conversation_id,
                        request_id=request_id,
                    )
                )
        except Exception:
            for task in tasks:
                self.delete_task(task["task_id"])
            with self.connection() as connection:
                connection.execute(
                    "DELETE FROM batches WHERE batch_id = ?", (batch_id,)
                )
            raise
        return batch_id, tasks

    def claim_next(
        self,
        worker_id: str,
        *,
        max_concurrent_tasks: int = 1,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        now_epoch = time.time()
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, stage = 'queued', state = ?, progress = 0,
                        lease_owner = NULL, lease_until = NULL, updated_at = ?
                    WHERE status IN (?, ?) AND lease_until IS NOT NULL AND lease_until < ?
                    """,
                    (
                        const.TASK_STATUS_QUEUED,
                        const.TASK_STATE_PROCESSING,
                        now,
                        const.TASK_STATUS_PROCESSING,
                        const.TASK_STATUS_CANCELLATION_REQUESTED,
                        now_epoch,
                    ),
                )
                active_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN (?, ?)",
                    (
                        const.TASK_STATUS_PROCESSING,
                        const.TASK_STATUS_CANCELLATION_REQUESTED,
                    ),
                ).fetchone()[0]
                if active_count >= max(1, int(max_concurrent_tasks)):
                    connection.execute("COMMIT")
                    return None
                row = connection.execute(
                    "SELECT task_id FROM tasks WHERE status = ? ORDER BY created_at LIMIT 1",
                    (const.TASK_STATUS_QUEUED,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                task_id = row["task_id"]
                attempt_count = (
                    connection.execute(
                        "SELECT attempt_count FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()[0]
                    + 1
                )
                connection.execute(
                    """
                    UPDATE tasks SET status = ?, stage = 'starting', state = ?,
                        attempt_count = ?, lease_owner = ?, lease_until = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        const.TASK_STATUS_PROCESSING,
                        const.TASK_STATE_PROCESSING,
                        attempt_count,
                        worker_id,
                        now_epoch + max(30, int(lease_seconds)),
                        now,
                        task_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO task_attempts(
                        task_id, attempt_number, started_at, status
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, attempt_count, now, const.TASK_STATUS_PROCESSING),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_task(task_id)

    def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET lease_until = ?, updated_at = ?
                WHERE task_id = ? AND lease_owner = ? AND status IN (?, ?)
                """,
                (
                    time.time() + max(30, int(lease_seconds)),
                    _utc_now(),
                    task_id,
                    worker_id,
                    const.TASK_STATUS_PROCESSING,
                    const.TASK_STATUS_CANCELLATION_REQUESTED,
                ),
            )
            return cursor.rowcount == 1

    def update_runtime_task(
        self,
        task_id: str,
        *,
        state: int = const.TASK_STATE_PROCESSING,
        progress: int = 0,
        **fields: Any,
    ) -> None:
        progress = max(0, min(100, int(progress)))
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, payload_json, attempt_count FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                current_status = row["status"] if row else ""
                payload = _json_load(row["payload_json"], {}) if row else {}
                payload.update(fields)
                # Payload-only patches (for example generation reports or billing
                # details) must not derive a new task status from the legacy numeric
                # state. Waiting tasks intentionally keep TASK_STATE_PROCESSING for
                # compatibility, so deriving from it would turn awaiting_material or
                # awaiting_approval back into a lease-less processing task.
                incoming_status = str(
                    fields.get("status")
                    or current_status
                    or _status_from_state(state)
                )
                if (
                    current_status == const.TASK_STATUS_CANCELLATION_REQUESTED
                    and incoming_status == const.TASK_STATUS_PROCESSING
                ):
                    incoming_status = current_status
                stage = str(
                    fields.get("stage") or payload.get("stage") or incoming_status
                )
                subject = str(
                    fields.get("video_subject") or payload.get("video_subject") or ""
                )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO tasks(
                            task_id, subject, status, stage, state, progress,
                            payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            subject,
                            incoming_status,
                            stage,
                            state,
                            progress,
                            _json_dump(payload),
                            now,
                            now,
                        ),
                    )
                    attempt_count = 0
                else:
                    connection.execute(
                        """
                        UPDATE tasks SET subject = CASE WHEN ? = '' THEN subject ELSE ? END,
                            status = ?, stage = ?, state = ?, progress = ?, payload_json = ?,
                            lease_owner = CASE WHEN ? NOT IN (?, ?) THEN NULL ELSE lease_owner END,
                            lease_until = CASE WHEN ? NOT IN (?, ?) THEN NULL ELSE lease_until END,
                            updated_at = ?
                        WHERE task_id = ?
                        """,
                        (
                            subject,
                            subject,
                            incoming_status,
                            stage,
                            state,
                            progress,
                            _json_dump(payload),
                            incoming_status,
                            const.TASK_STATUS_PROCESSING,
                            const.TASK_STATUS_CANCELLATION_REQUESTED,
                            incoming_status,
                            const.TASK_STATUS_PROCESSING,
                            const.TASK_STATUS_CANCELLATION_REQUESTED,
                            now,
                            task_id,
                        ),
                    )
                    attempt_count = row["attempt_count"]
                if (
                    incoming_status not in const.TASK_BUSY_STATUSES
                    and attempt_count
                ):
                    connection.execute(
                        """
                        UPDATE task_attempts SET finished_at = ?, status = ?, error = ?
                        WHERE task_id = ? AND attempt_number = ?
                            AND finished_at IS NULL
                        """,
                        (
                            now,
                            incoming_status,
                            str(fields.get("error") or ""),
                            task_id,
                            attempt_count,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def patch_task(self, task_id: str, **fields: Any) -> bool:
        if not fields:
            return False
        task = self.get_task(task_id)
        if not task:
            return False
        state = int(fields.pop("state", task.get("state", const.TASK_STATE_PROCESSING)))
        progress = int(fields.pop("progress", task.get("progress", 0)))
        self.update_runtime_task(task_id, state=state, progress=progress, **fields)
        return True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def get_attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT attempt_number, started_at, finished_at, status, error
                FROM task_attempts WHERE task_id = ? ORDER BY attempt_number DESC
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_tasks(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        statuses: Iterable[str] | None = None,
        batch_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        clauses = []
        values: list[Any] = []
        normalized_statuses = [str(status) for status in statuses or [] if status]
        if normalized_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in normalized_statuses)})")
            values.extend(normalized_statuses)
        if batch_id:
            clauses.append("batch_id = ?")
            values.append(batch_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM tasks{where}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM tasks{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*values, page_size, (page - 1) * page_size),
            ).fetchall()
        return [self._row_to_task(row) for row in rows], total

    def _row_to_task(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = _json_load(row["payload_json"], {})
        payload.update(
            {
                "task_id": row["task_id"],
                "batch_id": row["batch_id"],
                "conversation_id": row["conversation_id"]
                or payload.get("conversation_id")
                or "",
                "request_id": row["request_id"],
                "video_subject": row["subject"],
                "status": row["status"],
                "stage": row["stage"],
                "state": row["state"],
                "progress": row["progress"],
                "params": _json_load(row["params_json"], {}),
                "attempt_count": row["attempt_count"],
                "cancel_requested": bool(row["cancel_requested"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        return payload

    def request_cancel(self, task_id: str) -> str | None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, payload_json FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                status = row["status"]
                if status == const.TASK_STATUS_QUEUED:
                    next_status = const.TASK_STATUS_CANCELLED
                    state = const.TASK_STATE_FAILED
                    stage = "cancelled"
                elif status in {
                    const.TASK_STATUS_PROCESSING,
                    const.TASK_STATUS_CANCELLATION_REQUESTED,
                }:
                    next_status = const.TASK_STATUS_CANCELLATION_REQUESTED
                    state = const.TASK_STATE_PROCESSING
                    stage = "cancellation_requested"
                elif status in {
                    const.TASK_STATUS_AWAITING_APPROVAL,
                    const.TASK_STATUS_AWAITING_MATERIAL,
                }:
                    next_status = const.TASK_STATUS_CANCELLED
                    state = const.TASK_STATE_FAILED
                    stage = "cancelled"
                else:
                    connection.execute("COMMIT")
                    return status
                connection.execute(
                    """
                    UPDATE tasks SET status = ?, stage = ?, state = ?,
                        cancel_requested = 1, updated_at = ? WHERE task_id = ?
                    """,
                    (next_status, stage, state, now, task_id),
                )
                connection.execute("COMMIT")
                return next_status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def attach_scene_material(
        self,
        task_id: str,
        scene_index: int,
        file_path: str,
        *,
        original_name: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """把一个用户上传的视频原片绑定到指定分镜，返回任务和被替换的旧路径。"""
        task = self.get_task(task_id)
        if not task:
            return None, ""
        if not (
            task.get("status") == const.TASK_STATUS_AWAITING_MATERIAL
            and task.get("stage") == "awaiting_material_upload"
        ):
            raise TaskStoreError("task is not waiting for scene material uploads")
        valid_indexes = {
            int(scene.get("scene_index", position))
            for position, scene in enumerate(task.get("scene_plan") or [])
            if isinstance(scene, Mapping)
        }
        if int(scene_index) not in valid_indexes:
            raise TaskStoreError("scene does not exist in this task")
        required_indexes = {
            int(value)
            for value in task.get("required_upload_scene_indexes") or valid_indexes
            if str(value).isdigit()
        }
        if int(scene_index) not in required_indexes:
            raise TaskStoreError("scene does not require an uploaded material")
        scene_job = (task.get("scene_generation_jobs") or {}).get(str(scene_index)) or {}
        if scene_job.get("status") in {"queued", "submitted", "processing"}:
            raise TaskStoreError("scene is currently being generated by Seedance")
        normalized_path = os.path.abspath(str(file_path or ""))
        task_dir = os.path.abspath(utils.task_dir(task_id))
        try:
            belongs_to_task = (
                os.path.commonpath([task_dir, normalized_path]) == task_dir
            )
        except ValueError:
            belongs_to_task = False
        if not belongs_to_task or not os.path.isfile(normalized_path):
            raise TaskStoreError("scene material file is invalid")

        paths = dict(task.get("uploaded_scene_paths") or {})
        names = dict(task.get("uploaded_scene_original_names") or {})
        old_path = str(paths.get(str(scene_index)) or "")
        paths[str(scene_index)] = normalized_path
        names[str(scene_index)] = os.path.basename(str(original_name or ""))[:255]
        available_indexes = {
            int(index)
            for index, path_value in paths.items()
            if str(index).isdigit() and os.path.isfile(str(path_value))
        }
        for field in ("generated_scene_paths", "resolved_scene_paths"):
            available_indexes.update(
                int(index)
                for index, path_value in (task.get(field) or {}).items()
                if str(index).isdigit() and os.path.isfile(str(path_value))
            )
        missing = [
            scene
            for position, scene in enumerate(task.get("scene_plan") or [])
            if int(scene.get("scene_index", position)) in required_indexes
            and int(scene.get("scene_index", position)) not in available_indexes
        ]
        self.update_runtime_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=int(task.get("progress", 45)),
            status=const.TASK_STATUS_AWAITING_MATERIAL,
            stage="awaiting_material_upload",
            uploaded_scene_paths=paths,
            uploaded_scene_original_names=names,
            missing_scenes=missing,
            material_upload_confirmed=False,
        )
        return self.get_task(task_id), old_path

    def reserve_scene_generation(
        self, task_id: str, scene_index: int
    ) -> dict[str, Any]:
        """原子预占一个缺失分镜的 Seedance 生成操作，阻止重复付费提交。"""
        scene_index = int(scene_index)
        scene_key = str(scene_index)
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, stage, payload_json FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise TaskStoreError("task does not exist")
                if not (
                    row["status"] == const.TASK_STATUS_AWAITING_MATERIAL
                    and row["stage"] == "awaiting_material_upload"
                ):
                    raise TaskStoreError("task is not waiting for scene materials")
                payload = _json_load(row["payload_json"], {})
                scenes = [
                    dict(scene)
                    for scene in payload.get("scene_plan") or []
                    if isinstance(scene, Mapping)
                ]
                valid_indexes = {
                    int(scene.get("scene_index", position))
                    for position, scene in enumerate(scenes)
                }
                if scene_index not in valid_indexes:
                    raise TaskStoreError("scene does not exist in this task")
                configured_required = payload.get("required_upload_scene_indexes")
                required_indexes = (
                    valid_indexes
                    if configured_required is None
                    else {
                        int(value)
                        for value in configured_required
                        if str(value).isdigit()
                    }
                )
                if scene_index not in required_indexes:
                    raise TaskStoreError("scene does not require generated material")
                for field in (
                    "uploaded_scene_paths",
                    "generated_scene_paths",
                    "resolved_scene_paths",
                ):
                    path_value = (payload.get(field) or {}).get(scene_key)
                    if path_value and os.path.isfile(str(path_value)):
                        raise TaskStoreError("scene already has usable material")

                jobs = dict(payload.get("scene_generation_jobs") or {})
                previous = dict(jobs.get(scene_key) or {})
                if previous.get("status") in {"queued", "submitted", "processing"}:
                    raise TaskStoreError("scene generation is already in progress")
                provider_task_id = str(
                    previous.get("provider_task_id")
                    or (payload.get("seedance_provider_task_ids") or {}).get(scene_key)
                    or ""
                )
                run_id = uuid4().hex
                jobs[scene_key] = {
                    "run_id": run_id,
                    "status": "queued",
                    "provider_task_id": provider_task_id or None,
                    "requested_at": now,
                    "updated_at": now,
                    "error": None,
                }
                payload["scene_generation_jobs"] = jobs
                connection.execute(
                    """
                    UPDATE tasks SET payload_json = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (_json_dump(payload), now, task_id),
                )
                connection.execute("COMMIT")
                return dict(jobs[scene_key])
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def update_scene_generation_job(
        self,
        task_id: str,
        scene_index: int,
        run_id: str,
        **fields: Any,
    ) -> bool:
        """仅当运行标识匹配时更新单分镜生成状态。"""
        scene_key = str(int(scene_index))
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload_json FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return False
                payload = _json_load(row["payload_json"], {})
                jobs = dict(payload.get("scene_generation_jobs") or {})
                job = dict(jobs.get(scene_key) or {})
                if str(job.get("run_id") or "") != str(run_id or ""):
                    connection.execute("COMMIT")
                    return False
                job.update(fields)
                job["updated_at"] = now
                jobs[scene_key] = job
                payload["scene_generation_jobs"] = jobs
                provider_task_id = str(fields.get("provider_task_id") or "")
                if provider_task_id:
                    provider_ids = dict(payload.get("seedance_provider_task_ids") or {})
                    provider_ids[scene_key] = provider_task_id
                    payload["seedance_provider_task_ids"] = provider_ids
                connection.execute(
                    "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
                    (_json_dump(payload), now, task_id),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def complete_scene_generation(
        self,
        task_id: str,
        scene_index: int,
        run_id: str,
        file_path: str,
        *,
        provider_task_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        """保存单分镜生成结果，同时让任务继续停留在待处理状态。"""
        scene_index = int(scene_index)
        scene_key = str(scene_index)
        normalized_path = os.path.abspath(str(file_path or ""))
        if not os.path.isfile(normalized_path):
            raise TaskStoreError("generated scene material does not exist")
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload_json FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return False
                payload = _json_load(row["payload_json"], {})
                jobs = dict(payload.get("scene_generation_jobs") or {})
                job = dict(jobs.get(scene_key) or {})
                if str(job.get("run_id") or "") != str(run_id or ""):
                    connection.execute("COMMIT")
                    return False
                generated_paths = dict(payload.get("generated_scene_paths") or {})
                generated_paths[scene_key] = normalized_path
                payload["generated_scene_paths"] = generated_paths
                provider_ids = dict(payload.get("seedance_provider_task_ids") or {})
                provider_ids[scene_key] = str(provider_task_id or "")
                payload["seedance_provider_task_ids"] = provider_ids
                available_indexes = set()
                for field in (
                    "uploaded_scene_paths",
                    "generated_scene_paths",
                    "resolved_scene_paths",
                ):
                    available_indexes.update(
                        int(index)
                        for index, path_value in (payload.get(field) or {}).items()
                        if str(index).isdigit() and os.path.isfile(str(path_value))
                    )
                scenes = [
                    dict(scene)
                    for scene in payload.get("scene_plan") or []
                    if isinstance(scene, Mapping)
                ]
                payload["missing_scenes"] = [
                    scene
                    for position, scene in enumerate(scenes)
                    if int(scene.get("scene_index", position)) not in available_indexes
                ]
                payload["seedance_errors"] = [
                    error
                    for error in payload.get("seedance_errors") or []
                    if not isinstance(error, Mapping)
                    or int(error.get("scene_index", -1)) != scene_index
                ]
                job.update(
                    status="completed",
                    provider_task_id=str(provider_task_id or "") or None,
                    result_path=normalized_path,
                    details=dict(details or {}),
                    error=None,
                    updated_at=now,
                )
                jobs[scene_key] = job
                payload["scene_generation_jobs"] = jobs
                connection.execute(
                    "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
                    (_json_dump(payload), now, task_id),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def fail_scene_generation(
        self,
        task_id: str,
        scene_index: int,
        run_id: str,
        error: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        """记录单分镜失败，但不把整个作品任务改成失败。"""
        scene_index = int(scene_index)
        scene_key = str(scene_index)
        details = dict(details or {})
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload_json FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return False
                payload = _json_load(row["payload_json"], {})
                jobs = dict(payload.get("scene_generation_jobs") or {})
                job = dict(jobs.get(scene_key) or {})
                if str(job.get("run_id") or "") != str(run_id or ""):
                    connection.execute("COMMIT")
                    return False
                provider_task_id = str(
                    details.get("provider_task_id")
                    or job.get("provider_task_id")
                    or ""
                )
                provider_status = str(details.get("provider_status") or "").lower()
                if provider_task_id and provider_status in {
                    "failed",
                    "error",
                    "cancelled",
                    "canceled",
                }:
                    provider_ids = dict(payload.get("seedance_provider_task_ids") or {})
                    provider_ids.pop(scene_key, None)
                    payload["seedance_provider_task_ids"] = provider_ids
                    failed_ids = dict(
                        payload.get("seedance_failed_provider_task_ids") or {}
                    )
                    history = list(failed_ids.get(scene_key) or [])
                    if provider_task_id not in history:
                        history.append(provider_task_id)
                    failed_ids[scene_key] = history
                    payload["seedance_failed_provider_task_ids"] = failed_ids
                    provider_task_id = ""
                errors = [
                    item
                    for item in payload.get("seedance_errors") or []
                    if not isinstance(item, Mapping)
                    or int(item.get("scene_index", -1)) != scene_index
                ]
                errors.append({"scene_index": scene_index, "error": str(error)[:1000]})
                payload["seedance_errors"] = errors
                job.update(
                    status="failed",
                    provider_task_id=provider_task_id or None,
                    details=details,
                    error=str(error)[:1000],
                    updated_at=now,
                )
                jobs[scene_key] = job
                payload["scene_generation_jobs"] = jobs
                connection.execute(
                    "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE task_id = ?",
                    (_json_dump(payload), now, task_id),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def is_seedance_billing_failure(task: Mapping[str, Any]) -> bool:
        """判断失败是否明确来自 Seedance 欠费、余额或 Token 配额不足。"""
        seedance_errors = task.get("seedance_errors") or []
        error_messages = [str(task.get("error") or "")]
        for item in seedance_errors:
            if isinstance(item, Mapping):
                error_messages.append(str(item.get("error") or ""))
            else:
                error_messages.append(str(item or ""))
        combined = "\n".join(error_messages).lower()
        has_seedance_context = bool(seedance_errors) or "seedance" in combined
        return has_seedance_context and any(
            marker in combined for marker in _SEEDANCE_BILLING_FAILURE_MARKERS
        )

    @staticmethod
    def _existing_scene_paths(
        task: Mapping[str, Any], valid_indexes: set[int]
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        def existing_paths(field: str) -> dict[str, str]:
            return {
                str(index): os.path.abspath(str(path_value))
                for index, path_value in (task.get(field) or {}).items()
                if str(index).isdigit()
                and int(index) in valid_indexes
                and os.path.isfile(str(path_value))
            }

        return (
            existing_paths("uploaded_scene_paths"),
            existing_paths("generated_scene_paths"),
            existing_paths("resolved_scene_paths"),
        )

    def resume_scene_material_uploads(self, task_id: str) -> dict[str, Any] | None:
        """把保留完整分镜的旧失败任务恢复为等待上传状态。"""
        task = self.get_task(task_id)
        if not task:
            return None
        if task.get("status") != const.TASK_STATUS_FAILED:
            raise TaskStoreError("only failed tasks can resume scene material uploads")
        failed_stage = str(task.get("failed_stage") or task.get("stage") or "")
        if failed_stage != "materials" and not self.is_seedance_billing_failure(task):
            raise TaskStoreError("task did not fail while preparing scene materials")
        scenes = [
            dict(scene)
            for scene in task.get("scene_plan") or []
            if isinstance(scene, Mapping)
        ]
        valid_indexes = {
            int(scene.get("scene_index", position))
            for position, scene in enumerate(scenes)
        }
        if not valid_indexes:
            raise TaskStoreError("task has no scene plan to resume")
        uploaded_paths, generated_paths, resolved_paths = self._existing_scene_paths(
            task, valid_indexes
        )
        reusable_indexes = {
            int(index)
            for index in set(uploaded_paths) | set(generated_paths) | set(resolved_paths)
        }
        required_indexes = valid_indexes - reusable_indexes
        if not required_indexes:
            raise TaskStoreError("task has no missing scene materials to upload")
        missing = [
            scene
            for position, scene in enumerate(scenes)
            if int(scene.get("scene_index", position)) in required_indexes
            and str(scene.get("scene_index", position)) not in uploaded_paths
        ]
        self.update_runtime_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=max(45, int(task.get("progress", 0) or 0)),
            status=const.TASK_STATUS_AWAITING_MATERIAL,
            stage="awaiting_material_upload",
            uploaded_scene_paths=uploaded_paths,
            generated_scene_paths=generated_paths,
            resolved_scene_paths=resolved_paths,
            required_upload_scene_indexes=sorted(required_indexes),
            missing_scenes=missing,
            material_upload_confirmed=False,
            material_recovery_error=str(task.get("error") or ""),
            material_recovery_reason=(
                "seedance_billing_failure"
                if self.is_seedance_billing_failure(task)
                else "material_generation_failure"
            ),
            error=None,
            failed_stage=None,
        )
        return self.get_task(task_id)

    def recover_seedance_billing_failures(self) -> list[str]:
        """自动把历史 Seedance 欠费/额度失败任务恢复到人工补素材阶段。"""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at",
                (const.TASK_STATUS_FAILED,),
            ).fetchall()
        recovered = []
        for row in rows:
            task = self._row_to_task(row)
            if not task.get("scene_plan") or not self.is_seedance_billing_failure(task):
                continue
            try:
                resumed = self.resume_scene_material_uploads(task["task_id"])
            except TaskStoreError as exc:
                logger.warning(
                    "failed to recover Seedance billing task, "
                    f"task_id={task['task_id']}, error={exc}"
                )
                continue
            if resumed:
                recovered.append(task["task_id"])
        return recovered

    @staticmethod
    def _is_insufficient_visual_duration_failure(task: Mapping[str, Any]) -> bool:
        return "InsufficientVisualDurationError" in str(task.get("error") or "")

    @staticmethod
    def _recovery_report(
        task: Mapping[str, Any], original_error: str, details: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        report = task.get("generation_report")
        if not isinstance(report, Mapping):
            return None
        recovered_report = dict(report)
        task_details = dict(recovered_report.get("task_details") or {})
        task_details.update(
            {
                "material_recovery_reason": "insufficient_visual_duration",
                "material_recovery_error": original_error,
                **dict(details),
            }
        )
        recovered_report["task_details"] = task_details
        recovered_report["status"] = "awaiting_material"
        recovered_report["updated_at"] = _report_now()
        return recovered_report

    @staticmethod
    def _write_recovery_report_file(
        task_id: str, report: Mapping[str, Any]
    ) -> None:
        report_path = Path(utils.task_dir(task_id)) / "generation-report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = report_path.with_suffix(".json.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, indent=2, default=str)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, report_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def recover_insufficient_visual_duration_failures(self) -> list[str]:
        """Recover only explicit visual-duration failures without paid generation."""
        from app.services import material_pipeline, video

        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at",
                (const.TASK_STATUS_FAILED,),
            ).fetchall()
        recovered: list[str] = []
        for row in rows:
            task = self._row_to_task(row)
            if not self._is_insufficient_visual_duration_failure(task):
                continue
            scenes = [
                dict(scene)
                for scene in task.get("scene_plan") or []
                if isinstance(scene, Mapping)
            ]
            if not scenes:
                logger.warning(
                    "failed to recover visual-duration task without scene plan, "
                    f"task_id={task['task_id']}"
                )
                continue
            try:
                params = VideoParams.model_validate(task.get("params") or {})
                director_plan = task.get("director_plan") or {}
                clip_duration = director_plan.get("video_clip_duration")
                if clip_duration:
                    params.video_clip_duration = max(1, int(clip_duration))

                audio_file = str(task.get("audio_file") or "").strip()
                if not audio_file:
                    candidate = os.path.join(utils.task_dir(task["task_id"]), "audio.mp3")
                    audio_file = candidate if os.path.isfile(candidate) else ""
                audio_duration = 0.0
                if audio_file and os.path.isfile(audio_file):
                    audio_duration = float(video.get_audio_duration(audio_file))
                if audio_duration <= 0:
                    audio_duration = float(task.get("audio_duration") or 0)
                if audio_duration <= 0:
                    raise TaskStoreError("task has no reusable narration duration")
                required_visual_duration = video.get_required_video_duration(audio_duration)

                valid_indexes = {
                    int(scene.get("scene_index", position))
                    for position, scene in enumerate(scenes)
                }
                uploaded, generated, resolved = self._existing_scene_paths(
                    task, valid_indexes
                )
                available_paths: dict[str, str] = {}
                available_paths.update(generated)
                available_paths.update(resolved)
                available_paths.update(uploaded)
                ordered_indexes = sorted(int(index) for index in available_paths)
                ordered_paths = [available_paths[str(index)] for index in ordered_indexes]
                visual_duration = 0.0
                effective_durations: list[float] = []
                if ordered_paths:
                    selected_durations = task.get("selected_material_durations")
                    clip_durations = None
                    if isinstance(selected_durations, list):
                        clip_durations = [
                            selected_durations[index]
                            for index in ordered_indexes
                            if index < len(selected_durations)
                        ]
                        if len(clip_durations) != len(ordered_paths):
                            clip_durations = None
                    visual_duration, effective_durations = (
                        video.get_effective_visual_duration(
                            ordered_paths,
                            max_clip_duration=params.video_clip_duration,
                            clip_speed=params.video_clip_speed,
                            clip_durations=clip_durations,
                        )
                    )
                if visual_duration <= 0:
                    visual_duration = float(task.get("visual_duration") or 0)
                if visual_duration <= 0:
                    saved_effective = task.get("effective_material_durations") or []
                    visual_duration = sum(float(value or 0) for value in saved_effective)
                    effective_durations = [float(value or 0) for value in saved_effective]
                if visual_duration <= 0:
                    original_scenes = [
                        scene for scene in scenes if not scene.get("duration_shortfall_scene")
                    ]
                    visual_duration = len(original_scenes) * float(
                        params.video_clip_duration or 1
                    )

                shortfall_update = (
                    material_pipeline.build_visual_duration_shortfall_scenes(
                        params,
                        str(
                            params.video_script
                            or task.get("video_script")
                            or task.get("video_subject")
                            or "补充画面"
                        ),
                        scene_plan=scenes,
                        visual_duration=visual_duration,
                        required_visual_duration=required_visual_duration,
                    )
                )
                reusable_indexes = {int(index) for index in available_paths}
                required_indexes = [
                    index
                    for index in shortfall_update["required_upload_scene_indexes"]
                    if index not in reusable_indexes
                ]
                if not required_indexes:
                    raise TaskStoreError("task has no missing supplemental scenes")
                missing = [
                    scene
                    for scene in shortfall_update["supplemental_scenes"]
                    if int(scene["scene_index"]) in required_indexes
                ]
                original_error = str(task.get("error") or "")
                report_details = {
                    "visual_duration": round(visual_duration, 3),
                    "required_visual_duration": round(required_visual_duration, 3),
                    "shortfall_duration": shortfall_update["shortfall_duration"],
                    "supplemental_scene_count": shortfall_update[
                        "supplemental_scene_count"
                    ],
                    "required_upload_scene_indexes": required_indexes,
                }
                recovered_report = self._recovery_report(
                    task, original_error, report_details
                )
                update_fields: dict[str, Any] = {
                    "status": const.TASK_STATUS_AWAITING_MATERIAL,
                    "stage": "awaiting_material_upload",
                    "scene_plan": shortfall_update["scene_plan"],
                    "planned_scene_count": len(shortfall_update["scene_plan"]),
                    "visual_duration": round(visual_duration, 3),
                    "required_visual_duration": round(required_visual_duration, 3),
                    "shortfall_duration": shortfall_update["shortfall_duration"],
                    "supplemental_scene_count": shortfall_update[
                        "supplemental_scene_count"
                    ],
                    "effective_material_durations": effective_durations,
                    "uploaded_scene_paths": uploaded,
                    "generated_scene_paths": generated,
                    "resolved_scene_paths": resolved,
                    "required_upload_scene_indexes": required_indexes,
                    "missing_scenes": missing,
                    "material_upload_confirmed": False,
                    "material_recovery_error": original_error,
                    "material_recovery_reason": "insufficient_visual_duration",
                    "error": None,
                    "failed_stage": None,
                }
                if recovered_report is not None:
                    update_fields["generation_report"] = recovered_report
                self.update_runtime_task(
                    task["task_id"],
                    state=const.TASK_STATE_PROCESSING,
                    progress=max(45, int(task.get("progress", 0) or 0)),
                    **update_fields,
                )
                if recovered_report is not None:
                    try:
                        self._write_recovery_report_file(
                            task["task_id"], recovered_report
                        )
                    except OSError as exc:
                        logger.warning(
                            "failed to write recovered generation report, "
                            f"task_id={task['task_id']}, error={exc}"
                        )
                recovered.append(task["task_id"])
            except (OSError, TypeError, ValueError, TaskStoreError) as exc:
                logger.warning(
                    "failed to recover visual-duration task, "
                    f"task_id={task['task_id']}, error={exc}"
                )
        return recovered

    def confirm_scene_materials(self, task_id: str) -> dict[str, Any] | None:
        """校验所有分镜上传完成后，才允许任务继续进入合成流水线。"""
        task = self.get_task(task_id)
        if not task:
            return None
        if not (
            task.get("status") == const.TASK_STATUS_AWAITING_MATERIAL
            and task.get("stage") == "awaiting_material_upload"
        ):
            raise TaskStoreError("task is not waiting for scene material uploads")
        valid_indexes = {
            int(scene.get("scene_index", position))
            for position, scene in enumerate(task.get("scene_plan") or [])
            if isinstance(scene, Mapping)
        }
        configured_required = task.get("required_upload_scene_indexes")
        required_indexes = (
            valid_indexes
            if not configured_required
            else {
                int(value)
                for value in configured_required
                if str(value).isdigit() and int(value) in valid_indexes
            }
        )
        available_paths = {}
        for field in (
            "generated_scene_paths",
            "resolved_scene_paths",
            "uploaded_scene_paths",
        ):
            available_paths.update(
                {
                    str(index): str(path_value)
                    for index, path_value in (task.get(field) or {}).items()
                    if str(index).isdigit() and os.path.isfile(str(path_value))
                }
            )
        missing_indexes = [
            index
            for index in sorted(required_indexes)
            if str(index) not in available_paths
        ]
        if missing_indexes:
            display_indexes = ", ".join(str(index + 1) for index in missing_indexes)
            raise TaskStoreError(f"scene materials are incomplete: {display_indexes}")
        self.update_runtime_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=int(task.get("progress", 45)),
            status=const.TASK_STATUS_QUEUED,
            stage="queued",
            material_upload_confirmed=True,
            missing_scenes=[],
        )
        return self.get_task(task_id)

    def approve_supplemental_scenes(
        self,
        task_id: str,
        prompts: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if not task:
            return None
        if not (
            task.get("status") == const.TASK_STATUS_AWAITING_APPROVAL
            and task.get("stage") == "awaiting_supplemental_scenes"
            and task.get("supplemental_scenes")
        ):
            raise TaskStoreError("task is not waiting for supplemental scenes")
        edited_prompts = {
            str(key): str(value).strip()[:4000]
            for key, value in (prompts or {}).items()
            if str(value).strip()
        }
        supplemental = []
        for scene in task.get("supplemental_scenes") or []:
            item = dict(scene)
            scene_index = str(item.get("scene_index"))
            if scene_index in edited_prompts:
                item["video_prompt"] = edited_prompts[scene_index]
            supplemental.append(item)
        self.update_runtime_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=int(task.get("progress", 45)),
            status=const.TASK_STATUS_QUEUED,
            stage="queued",
            supplemental_scenes=supplemental,
            supplemental_scenes_approved=True,
        )
        return self.get_task(task_id)

    def reject_seedance(self, task_id: str) -> dict[str, Any] | None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, payload_json FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                if row["status"] != const.TASK_STATUS_AWAITING_APPROVAL:
                    raise TaskStoreError("task is not waiting for Seedance approval")
                payload = _json_load(row["payload_json"], {})
                payload.update(seedance_approved=False)
                connection.execute(
                    """
                    UPDATE tasks SET status = ?, stage = 'awaiting_material', state = ?,
                        payload_json = ?, lease_owner = NULL, lease_until = NULL,
                        updated_at = ? WHERE task_id = ?
                    """,
                    (
                        const.TASK_STATUS_AWAITING_MATERIAL,
                        const.TASK_STATE_PROCESSING,
                        _json_dump(payload),
                        now,
                        task_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_task(task_id)

    def add_replacement_segments(
        self,
        task_id: str,
        replacements: dict[str, str],
    ) -> dict[str, Any] | None:
        now = _utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, payload_json FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                if row["status"] != const.TASK_STATUS_AWAITING_MATERIAL:
                    raise TaskStoreError(
                        "task is not waiting for replacement materials"
                    )
                payload = _json_load(row["payload_json"], {})
                merged = dict(payload.get("replacement_segment_ids") or {})
                merged.update(
                    {
                        str(key): str(value)
                        for key, value in replacements.items()
                        if str(key).isdigit() and str(value).strip()
                    }
                )
                payload.update(
                    replacement_segment_ids=merged,
                    seedance_approved=False,
                )
                connection.execute(
                    """
                    UPDATE tasks SET status = ?, stage = 'queued', state = ?,
                        payload_json = ?, cancel_requested = 0,
                        lease_owner = NULL, lease_until = NULL, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        const.TASK_STATUS_QUEUED,
                        const.TASK_STATE_PROCESSING,
                        _json_dump(payload),
                        now,
                        task_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_task(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def mark_cancelled(self, task_id: str) -> None:
        self.update_runtime_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=0,
            status=const.TASK_STATUS_CANCELLED,
            stage="cancelled",
            error="task cancelled by user",
        )

    def retry_task(
        self,
        task_id: str,
        *,
        params_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        preserve_task_files = False
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT status, params_json, payload_json
                    FROM tasks WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                if row["status"] not in {
                    const.TASK_STATUS_FAILED,
                    const.TASK_STATUS_CANCELLED,
                    const.TASK_STATUS_AWAITING_MATERIAL,
                }:
                    raise TaskStoreError(
                        "only failed, cancelled or blocked tasks can be retried"
                    )
                old_payload = _json_load(row["payload_json"], {})
                preserve_task_files = bool(
                    old_payload.get("seedance_provider_task_ids")
                    or old_payload.get("generated_scene_paths")
                    or old_payload.get("uploaded_scene_paths")
                    or old_payload.get("resolved_scene_paths")
                )
                if preserve_task_files:
                    retry_payload = dict(old_payload)
                    for field in (
                        "error",
                        "failed_stage",
                        "videos",
                        "original_videos",
                        "combined_videos",
                        "warnings",
                        "cross_post_state",
                        "cross_post_results",
                        "cross_post_error",
                        "cross_post_owner",
                    ):
                        retry_payload.pop(field, None)
                    retry_payload["seedance_errors"] = []
                else:
                    retry_payload = {"stop_at": old_payload.get("stop_at", "video")}
                retry_params = _json_load(row["params_json"], {})
                if not isinstance(retry_params, dict):
                    retry_params = {}
                if params_overrides:
                    retry_params.update(dict(params_overrides))
                connection.execute(
                    """
                    UPDATE tasks SET status = ?, stage = 'queued', state = ?, progress = 0,
                        params_json = ?, payload_json = ?, cancel_requested = 0,
                        lease_owner = NULL, lease_until = NULL, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        const.TASK_STATUS_QUEUED,
                        const.TASK_STATE_PROCESSING,
                        _json_dump(retry_params),
                        _json_dump(retry_payload),
                        now,
                        task_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        task_path = utils.task_dir(task_id)
        if not preserve_task_files and os.path.isdir(task_path):
            shutil.rmtree(task_path, ignore_errors=True)
        return self.get_task(task_id)

    def delete_task(self, task_id: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM tasks WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "DELETE FROM batches WHERE batch_id NOT IN (SELECT DISTINCT batch_id FROM tasks WHERE batch_id IS NOT NULL)"
            )
            connection.execute(
                """
                DELETE FROM conversations
                WHERE conversation_id NOT IN (
                    SELECT DISTINCT conversation_id FROM tasks
                    WHERE conversation_id IS NOT NULL AND conversation_id != ''
                )
                """
            )
        return cursor.rowcount == 1


_default_store: TaskStore | None = None
_default_store_lock = threading.Lock()


def get_task_store() -> TaskStore:
    global _default_store
    if _default_store is None:
        with _default_store_lock:
            if _default_store is None:
                configured_path = str(
                    config.app.get("task_database_path") or ""
                ).strip()
                database_path = configured_path or utils.storage_dir("app.db")
                _default_store = TaskStore(
                    database_path,
                    max_queued_tasks=int(config.app.get("max_queued_tasks", 100)),
                )
                duration_recovered = (
                    _default_store.recover_insufficient_visual_duration_failures()
                )
                if duration_recovered:
                    logger.info(
                        "recovered historical visual-duration failures for manual "
                        f"scene uploads: {', '.join(duration_recovered)}"
                    )
                recovered = _default_store.recover_seedance_billing_failures()
                if recovered:
                    logger.info(
                        "recovered historical Seedance billing failures for manual "
                        f"scene uploads: {', '.join(recovered)}"
                    )
    return _default_store


class TaskWorkerPool:
    def __init__(self, store: TaskStore | None = None):
        self.store = store or get_task_store()
        self.max_workers = max(1, int(config.app.get("max_concurrent_tasks", 1)))
        self.lease_seconds = max(
            30, int(config.app.get("task_lease_seconds", DEFAULT_LEASE_SECONDS))
        )
        self.worker_id = f"{os.getpid()}-{uuid4().hex}"
        self._stop_event = threading.Event()
        self._threads: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()

    def _start_worker_locked(self, index: int) -> None:
        current = self._threads.get(index)
        if current is not None and current.is_alive():
            return
        thread = threading.Thread(
            target=self._run,
            args=(index,),
            name=f"mpt-task-worker-{index + 1}",
            daemon=True,
        )
        thread.start()
        self._threads[index] = thread

    def start(self) -> None:
        with self._lock:
            self._stop_event.clear()
            for index in range(self.max_workers):
                self._start_worker_locked(index)

    def reconfigure(self, *, max_workers: int, lease_seconds: int) -> None:
        """Resize workers without interrupting tasks already running."""
        desired_workers = max(1, int(max_workers))
        desired_lease = max(30, int(lease_seconds))
        with self._lock:
            self.max_workers = desired_workers
            self.lease_seconds = desired_lease
            self._stop_event.clear()
            for index in range(desired_workers):
                self._start_worker_locked(index)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=timeout)

    def _run(self, worker_index: int) -> None:
        from app.services import task as task_service

        while not self._stop_event.is_set():
            # Decreasing the configured pool size is graceful: excess workers
            # finish their current task, then leave before claiming another.
            if worker_index >= self.max_workers:
                return
            claimed = self.store.claim_next(
                self.worker_id,
                max_concurrent_tasks=self.max_workers,
                lease_seconds=self.lease_seconds,
            )
            if not claimed:
                self._stop_event.wait(0.5)
                continue
            task_id = claimed["task_id"]
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat,
                args=(task_id, heartbeat_stop),
                daemon=True,
            )
            heartbeat.start()
            try:
                params_data = dict(claimed.get("params") or {})
                # Tasks persisted before AI director fields existed must retain
                # their original manual-parameter behavior when retried.
                if "ai_director_enabled" not in params_data:
                    params_data["ai_director_enabled"] = False
                if claimed.get("script"):
                    params_data["video_script"] = claimed["script"]
                if claimed.get("terms"):
                    params_data["video_terms"] = claimed["terms"]
                params = VideoParams.model_validate(params_data)
                task_service.start(
                    task_id=task_id,
                    params=params,
                    stop_at=str(claimed.get("stop_at") or "video"),
                )
            except Exception as exc:
                logger.exception(
                    f"background task failed: task_id={task_id}, error={exc}"
                )
                self.store.update_runtime_task(
                    task_id,
                    state=const.TASK_STATE_FAILED,
                    status=const.TASK_STATUS_FAILED,
                    stage="unexpected",
                    error=str(exc),
                )
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=1)

    def _heartbeat(self, task_id: str, stop_event: threading.Event) -> None:
        interval = max(10, self.lease_seconds // 3)
        while not stop_event.wait(interval):
            if not self.store.renew_lease(task_id, self.worker_id, self.lease_seconds):
                return


_worker_pool: TaskWorkerPool | None = None
_worker_lock = threading.Lock()


def ensure_task_workers_started() -> TaskWorkerPool:
    global _worker_pool
    if _worker_pool is None:
        with _worker_lock:
            if _worker_pool is None:
                _worker_pool = TaskWorkerPool()
    _worker_pool.reconfigure(
        max_workers=max(1, int(config.app.get("max_concurrent_tasks", 1))),
        lease_seconds=max(
            30, int(config.app.get("task_lease_seconds", DEFAULT_LEASE_SECONDS))
        ),
    )
    return _worker_pool


def stop_task_workers() -> None:
    if _worker_pool is not None:
        _worker_pool.stop()
