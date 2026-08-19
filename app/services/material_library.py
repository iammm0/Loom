from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable
from uuid import uuid4

from loguru import logger
from PIL import Image

from app.config import config
from app.services import vision
from app.services.task_store import TaskStore, get_task_store
from app.utils import file_security, utils


MATERIAL_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
MATERIAL_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".flv", ".mkv", ".webm"}
MATERIAL_EXTENSIONS = MATERIAL_IMAGE_EXTENSIONS | MATERIAL_VIDEO_EXTENSIONS
MATERIAL_STATUS_PROCESSING = "processing"
MATERIAL_STATUS_REVIEW = "review"
MATERIAL_STATUS_READY = "ready"
MATERIAL_STATUS_FAILED = "failed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def normalize_tags(values: str | Iterable[str] | None) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,，、;；\n]", values)
    result = []
    seen = set()
    for raw in values or []:
        tag = re.sub(r"\s+", " ", str(raw or "").strip()).strip("#")
        key = tag.casefold()
        if not tag or len(tag) > 60 or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= 30:
            break
    return result


class MaterialLibraryError(RuntimeError):
    pass


class MaterialLibrary:
    def __init__(self, store: TaskStore | None = None):
        self.store = store or get_task_store()
        self._initialize()

    def _initialize(self) -> None:
        with self.store.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS materials (
                    material_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    description_zh TEXT NOT NULL DEFAULT '',
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    duration REAL NOT NULL DEFAULT 0,
                    ai_tagging INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_materials_status_created
                    ON materials(status, created_at);

                CREATE TABLE IF NOT EXISTS material_segments (
                    segment_id TEXT PRIMARY KEY,
                    material_id TEXT NOT NULL,
                    segment_index INTEGER NOT NULL,
                    start_time REAL NOT NULL DEFAULT 0,
                    end_time REAL NOT NULL DEFAULT 0,
                    file_path TEXT NOT NULL,
                    thumbnail_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    description_zh TEXT NOT NULL DEFAULT '',
                    manual_tags_json TEXT NOT NULL DEFAULT '[]',
                    ai_tags_zh_json TEXT NOT NULL DEFAULT '[]',
                    tags_en_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(material_id, segment_index),
                    FOREIGN KEY(material_id) REFERENCES materials(material_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_segments_material
                    ON material_segments(material_id, segment_index);
                CREATE INDEX IF NOT EXISTS idx_segments_status
                    ON material_segments(status);

                CREATE TABLE IF NOT EXISTS material_task_scenes (
                    task_id TEXT NOT NULL,
                    scene_index INTEGER NOT NULL,
                    material_id TEXT NOT NULL,
                    segment_id TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    scene_text TEXT NOT NULL DEFAULT '',
                    search_query TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, scene_index),
                    FOREIGN KEY(material_id) REFERENCES materials(material_id) ON DELETE CASCADE,
                    FOREIGN KEY(segment_id) REFERENCES material_segments(segment_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_material_task_scenes_material
                    ON material_task_scenes(material_id, scene_index);

                -- 旧版会把自动打标或无人工标签的素材停在 review，等待用户再点
                -- 一次“确认入库”。素材库现在采用自动入库，历史记录也直接迁移为
                -- ready，避免已经存在的素材因为旧状态无法参与匹配。
                UPDATE material_segments SET status = 'ready' WHERE status = 'review';
                UPDATE materials SET status = 'ready' WHERE status = 'review';
                """
            )

    def save_upload(
        self,
        filename: str,
        source: BinaryIO,
        *,
        manual_tags: str | Iterable[str] | None = None,
        ai_tagging: bool = False,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        safe_name = os.path.basename((filename or "").replace("\\", "/")).strip()
        extension = Path(safe_name).suffix.lower()
        if not safe_name or extension not in MATERIAL_EXTENSIONS:
            raise MaterialLibraryError("unsupported material file type")
        local_dir = utils.storage_dir("local_videos", create=True)
        temp_path = os.path.join(local_dir, f".upload-{uuid4().hex}{extension}")
        digest = hashlib.sha256()
        size = 0
        try:
            with open(temp_path, "wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 4 * 1024 * 1024 * 1024:
                        raise MaterialLibraryError("material file exceeds 4 GB")
                    digest.update(chunk)
                    output.write(chunk)
            if size <= 0:
                raise MaterialLibraryError("material file is empty")
            existing = self.get_by_hash(digest.hexdigest())
            if existing:
                os.remove(temp_path)
                tags = normalize_tags(manual_tags)
                if tags:
                    self.update_material_tags(
                        existing["material_id"],
                        normalize_tags((existing.get("tags") or []) + tags),
                    )
                existing = self.get(existing["material_id"])
                existing["duplicate"] = True
                if progress_callback:
                    progress_callback(1, 1, "已合并重复素材")
                return existing
            material_id = str(uuid4())
            final_path = os.path.join(local_dir, f"material-{material_id}{extension}")
            os.replace(temp_path, final_path)
            media_type = "image" if extension in MATERIAL_IMAGE_EXTENSIONS else "video"
            now = _utc_now()
            with self.store.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO materials(
                        material_id, name, original_path, sha256, media_type, status,
                        ai_tagging, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        material_id,
                        safe_name,
                        final_path,
                        digest.hexdigest(),
                        media_type,
                        MATERIAL_STATUS_PROCESSING,
                        int(ai_tagging),
                        now,
                        now,
                    ),
                )
            self._process_material(
                material_id,
                normalize_tags(manual_tags),
                ai_tagging,
                progress_callback=progress_callback,
            )
            return self.get(material_id)
        except Exception:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
            raise

    def _process_material(
        self,
        material_id: str,
        manual_tags: list[str],
        ai_tagging: bool,
        *,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> None:
        material = self.get(material_id, include_segments=False)
        if not material:
            raise MaterialLibraryError("material does not exist")
        try:
            width, height, duration = self._probe(material["original_path"])
            if material["media_type"] == "image":
                ranges = [(0.0, 0.0, material["original_path"])]
            else:
                ranges = [
                    (start, end, "")
                    for start, end in self._scene_ranges(
                        material["original_path"], duration
                    )
                ]
            total_ranges = max(1, len(ranges))
            if progress_callback:
                progress_callback(0, total_ranges, "正在准备素材片段")
            material_dir = (
                Path(utils.storage_dir("materials", create=True)) / material_id
            )
            material_dir.mkdir(parents=True, exist_ok=True)
            segment_rows = []
            errors = []
            for index, (start, end, existing_path) in enumerate(ranges, start=1):
                segment_id = str(uuid4())
                if material["media_type"] == "image":
                    segment_path = existing_path
                else:
                    segment_path = str(material_dir / f"segment-{index:04d}.mp4")
                    self._extract_segment(
                        material["original_path"], segment_path, start, end - start
                    )
                thumbnail_path = str(material_dir / f"thumb-{index:04d}.jpg")
                self._create_thumbnail(
                    segment_path,
                    thumbnail_path,
                    media_type=material["media_type"],
                    duration=max(0.0, end - start),
                )
                analysis = {"description_zh": "", "tags_zh": [], "tags_en": []}
                analysis_error = ""
                if ai_tagging:
                    try:
                        analysis = (
                            vision.analyze_images([segment_path])
                            if material["media_type"] == "image"
                            else vision.analyze_video_clip(segment_path, end - start)
                        )
                    except Exception as exc:
                        analysis_error = str(exc)
                        errors.append(f"片段 {index}: {analysis_error}")
                        logger.warning(
                            f"material visual tagging failed: material={material_id}, "
                            f"segment={index}, error={exc}"
                        )
                status = MATERIAL_STATUS_READY
                now = _utc_now()
                segment_rows.append(
                    (
                        segment_id,
                        material_id,
                        index,
                        start,
                        end,
                        segment_path,
                        thumbnail_path,
                        status,
                        analysis["description_zh"],
                        _json_dump(manual_tags),
                        _json_dump(analysis["tags_zh"]),
                        _json_dump(analysis["tags_en"]),
                        analysis_error,
                        now,
                        now,
                    )
                )
                if progress_callback:
                    progress_callback(
                        index, total_ranges, f"已处理片段 {index}/{total_ranges}"
                    )
            final_status = MATERIAL_STATUS_READY
            with self.store.connection() as connection:
                connection.executemany(
                    """
                    INSERT INTO material_segments(
                        segment_id, material_id, segment_index, start_time, end_time,
                        file_path, thumbnail_path, status, description_zh,
                        manual_tags_json, ai_tags_zh_json, tags_en_json, error,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    segment_rows,
                )
                connection.execute(
                    """
                    UPDATE materials SET width = ?, height = ?, duration = ?, status = ?,
                        error = ?, updated_at = ? WHERE material_id = ?
                    """,
                    (
                        width,
                        height,
                        duration,
                        final_status,
                        "\n".join(errors)[:2000],
                        _utc_now(),
                        material_id,
                    ),
                )
        except Exception as exc:
            with self.store.connection() as connection:
                connection.execute(
                    "UPDATE materials SET status = ?, error = ?, updated_at = ? WHERE material_id = ?",
                    (MATERIAL_STATUS_FAILED, str(exc)[:2000], _utc_now(), material_id),
                )
            raise

    def _probe(self, path: str) -> tuple[int, int, float]:
        if Path(path).suffix.lower() in MATERIAL_IMAGE_EXTENSIONS:
            with Image.open(path) as image:
                return image.width, image.height, 0.0
        ffmpeg = Path(utils.get_ffmpeg_binary())
        ffprobe = shutil.which("ffprobe") or str(ffmpeg.with_name("ffprobe"))
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise MaterialLibraryError(
                result.stderr.strip() or "failed to inspect video"
            )
        payload = json.loads(result.stdout)
        stream = (payload.get("streams") or [{}])[0]
        duration = float((payload.get("format") or {}).get("duration") or 0)
        if duration <= 0:
            raise MaterialLibraryError("video duration is invalid")
        return int(stream.get("width") or 0), int(stream.get("height") or 0), duration

    def _scene_ranges(self, path: str, duration: float) -> list[tuple[float, float]]:
        threshold = min(
            1.0, max(0.05, float(config.app.get("material_scene_threshold", 0.35)))
        )
        minimum = max(0.5, float(config.app.get("material_min_segment_duration", 2)))
        maximum = max(
            minimum, float(config.app.get("material_max_segment_duration", 10))
        )
        command = [
            utils.get_ffmpeg_binary(),
            "-hide_banner",
            "-i",
            path,
            "-filter:v",
            f"select='gt(scene,{threshold})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(120, int(duration * 3)),
        )
        points = [
            float(value)
            for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr)
        ]
        raw_boundaries = (
            [0.0] + [point for point in points if 0 < point < duration] + [duration]
        )
        boundaries = [0.0]
        for point in sorted(set(raw_boundaries[1:])):
            if point - boundaries[-1] >= minimum or point == duration:
                boundaries.append(point)
        if boundaries[-1] != duration:
            boundaries.append(duration)

        ranges: list[tuple[float, float]] = []
        for start, end in zip(boundaries, boundaries[1:]):
            cursor = start
            while end - cursor > maximum:
                ranges.append((cursor, cursor + maximum))
                cursor += maximum
            if end - cursor >= minimum:
                ranges.append((cursor, end))
            elif ranges:
                previous_start, _ = ranges[-1]
                ranges[-1] = (previous_start, end)
            elif end > cursor:
                ranges.append((cursor, end))
        return ranges or [(0.0, duration)]

    def _extract_segment(
        self, source: str, output: str, start: float, duration: float
    ) -> None:
        result = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.3f}",
                "-i",
                source,
                "-t",
                f"{duration:.3f}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "21",
                "-movflags",
                "+faststart",
                "-y",
                output,
            ],
            capture_output=True,
            text=True,
            timeout=max(120, int(duration * 10)),
        )
        if result.returncode != 0 or not os.path.isfile(output):
            raise MaterialLibraryError(result.stderr.strip() or "failed to split video")

    def _create_thumbnail(
        self,
        source: str,
        output: str,
        *,
        media_type: str,
        duration: float,
    ) -> None:
        if media_type == "image":
            with Image.open(source) as image:
                image.thumbnail((480, 320))
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(output, "JPEG", quality=85)
            return
        result = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, duration / 2):.3f}",
                "-i",
                source,
                "-frames:v",
                "1",
                "-vf",
                "scale='min(480,iw)':-2",
                "-y",
                output,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 or not os.path.isfile(output):
            raise MaterialLibraryError(
                result.stderr.strip() or "failed to create thumbnail"
            )

    def get_by_hash(self, sha256: str) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT material_id FROM materials WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return self.get(row["material_id"]) if row else None

    def get(
        self, material_id: str, *, include_segments: bool = True
    ) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM materials WHERE material_id = ?", (material_id,)
            ).fetchone()
            segment_rows = (
                connection.execute(
                    "SELECT * FROM material_segments WHERE material_id = ? ORDER BY segment_index",
                    (material_id,),
                ).fetchall()
                if row and include_segments
                else []
            )
            task_scene_rows = (
                connection.execute(
                    """
                    SELECT task_id, scene_index, segment_id, provider, source_url,
                           scene_text, search_query, created_at
                    FROM material_task_scenes
                    WHERE material_id = ?
                    ORDER BY created_at DESC
                    """,
                    (material_id,),
                ).fetchall()
                if row and include_segments
                else []
            )
        if not row:
            return None
        result = dict(row)
        result["ai_tagging"] = bool(result["ai_tagging"])
        if include_segments:
            result["segments"] = [
                self._segment_dict(segment) for segment in segment_rows
            ]
            result["task_scenes"] = [dict(item) for item in task_scene_rows]
            result["tags"] = normalize_tags(
                tag
                for segment in result["segments"]
                for tag in segment.get("tags") or []
            )
        return result

    def list(
        self,
        *,
        query: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 30,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = []
        values: list[Any] = []
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        page_size = max(1, min(100, int(page_size)))
        with self.store.connection() as connection:
            rows = connection.execute(
                f"SELECT material_id FROM materials{where} ORDER BY updated_at DESC",
                values,
            ).fetchall()
        materials = [self.get(row["material_id"]) for row in rows]
        needle = query.strip().casefold()
        if needle:
            materials = [
                item
                for item in materials
                if needle in item["name"].casefold()
                or needle in str(item.get("description_zh") or "").casefold()
                or any(needle in tag.casefold() for tag in item.get("tags") or [])
            ]
        total = len(materials)
        offset = max(0, page - 1) * page_size
        return materials[offset : offset + page_size], total

    def _segment_dict(self, row) -> dict[str, Any]:
        result = dict(row)
        result["manual_tags"] = _json_load(result.pop("manual_tags_json"), [])
        result["ai_tags_zh"] = _json_load(result.pop("ai_tags_zh_json"), [])
        result["tags_en"] = _json_load(result.pop("tags_en_json"), [])
        result["tags"] = normalize_tags(result["manual_tags"] + result["ai_tags_zh"])
        return result

    def update_segment(
        self,
        segment_id: str,
        *,
        manual_tags: str | Iterable[str] | None = None,
        description_zh: str | None = None,
    ) -> bool:
        fields = []
        values: list[Any] = []
        material_id = ""
        if manual_tags is not None:
            with self.store.connection() as connection:
                row = connection.execute(
                    "SELECT material_id FROM material_segments WHERE segment_id = ?",
                    (segment_id,),
                ).fetchone()
            material_id = str(row["material_id"] if row else "")
            if not material_id:
                return False
            self.update_material_tags(material_id, manual_tags)
        if description_zh is not None:
            fields.append("description_zh = ?")
            values.append(str(description_zh).strip()[:500])
        if not fields:
            return bool(material_id)
        fields.append("updated_at = ?")
        values.append(_utc_now())
        values.append(segment_id)
        with self.store.connection() as connection:
            cursor = connection.execute(
                f"UPDATE material_segments SET {', '.join(fields)} WHERE segment_id = ?",
                values,
            )
        return cursor.rowcount == 1

    def update_material_tags(
        self,
        material_id: str,
        tags: str | Iterable[str] | None,
    ) -> dict[str, Any] | None:
        """Replace the material's tags and apply them to all of its segments."""
        normalized = normalize_tags(tags)
        now = _utc_now()
        with self.store.connection() as connection:
            material = connection.execute(
                "SELECT material_id FROM materials WHERE material_id = ?",
                (material_id,),
            ).fetchone()
            if not material:
                return None
            connection.execute(
                """
                UPDATE material_segments
                SET manual_tags_json = ?, ai_tags_zh_json = '[]',
                    tags_en_json = '[]', updated_at = ?
                WHERE material_id = ?
                """,
                (_json_dump(normalized), now, material_id),
            )
            connection.execute(
                "UPDATE materials SET updated_at = ? WHERE material_id = ?",
                (now, material_id),
            )
        return self.get(material_id)

    def update_material(
        self,
        material_id: str,
        *,
        tags: str | Iterable[str] | None = None,
        description_zh: str | None = None,
    ) -> dict[str, Any] | None:
        if tags is not None:
            if not self.update_material_tags(material_id, tags):
                return None
        if description_zh is not None:
            with self.store.connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE materials SET description_zh = ?, updated_at = ?
                    WHERE material_id = ?
                    """,
                    (str(description_zh).strip()[:500], _utc_now(), material_id),
                )
            if cursor.rowcount != 1:
                return None
        return self.get(material_id)

    def confirm(self, material_id: str) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE material_segments SET status = ?, updated_at = ? WHERE material_id = ?",
                (MATERIAL_STATUS_READY, _utc_now(), material_id),
            )
            cursor = connection.execute(
                "UPDATE materials SET status = ?, error = '', updated_at = ? WHERE material_id = ?",
                (MATERIAL_STATUS_READY, _utc_now(), material_id),
            )
        return self.get(material_id) if cursor.rowcount else None

    def retag(
        self,
        material_id: str,
        *,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any] | None:
        material = self.get(material_id)
        if not material:
            return None
        errors = []
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE materials SET status = ?, updated_at = ? WHERE material_id = ?",
                (MATERIAL_STATUS_PROCESSING, _utc_now(), material_id),
            )
        total_segments = max(1, len(material["segments"]))
        if progress_callback:
            progress_callback(0, total_segments, "正在准备重新打标")
        for index, segment in enumerate(material["segments"], start=1):
            try:
                duration = max(0.0, segment["end_time"] - segment["start_time"])
                result = (
                    vision.analyze_images([segment["file_path"]])
                    if material["media_type"] == "image"
                    else vision.analyze_video_clip(segment["file_path"], duration)
                )
                with self.store.connection() as connection:
                    connection.execute(
                        """
                        UPDATE material_segments SET description_zh = ?, ai_tags_zh_json = ?,
                            tags_en_json = ?, status = ?, error = '', updated_at = ?
                        WHERE segment_id = ?
                        """,
                        (
                            result["description_zh"],
                            _json_dump(result["tags_zh"]),
                            _json_dump(result["tags_en"]),
                            MATERIAL_STATUS_READY,
                            _utc_now(),
                            segment["segment_id"],
                        ),
                    )
            except Exception as exc:
                errors.append(str(exc))
            if progress_callback:
                progress_callback(
                    index, total_segments, f"已识别片段 {index}/{total_segments}"
                )
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE materials SET status = ?, error = ?, updated_at = ? WHERE material_id = ?",
                (
                    MATERIAL_STATUS_READY,
                    "\n".join(errors)[:2000],
                    _utc_now(),
                    material_id,
                ),
            )
        return self.get(material_id)

    def delete(self, material_id: str) -> bool:
        material = self.get(material_id)
        if not material:
            return False
        with self.store.connection() as connection:
            connection.execute(
                "DELETE FROM material_task_scenes WHERE material_id = ?", (material_id,)
            )
            connection.execute(
                "DELETE FROM materials WHERE material_id = ?", (material_id,)
            )
        material_root = utils.storage_dir("materials")
        candidate = os.path.join(material_root, material_id)
        try:
            safe_dir = file_security.resolve_path_within_directory(
                material_root, candidate
            )
        except ValueError:
            safe_dir = ""
        if safe_dir and os.path.isdir(safe_dir):
            shutil.rmtree(safe_dir, ignore_errors=True)
        local_root = utils.storage_dir("local_videos")
        try:
            safe_original = file_security.resolve_path_within_directory(
                local_root, material["original_path"]
            )
        except ValueError:
            safe_original = ""
        if safe_original and os.path.isfile(safe_original):
            os.remove(safe_original)
        return True

    def import_task_scene(
        self,
        task_id: str,
        scene_index: int,
        file_path: str,
        *,
        scene_text: str = "",
        search_query: str = "",
        provider: str = "",
        source_url: str = "",
    ) -> dict[str, Any]:
        """Copy one completed task scene into the durable material library."""
        task_id = str(task_id or "").strip()
        source_path = Path(file_path)
        if not task_id or not source_path.is_file():
            raise MaterialLibraryError("task scene file does not exist")
        scene_index = int(scene_index)

        provider_label = {
            "pexels": "Pexels",
            "pixabay": "Pixabay",
            "coverr": "Coverr",
            "local": "本地素材",
            "seedance": "AI 生成",
            "ai": "AI 生成",
            "user_upload": "用户上传",
        }.get(str(provider or "").casefold(), str(provider or "").strip())
        tags = normalize_tags(["任务分镜", provider_label, search_query])
        with source_path.open("rb") as source:
            material = self.save_upload(
                f"任务-{task_id[:8]}-分镜-{scene_index + 1}{source_path.suffix.lower()}",
                source,
                manual_tags=tags,
                ai_tagging=False,
            )
        segments = material.get("segments") or []
        if not segments:
            raise MaterialLibraryError("task scene import produced no material segment")
        segment = segments[0]
        description = str(scene_text or "").strip()[:500]
        if description and not segment.get("description_zh"):
            self.update_segment(segment["segment_id"], description_zh=description)
        now = _utc_now()
        with self.store.connection() as connection:
            connection.execute(
                """
                INSERT INTO material_task_scenes(
                    task_id, scene_index, material_id, segment_id, provider,
                    source_url, scene_text, search_query, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, scene_index) DO UPDATE SET
                    material_id = excluded.material_id,
                    segment_id = excluded.segment_id,
                    provider = excluded.provider,
                    source_url = excluded.source_url,
                    scene_text = excluded.scene_text,
                    search_query = excluded.search_query,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    scene_index,
                    material["material_id"],
                    segment["segment_id"],
                    str(provider or "")[:60],
                    str(source_url or "")[:2000],
                    description,
                    str(search_query or "")[:500],
                    now,
                    now,
                ),
            )
        return self.get(material["material_id"])

    def list_tags(self, query: str = "") -> list[dict[str, Any]]:
        """Return each tag together with the materials related to it."""
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.material_id, s.segment_index, s.thumbnail_path,
                       s.manual_tags_json, s.ai_tags_zh_json, s.tags_en_json,
                       m.name AS material_name, m.media_type, m.original_path
                FROM material_segments s
                JOIN materials m ON m.material_id = s.material_id
                ORDER BY s.material_id, s.segment_index
                """
            ).fetchall()
        aggregated: dict[str, dict[str, Any]] = {}
        for row in rows:
            sources = (
                ("manual", _json_load(row["manual_tags_json"], [])),
                ("ai_zh", _json_load(row["ai_tags_zh_json"], [])),
                ("ai_en", _json_load(row["tags_en_json"], [])),
            )
            seen_in_segment = set()
            for source, values in sources:
                for tag in normalize_tags(values):
                    key = tag.casefold()
                    item = aggregated.setdefault(
                        key,
                        {
                            "name": tag,
                            "segment_count": 0,
                            "materials": {},
                            "manual_count": 0,
                            "ai_count": 0,
                        },
                    )
                    item["materials"].setdefault(
                        row["material_id"],
                        {
                            "material_id": row["material_id"],
                            "name": row["material_name"],
                            "media_type": row["media_type"],
                            "original_path": row["original_path"],
                            "thumbnail_path": row["thumbnail_path"],
                        },
                    )
                    item["manual_count" if source == "manual" else "ai_count"] += 1
                    if key not in seen_in_segment:
                        item["segment_count"] += 1
                        seen_in_segment.add(key)
        needle = str(query or "").strip().casefold()
        result = []
        for key, item in aggregated.items():
            if needle and needle not in key:
                continue
            materials = sorted(
                item["materials"].values(),
                key=lambda material: material["name"].casefold(),
            )
            result.append(
                {
                    "name": item["name"],
                    "segment_count": item["segment_count"],
                    "material_count": len(materials),
                    "material_ids": [item["material_id"] for item in materials],
                    "materials": materials,
                    "manual_count": item["manual_count"],
                    "ai_count": item["ai_count"],
                }
            )
        return sorted(
            result, key=lambda item: (-item["material_count"], item["name"].casefold())
        )

    def set_tag_materials(
        self,
        name: str,
        material_ids: Iterable[str] | None,
    ) -> int:
        """Set the complete material collection related to one tag."""
        names = normalize_tags([name])
        if not names:
            raise MaterialLibraryError("tag is invalid")
        canonical_name = names[0]
        tag_key = canonical_name.casefold()
        requested = {str(value).strip() for value in material_ids or [] if value}
        with self.store.connection() as connection:
            existing_ids = {
                row["material_id"]
                for row in connection.execute("SELECT material_id FROM materials")
            }
            if not requested <= existing_ids:
                raise MaterialLibraryError("material does not exist")
            rows = connection.execute(
                """
                SELECT segment_id, material_id, manual_tags_json,
                       ai_tags_zh_json, tags_en_json
                FROM material_segments
                """
            ).fetchall()
            changed_materials = set()
            for row in rows:
                manual = normalize_tags(_json_load(row["manual_tags_json"], []))
                ai_zh = normalize_tags(_json_load(row["ai_tags_zh_json"], []))
                ai_en = normalize_tags(_json_load(row["tags_en_json"], []))
                had_tag = any(
                    value.casefold() == tag_key
                    for value in (*manual, *ai_zh, *ai_en)
                )
                should_have_tag = row["material_id"] in requested
                if had_tag == should_have_tag and (
                    not should_have_tag
                    or any(value.casefold() == tag_key for value in manual)
                ):
                    continue
                manual = [value for value in manual if value.casefold() != tag_key]
                ai_zh = [value for value in ai_zh if value.casefold() != tag_key]
                ai_en = [value for value in ai_en if value.casefold() != tag_key]
                if should_have_tag:
                    manual.append(canonical_name)
                connection.execute(
                    """
                    UPDATE material_segments
                    SET manual_tags_json = ?, ai_tags_zh_json = ?,
                        tags_en_json = ?, updated_at = ?
                    WHERE segment_id = ?
                    """,
                    (
                        _json_dump(normalize_tags(manual)),
                        _json_dump(normalize_tags(ai_zh)),
                        _json_dump(normalize_tags(ai_en)),
                        _utc_now(),
                        row["segment_id"],
                    ),
                )
                changed_materials.add(row["material_id"])
            if changed_materials:
                placeholders = ",".join("?" for _ in changed_materials)
                connection.execute(
                    f"UPDATE materials SET updated_at = ? WHERE material_id IN ({placeholders})",
                    (_utc_now(), *sorted(changed_materials)),
                )
        return len(requested)

    def rename_tag(self, old_name: str, new_name: str) -> int:
        new_tags = normalize_tags([new_name])
        if not new_tags:
            raise MaterialLibraryError("new tag is invalid")
        return self._replace_tag(old_name, new_tags[0])

    def delete_tag(self, name: str) -> int:
        return self._replace_tag(name, None)

    def _replace_tag(self, old_name: str, new_name: str | None) -> int:
        old_key = str(old_name or "").strip().casefold()
        if not old_key:
            raise MaterialLibraryError("tag is required")
        columns = ("manual_tags_json", "ai_tags_zh_json", "tags_en_json")
        updated_materials = set()
        with self.store.connection() as connection:
            rows = connection.execute(
                f"SELECT segment_id, material_id, {', '.join(columns)} "
                "FROM material_segments"
            ).fetchall()
            for row in rows:
                fields = []
                values: list[Any] = []
                changed = False
                for column in columns:
                    current = normalize_tags(_json_load(row[column], []))
                    if not any(tag.casefold() == old_key for tag in current):
                        continue
                    replacement = [
                        new_name if tag.casefold() == old_key and new_name else tag
                        for tag in current
                        if tag.casefold() != old_key or new_name
                    ]
                    fields.append(f"{column} = ?")
                    values.append(_json_dump(normalize_tags(replacement)))
                    changed = True
                if changed:
                    fields.append("updated_at = ?")
                    values.extend((_utc_now(), row["segment_id"]))
                    connection.execute(
                        f"UPDATE material_segments SET {', '.join(fields)} WHERE segment_id = ?",
                        values,
                    )
                    updated_materials.add(row["material_id"])
        return len(updated_materials)

    def import_legacy(self) -> int:
        local_root = Path(utils.storage_dir("local_videos", create=True))
        imported = 0
        for path in local_root.iterdir():
            if (
                not path.is_file()
                or path.name.startswith(".")
                or path.suffix.lower() not in MATERIAL_EXTENSIONS
            ):
                continue
            digest_builder = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest_builder.update(chunk)
            digest = digest_builder.hexdigest()
            if self.get_by_hash(digest):
                continue
            media_type = (
                "image" if path.suffix.lower() in MATERIAL_IMAGE_EXTENSIONS else "video"
            )
            material_id = str(uuid4())
            now = _utc_now()
            try:
                width, height, duration = self._probe(str(path))
            except Exception as exc:
                width, height, duration = 0, 0, 0.0
                error = str(exc)
            else:
                error = ""
            segment_id = str(uuid4())
            with self.store.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO materials(
                        material_id, name, original_path, sha256, media_type, status,
                        width, height, duration, error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        material_id,
                        path.name,
                        str(path),
                        digest,
                        media_type,
                        MATERIAL_STATUS_READY,
                        width,
                        height,
                        duration,
                        error,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO material_segments(
                        segment_id, material_id, segment_index, start_time, end_time,
                        file_path, status, created_at, updated_at
                    ) VALUES (?, ?, 1, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment_id,
                        material_id,
                        duration,
                        str(path),
                        MATERIAL_STATUS_READY,
                        now,
                        now,
                    ),
                )
            imported += 1
        return imported

    def import_task_history(self) -> int:
        """Backfill completed scene files from tasks created before auto-archiving."""
        imported = 0
        page = 1
        processed = 0
        while True:
            tasks, total = self.store.list_tasks(page=page, page_size=100)
            if not tasks:
                break
            for task in tasks:
                task_id = str(task.get("task_id") or "").strip()
                scene_plan = task.get("scene_plan") or []
                generated = task.get("generated_scene_paths") or {}
                selected = task.get("selected_materials") or []
                params = task.get("params") or {}
                fallback_provider = str(params.get("video_source") or "")
                for offset, scene in enumerate(scene_plan):
                    try:
                        scene_index = int(scene.get("scene_index", offset))
                    except (TypeError, ValueError):
                        scene_index = offset
                    file_path = generated.get(str(scene_index)) or generated.get(
                        scene_index
                    )
                    if not file_path and scene_index < len(selected):
                        file_path = selected[scene_index]
                    if not file_path or not os.path.isfile(file_path):
                        continue
                    before = self.get_task_scene(task_id, scene_index)
                    self.import_task_scene(
                        task_id,
                        scene_index,
                        file_path,
                        scene_text=str(scene.get("text") or ""),
                        search_query=str(scene.get("search_query") or ""),
                        provider=fallback_provider,
                    )
                    if before is None:
                        imported += 1
            processed += len(tasks)
            if processed >= int(total or 0):
                break
            page += 1
        return imported

    def get_task_scene(self, task_id: str, scene_index: int) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, scene_index, material_id, segment_id, provider,
                       source_url, scene_text, search_query, created_at
                FROM material_task_scenes
                WHERE task_id = ? AND scene_index = ?
                """,
                (task_id, int(scene_index)),
            ).fetchone()
        return dict(row) if row else None

    def find_matching_segments(
        self,
        query: str,
        *,
        limit: int = 5,
        exclude_ids: Iterable[str] | None = None,
        exclude_material_ids: Iterable[str] | None = None,
        exclude_task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        tokens = {
            token.casefold()
            for token in re.findall(r"[\w\u4e00-\u9fff]+", query)
            if len(token) > 1
        }
        excluded = set(exclude_ids or [])
        excluded_materials = set(exclude_material_ids or [])
        task_id = str(exclude_task_id or "").strip()
        with self.store.connection() as connection:
            clauses = [
                "s.status = ?",
                "m.status = ?",
                """
                NOT EXISTS (
                    SELECT 1 FROM material_task_scenes local_task_scene
                    WHERE local_task_scene.material_id = s.material_id
                      AND local_task_scene.provider = 'local'
                )
                """,
            ]
            values: list[Any] = [MATERIAL_STATUS_READY, MATERIAL_STATUS_READY]
            if task_id:
                clauses.append(
                    """
                    NOT EXISTS (
                        SELECT 1 FROM material_task_scenes current_task_scene
                        WHERE current_task_scene.material_id = s.material_id
                          AND current_task_scene.task_id = ?
                    )
                    """
                )
                values.append(task_id)
            rows = connection.execute(
                f"""
                SELECT s.*, m.media_type, m.name AS material_name
                FROM material_segments s JOIN materials m USING(material_id)
                WHERE {' AND '.join(clauses)}
                """,
                values,
            ).fetchall()
        scored = []
        for row in rows:
            segment = self._segment_dict(row)
            if (
                segment["segment_id"] in excluded
                or segment["material_id"] in excluded_materials
            ):
                continue
            manual = {tag.casefold() for tag in segment["manual_tags"]}
            ai_zh = {tag.casefold() for tag in segment["ai_tags_zh"]}
            english = {tag.casefold() for tag in segment["tags_en"]}
            description = segment["description_zh"].casefold()
            score = sum(
                3
                for token in tokens
                if any(token in tag or tag in token for tag in manual)
            )
            score += sum(
                2
                for token in tokens
                if any(token in tag or tag in token for tag in ai_zh | english)
            )
            score += sum(1 for token in tokens if token in description)
            if score:
                segment["match_score"] = score
                scored.append(segment)
        scored.sort(
            key=lambda item: (
                -item["match_score"],
                item["material_id"],
                item["segment_index"],
            )
        )
        return scored[: max(1, int(limit))]


_library: MaterialLibrary | None = None


def get_material_library() -> MaterialLibrary:
    global _library
    if _library is None:
        _library = MaterialLibrary()
        imported = _library.import_legacy()
        if imported:
            logger.info(f"registered {imported} legacy local materials")
        history_imported = _library.import_task_history()
        if history_imported:
            logger.info(f"archived {history_imported} historical task scenes")
    return _library
