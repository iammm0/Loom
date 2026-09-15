from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoAspect, VideoParams
from app.services import llm, material, video, vision
from app.services.material_library import get_material_library
from app.services.task_store import get_task_store
from app.utils import utils


ONLINE_SOURCES = ("pexels", "pixabay", "coverr")
STOCK_SOURCES = ("local", *ONLINE_SOURCES)
DEFAULT_STOCK_SOURCES = ["local", "pexels", "pixabay", "coverr"]


def stock_sources_from(requested: Iterable[str] | None) -> list[str]:
    """Keep only the built-in library and stock video providers, in fixed order."""
    result: list[str] = []
    for source in STOCK_SOURCES:
        if source in (requested or []) and source not in result:
            result.append(source)
    return result


def configured_stock_sources() -> list[str]:
    """Return workspace-enabled stock sources, or the default mix when unset."""
    return stock_sources_from(config.app.get("video_sources")) or list(
        DEFAULT_STOCK_SOURCES
    )


def _task_cancel_requested(store, task_id: str) -> bool:
    """Read the persisted cancellation flag before resolving another scene."""
    task = store.get_task(task_id) or {}
    return bool(
        task.get("cancel_requested")
        or task.get("status") == const.TASK_STATUS_CANCELLATION_REQUESTED
    )


def _split_text_parts(text: str, count: int) -> list[str]:
    parts = [
        value
        for value in re.findall(r".*?[。！？!?；;，,：:\n]|.+$", text or "")
        if value
    ] or [text]
    while len(parts) < count:
        index = max(range(len(parts)), key=lambda item: len(parts[item]))
        value = parts[index]
        if len(value) <= 1:
            break
        middle = max(1, len(value) // 2)
        parts[index : index + 1] = [value[:middle], value[middle:]]
    while len(parts) > count:
        parts[-2] += parts.pop()
    return [part for part in parts if part]


def build_narration_units(
    script: str,
    audio_duration: float,
    preferred_scene_duration: float,
    sub_maker: Any = None,
) -> list[dict[str, Any]]:
    """Build timestamped atomic narration units without relying on sentence count."""
    duration = max(0.1, float(audio_duration))
    raw_units: list[tuple[str, float, float]] = []
    cues = list(getattr(sub_maker, "cues", []) or [])
    for cue in cues:
        try:
            start = float(cue.start.total_seconds())
            end = float(cue.end.total_seconds())
            text = str(getattr(cue, "text", "") or "")
        except (AttributeError, TypeError, ValueError):
            continue
        if text and end > start:
            raw_units.append((text, start, min(duration, end)))
    if not raw_units:
        for text, offsets in zip(
            list(getattr(sub_maker, "subs", []) or []),
            list(getattr(sub_maker, "offset", []) or []),
        ):
            try:
                start, end = (
                    float(offsets[0]) / 10_000_000,
                    float(offsets[1]) / 10_000_000,
                )
            except (TypeError, ValueError, IndexError):
                continue
            if str(text) and end > start:
                raw_units.append((str(text), start, min(duration, end)))
    if not raw_units:
        raw_units = [(script.strip(), 0.0, duration)]

    atomic_duration = min(2.5, max(0.75, float(preferred_scene_duration) / 2))
    units: list[dict[str, Any]] = []
    for text, start, end in raw_units:
        segment_duration = max(0.0, end - start)
        count = max(1, int(math.ceil(segment_duration / atomic_duration)))
        parts = _split_text_parts(text, count)
        weights = [max(1, len(part.strip())) for part in parts]
        total_weight = sum(weights)
        cursor = start
        for part_index, (part, weight) in enumerate(zip(parts, weights)):
            next_cursor = (
                end
                if part_index == len(parts) - 1
                else cursor + segment_duration * weight / total_weight
            )
            units.append(
                {
                    "unit_index": len(units),
                    "text": part,
                    "start": round(cursor, 3),
                    "end": round(next_cursor, 3),
                }
            )
            cursor = next_cursor
    if units:
        units[0]["start"] = 0.0
        units[-1]["end"] = round(duration, 3)
    return units


class ScenePlanInvariantError(RuntimeError):
    def __init__(self, required_scene_count: int, planned_scene_count: int):
        self.required_scene_count = int(required_scene_count)
        self.planned_scene_count = int(planned_scene_count)
        super().__init__(
            "scene plan does not satisfy minimum scene count: "
            f"required={self.required_scene_count}, "
            f"planned={self.planned_scene_count}"
        )


def _split_fragment(fragment: str) -> tuple[str, str] | None:
    value = fragment
    if len(value.strip()) <= 1:
        return None

    midpoint = len(value) / 2
    boundary_patterns = (
        r"[。！？!?；;\.]|\n+",
        r"[，,：:、]",
        r"\s+",
    )
    for pattern in boundary_patterns:
        positions = [
            match.end()
            for match in re.finditer(pattern, value)
            if 0 < match.end() < len(value)
        ]
        for position in sorted(positions, key=lambda item: abs(item - midpoint)):
            left, right = value[:position], value[position:]
            if left.strip() and right.strip():
                return left, right

    position = max(1, min(len(value) - 1, int(round(midpoint))))
    left, right = value[:position], value[position:]
    return (left, right) if left.strip() and right.strip() else None


def _split_script(script: str, count: int) -> list[str]:
    """Split narration into exactly ``count`` ordered chunks when possible."""
    value = (script or "").strip()
    if not value:
        return []

    requested = max(1, int(count))
    chunks = [value]
    while len(chunks) < requested:
        candidates = sorted(
            range(len(chunks)), key=lambda index: len(chunks[index]), reverse=True
        )
        split_result = None
        split_index = -1
        for index in candidates:
            split_result = _split_fragment(chunks[index])
            if split_result:
                split_index = index
                break
        if split_result is None:
            break
        chunks[split_index : split_index + 1] = list(split_result)
    return chunks


def build_scene_plan(
    script: str,
    search_terms: Iterable[str],
    audio_duration: float,
    clip_duration: int,
) -> list[dict[str, Any]]:
    maximum_scene_duration = max(1, int(clip_duration))
    required_visual_duration = video.get_required_video_duration(audio_duration)
    scene_count = max(
        1, int(math.ceil(required_visual_duration / maximum_scene_duration))
    )
    chunks = _split_script(script, scene_count)
    narration_chunk_count = len(chunks)
    while len(chunks) < scene_count:
        chunks.append("")
    terms = [str(term).strip() for term in search_terms if str(term).strip()]
    if not terms:
        terms = [chunk for chunk in chunks if chunk] or [script.strip()]
    target_duration = math.ceil(required_visual_duration / scene_count * 1000) / 1000
    scenes = []
    for index, chunk in enumerate(chunks):
        query = terms[min(index, len(terms) - 1)]
        visual_context = chunk or script.strip()
        scenes.append(
            {
                "scene_index": index,
                "text": chunk,
                "search_query": query,
                "target_duration": round(target_duration, 3),
                "supplemental_visual": index >= narration_chunk_count,
                "seedance_prompt": (
                    f"为旁白制作写实电影感 B-roll：{visual_context}。"
                    "自然运动，画面稳定，无字幕、无文字、无水印、无品牌标识。"
                ),
            }
        )
    if len(scenes) < scene_count or any(
        float(scene["target_duration"]) > maximum_scene_duration + 0.001
        for scene in scenes
    ):
        raise ScenePlanInvariantError(scene_count, len(scenes))
    return scenes


def _configured_sources(params: VideoParams) -> list[str]:
    if params.material_strategy != "local_first":
        requested = [params.video_source or "pexels"]
    else:
        requested = list(params.video_sources or [])
    result = stock_sources_from(requested)
    if params.material_strategy == "local_first" and not result:
        return configured_stock_sources()
    return result


def _aspect_ratio_value(video_aspect: VideoAspect | str | None) -> str:
    return VideoAspect(video_aspect or VideoAspect.portrait.value).value


def _search_provider(source: str, query: str, duration: float, aspect: VideoAspect):
    function = {
        "pexels": material.search_videos_pexels,
        "pixabay": material.search_videos_pixabay,
        "coverr": material.search_videos_coverr,
    }[source]
    return function(
        search_term=query,
        minimum_duration=max(1, int(math.ceil(duration))),
        video_aspect=aspect,
    )


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_clip(source: str, output: str, duration: float) -> str:
    extension = Path(source).suffix.lower()
    command = [utils.get_ffmpeg_binary(), "-hide_banner", "-loglevel", "error"]
    if extension in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        command.extend(["-loop", "1", "-i", source])
    else:
        # 视频素材只允许沿自身时间线播放一次。短素材不能通过循环首尾来填满
        # 分镜时长，否则用户会看到同一个动作在一条成片里反复播放。实际输出
        # 比目标短时，后续统一的画面容量检查会把缺口转为待上传分镜。
        command.extend(["-i", source])
    command.extend(
        [
            "-t",
            f"{max(0.1, duration):.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            output,
        ]
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(120, int(duration * 20)),
    )
    if result.returncode != 0 or not os.path.isfile(output):
        raise RuntimeError(result.stderr.strip() or "failed to prepare material clip")
    return output


def _review_online_candidate(path: str, scene_text: str, duration: float) -> bool:
    if not vision.is_enabled():
        logger.warning(
            "vision model is not configured; accept stock candidate without review"
        )
        return True
    frames = vision.extract_video_frames(path, duration=duration, max_frames=3)
    if not frames:
        return False
    frame_dir = Path(frames[0]).parent
    try:
        return bool(vision.review_candidate(frames, scene_text).get("relevant"))
    finally:
        for frame in frames:
            Path(frame).unlink(missing_ok=True)
        try:
            frame_dir.rmdir()
        except OSError:
            pass


def _online_candidates(
    query: str,
    duration: float,
    aspect: VideoAspect,
    sources: list[str],
) -> list[Any]:
    results: dict[str, list[Any]] = {source: [] for source in sources}
    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as executor:
        future_map = {
            executor.submit(_search_provider, source, query, duration, aspect): source
            for source in sources
        }
        for future in as_completed(future_map):
            source = future_map[future]
            try:
                results[source] = future.result() or []
            except Exception as exc:
                logger.warning(f"material search failed: source={source}, error={exc}")
    candidates = []
    seen = set()
    # Round-robin keeps all three free providers represented without depending
    # on network completion order.
    for index in range(max((len(items) for items in results.values()), default=0)):
        for source in sources:
            items = results[source]
            if index >= len(items):
                continue
            item = items[index]
            key = _canonical_url(item.url)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)
    return candidates


def _replacement_segment(task: dict[str, Any], scene_index: int):
    replacement_ids = task.get("replacement_segment_ids") or {}
    segment_id = replacement_ids.get(str(scene_index)) or replacement_ids.get(
        scene_index
    )
    if not segment_id:
        return None
    library = get_material_library()
    with library.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM material_segments WHERE segment_id = ? AND status = ?",
            (segment_id, "ready"),
        ).fetchone()
    return library._segment_dict(row) if row else None


def _archive_task_scene(
    task_id: str,
    scene: dict[str, Any],
    file_path: str,
    *,
    provider: str,
    source_url: str = "",
) -> None:
    """Persist a completed scene without allowing library errors to fail a task."""
    if not file_path or not os.path.isfile(file_path):
        return
    try:
        get_material_library().import_task_scene(
            task_id,
            int(scene.get("scene_index", 0)),
            file_path,
            scene_text=str(scene.get("text") or ""),
            search_query=str(scene.get("search_query") or ""),
            provider=provider,
            source_url=source_url,
        )
    except Exception as exc:
        logger.warning(
            f"failed to archive task scene, task_id={task_id}, "
            f"scene={scene.get('scene_index')}, error={exc}"
        )


def prepare_ordered_materials(
    task_id: str,
    params: VideoParams,
    video_script: str,
    video_terms: list[str],
    audio_duration: float,
) -> dict[str, Any]:
    store = get_task_store()
    task = store.get_task(task_id) or {}
    scene_plan = task.get("scene_plan") or build_scene_plan(
        video_script,
        video_terms,
        audio_duration,
        params.video_clip_duration,
    )
    for scene in scene_plan:
        scene.setdefault(
            "material_prompt",
            str(
                scene.get("seedance_prompt")
                or scene.get("video_prompt")
                or scene.get("text")
                or ""
            ),
        )
    store.patch_task(
        task_id,
        scene_plan=scene_plan,
        required_visual_duration=video.get_required_video_duration(audio_duration),
        required_scene_count=max(
            1,
            math.ceil(
                video.get_required_video_duration(audio_duration)
                / max(1, int(params.video_clip_duration or 1))
            ),
        ),
        planned_scene_count=len(scene_plan),
    )
    sources = _configured_sources(params)
    online_sources = [source for source in ONLINE_SOURCES if source in sources]
    task_material_dir = Path(utils.task_dir(task_id)) / "materials"
    task_material_dir.mkdir(parents=True, exist_ok=True)
    aspect_ratio = _aspect_ratio_value(params.video_aspect)
    library = get_material_library()
    used_local_ids: set[str] = set()
    used_local_material_ids: set[str] = set()
    used_urls: set[str] = set()
    used_hashes: set[str] = set()
    paths_by_scene: dict[int, str] = {
        int(key): str(value)
        for key, value in (task.get("resolved_scene_paths") or {}).items()
        if str(key).isdigit() and os.path.isfile(str(value))
    }
    existing_generated = {
        int(key): value
        for key, value in (task.get("generated_scene_paths") or {}).items()
        if str(key).isdigit() and os.path.isfile(value)
    }
    paths_by_scene.update(existing_generated)
    scenes_by_index = {
        int(scene["scene_index"]): scene
        for scene in scene_plan
        if str(scene.get("scene_index", "")).isdigit()
    }
    for existing_index, existing_path in existing_generated.items():
        existing_scene = scenes_by_index.get(existing_index)
        if existing_scene:
            _archive_task_scene(
                task_id, existing_scene, existing_path, provider="seedance"
            )
    uploaded_paths = {
        int(key): os.path.abspath(str(value))
        for key, value in (task.get("uploaded_scene_paths") or {}).items()
        if str(key).isdigit() and os.path.isfile(str(value))
    }
    for uploaded_index, uploaded_path in uploaded_paths.items():
        uploaded_scene = scenes_by_index.get(uploaded_index)
        if not uploaded_scene:
            continue
        output = str(task_material_dir / f"scene-{uploaded_index:04d}-uploaded.mp4")
        _prepare_clip(
            uploaded_path,
            output,
            float(uploaded_scene.get("target_duration") or 0.1),
        )
        paths_by_scene[uploaded_index] = output
        _archive_task_scene(task_id, uploaded_scene, output, provider="user_upload")
    missing = []

    for scene in scene_plan:
        # An in-flight provider task cannot be safely abandoned, but once it
        # returns we must not submit the next billable scene after the user has
        # requested cancellation.
        if _task_cancel_requested(store, task_id):
            return {
                "paths": [],
                "missing": [
                    item
                    for item in scene_plan
                    if int(item["scene_index"]) not in paths_by_scene
                ],
                "errors": [],
                "status": const.TASK_STATUS_CANCELLATION_REQUESTED,
            }
        index = int(scene["scene_index"])
        if index in paths_by_scene:
            continue
        duration = float(scene["target_duration"])
        selected = _replacement_segment(task, index)
        if selected is None and "local" in sources:
            matches = library.find_matching_segments(
                f"{scene['search_query']} {scene['text']}",
                limit=5,
                exclude_ids=used_local_ids,
                exclude_material_ids=used_local_material_ids,
                exclude_task_id=task_id,
            )
            selected = matches[0] if matches else None
        if selected:
            material_id = str(selected.get("material_id") or "").strip()
            segment_id = str(selected.get("segment_id") or "").strip()
            if material_id and material_id in used_local_material_ids:
                selected = None
            elif segment_id and segment_id in used_local_ids:
                selected = None
        if selected:
            output = str(task_material_dir / f"scene-{index:04d}-local.mp4")
            _prepare_clip(selected["file_path"], output, duration)
            paths_by_scene[index] = output
            segment_id = str(selected.get("segment_id") or "").strip()
            material_id = str(selected.get("material_id") or "").strip()
            if segment_id:
                used_local_ids.add(segment_id)
            if material_id:
                used_local_material_ids.add(material_id)
            # 本地素材已经存在于素材库，不能把裁剪副本再次入库。旧逻辑会让
            # 下一分镜匹配到刚生成的副本，形成同一镜头不断转码、不断复用的
            # 自反馈链路。
            continue

        accepted = ""
        for candidate in _online_candidates(
            scene["search_query"],
            duration,
            VideoAspect(aspect_ratio),
            online_sources,
        )[:9]:
            canonical = _canonical_url(candidate.url)
            if canonical in used_urls:
                continue
            downloaded = material.save_video(
                candidate.url, save_dir=str(task_material_dir)
            )
            if not downloaded:
                continue
            content_hash = _sha256(downloaded)
            if content_hash in used_hashes:
                continue
            if not _review_online_candidate(downloaded, scene["text"], duration):
                continue
            accepted = str(
                task_material_dir / f"scene-{index:04d}-{candidate.provider}.mp4"
            )
            _prepare_clip(downloaded, accepted, duration)
            used_urls.add(canonical)
            used_hashes.add(content_hash)
            _archive_task_scene(
                task_id,
                scene,
                accepted,
                provider=str(candidate.provider or ""),
                source_url=str(candidate.url or ""),
            )
            break
        if accepted:
            paths_by_scene[index] = accepted
        else:
            scene["material_missing_reason"] = (
                "素材库和已配置来源中没有尚未在本作品使用过的合适素材，"
                "请按提示词上传新素材，或逐镜使用 Seedance 生成。"
            )
            missing.append(scene)

    if missing:
        required_indexes = sorted(int(scene["scene_index"]) for scene in missing)
        store.update_runtime_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=45,
            status=const.TASK_STATUS_AWAITING_MATERIAL,
            stage="awaiting_material_upload",
            scene_plan=scene_plan,
            missing_scenes=missing,
            resolved_scene_paths={
                str(index): path for index, path in paths_by_scene.items()
            },
            required_upload_scene_indexes=required_indexes,
            material_upload_confirmed=False,
        )
        return {
            "paths": [],
            "missing": missing,
            "status": const.TASK_STATUS_AWAITING_MATERIAL,
        }

    ordered = [paths_by_scene[index] for index in sorted(paths_by_scene)]
    store.patch_task(
        task_id,
        selected_materials=ordered,
        resolved_scene_paths={
            str(index): path for index, path in paths_by_scene.items()
        },
        required_upload_scene_indexes=[],
        missing_scenes=[],
        material_upload_confirmed=False,
    )
    return {"paths": ordered, "missing": [], "status": const.TASK_STATUS_PROCESSING}


def build_visual_duration_shortfall_scenes(
    params: VideoParams,
    video_script: str,
    *,
    scene_plan: list[dict[str, Any]],
    visual_duration: float,
    required_visual_duration: float,
) -> dict[str, Any]:
    """Build missing visual-only scenes without triggering a paid operation."""
    scene_plan = [dict(scene) for scene in scene_plan]
    shortfall = max(0.0, float(required_visual_duration) - float(visual_duration))
    maximum_duration = max(1.0, float(params.video_clip_duration or 1))
    existing_supplemental = [
        scene for scene in scene_plan if scene.get("duration_shortfall_scene")
    ]
    existing_capacity = sum(
        max(0.0, float(scene.get("target_duration") or 0))
        for scene in existing_supplemental
    )
    remaining_shortfall = max(0.0, shortfall - existing_capacity)
    new_count = (
        int(math.ceil(remaining_shortfall / maximum_duration))
        if remaining_shortfall > 0.001
        else 0
    )
    duration_per_scene = (
        math.ceil(remaining_shortfall / new_count * 1000) / 1000
        if new_count
        else 0.0
    )
    context_chunks = _split_script(video_script, max(1, new_count)) or [video_script]
    next_index = max(
        [int(scene.get("scene_index", position)) for position, scene in enumerate(scene_plan)],
        default=-1,
    ) + 1
    supplemental_scenes = []
    for offset in range(new_count):
        context = context_chunks[min(offset, len(context_chunks) - 1)].strip()
        scene_index = next_index + offset
        prompt = (
            f"为旁白补充一段不同构图的写实电影感 B-roll：{context or video_script}。"
            "自然运动，画面稳定，无字幕、无文字、无水印、无品牌标识。"
        )
        supplemental_scenes.append(
            {
                "scene_index": scene_index,
                "text": context,
                "search_query": context or str(params.video_subject or "补充画面"),
                "target_duration": duration_per_scene,
                "supplemental_visual": True,
                "duration_shortfall_scene": True,
                "seedance_prompt": prompt,
                "material_prompt": prompt,
            }
        )
    scene_plan.extend(supplemental_scenes)
    all_supplemental = existing_supplemental + supplemental_scenes
    return {
        "scene_plan": scene_plan,
        "supplemental_scenes": all_supplemental,
        "new_supplemental_scenes": supplemental_scenes,
        "required_upload_scene_indexes": [
            int(scene["scene_index"]) for scene in all_supplemental
        ],
        "shortfall_duration": round(shortfall, 3),
        "supplemental_scene_count": len(all_supplemental),
    }


def pause_for_visual_duration_shortfall(
    task_id: str,
    params: VideoParams,
    video_script: str,
    *,
    visual_duration: float,
    required_visual_duration: float,
) -> dict[str, Any]:
    """Append uploadable visual-only scenes and pause before final composition."""
    store = get_task_store()
    task = store.get_task(task_id) or {}
    shortfall_update = build_visual_duration_shortfall_scenes(
        params,
        video_script,
        scene_plan=task.get("scene_plan") or [],
        visual_duration=visual_duration,
        required_visual_duration=required_visual_duration,
    )
    scene_plan = shortfall_update["scene_plan"]
    supplemental_scenes = shortfall_update["supplemental_scenes"]
    required_indexes = shortfall_update["required_upload_scene_indexes"]
    store.update_runtime_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=45,
        status=const.TASK_STATUS_AWAITING_MATERIAL,
        stage="awaiting_material_upload",
        scene_plan=scene_plan,
        planned_scene_count=len(scene_plan),
        visual_duration=round(float(visual_duration), 3),
        required_visual_duration=round(float(required_visual_duration), 3),
        shortfall_duration=shortfall_update["shortfall_duration"],
        supplemental_scene_count=shortfall_update["supplemental_scene_count"],
        missing_scenes=supplemental_scenes,
        required_upload_scene_indexes=required_indexes,
        material_upload_confirmed=False,
    )
    return {
        "paths": [],
        "missing": supplemental_scenes,
        "status": const.TASK_STATUS_AWAITING_MATERIAL,
    }


def prepare_ai_generated_materials(
    task_id: str,
    params: VideoParams,
    video_script: str,
    audio_duration: float,
    narration_units: list[dict[str, Any]] | None = None,
    director_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成分镜提示词，等待用户逐项上传素材，确认后继续原有合成流程。"""
    store = get_task_store()
    task = store.get_task(task_id) or {}
    task_material_dir = Path(utils.task_dir(task_id)) / "materials"
    task_material_dir.mkdir(parents=True, exist_ok=True)
    aspect_ratio = _aspect_ratio_value(params.video_aspect)

    director_plan = director_plan or task.get("director_plan") or {}
    narration_units = (
        narration_units
        or task.get("narration_units")
        or build_narration_units(
            video_script,
            audio_duration,
            director_plan.get("video_clip_duration") or params.video_clip_duration,
        )
    )
    scene_plan = task.get("scene_plan") or []
    if not scene_plan:
        scene_plan = llm.generate_video_scene_plan(
            video_subject=params.video_subject or "",
            video_script=video_script,
            narration_units=narration_units,
            aspect_ratio=aspect_ratio,
            director_plan=director_plan,
            maximum_duration=params.video_clip_duration,
        )
        if scene_plan:
            # 分镜规划和素材提示词生成是两个明确步骤。后者只产出文本，绝不
            # 提交视频生成任务；用户可以把提示词复制到任意外部工具中使用。
            scene_plan = llm.generate_video_scene_prompts(
                video_subject=params.video_subject or "",
                video_script=video_script,
                scenes=scene_plan,
                aspect_ratio=aspect_ratio,
            )
    if not scene_plan:
        return {
            "paths": [],
            "missing": [],
            "errors": [{"error": "scene planning produced no scenes"}],
            "status": const.TASK_STATUS_FAILED,
        }
    normalized_scene_plan = []
    for position, scene in enumerate(scene_plan):
        scene = dict(scene)
        scene["scene_index"] = int(scene.get("scene_index", position))
        video_prompt = str(scene.get("video_prompt") or "").strip()
        negative_prompt = str(scene.get("negative_prompt") or "").strip()
        if not video_prompt:
            fallback = llm._fallback_video_scene_prompt(scene)
            video_prompt = str(fallback.get("video_prompt") or "").strip()
            negative_prompt = str(fallback.get("negative_prompt") or "").strip()
        scene["video_prompt"] = video_prompt
        scene["negative_prompt"] = negative_prompt
        scene["material_prompt"] = (
            f"{video_prompt}\n\nNegative prompt: {negative_prompt}"
            if negative_prompt
            else video_prompt
        )
        normalized_scene_plan.append(scene)
    scene_plan = normalized_scene_plan
    store.patch_task(
        task_id,
        scene_plan=scene_plan,
        narration_units=narration_units,
        director_plan=director_plan,
        required_visual_duration=video.get_required_video_duration(audio_duration),
        required_scene_count=max(
            1,
            math.ceil(
                video.get_required_video_duration(audio_duration)
                / max(1, int(params.video_clip_duration or 1))
            ),
        ),
        planned_scene_count=len(scene_plan),
    )

    uploaded_paths = {
        str(key): os.path.abspath(str(value))
        for key, value in (task.get("uploaded_scene_paths") or {}).items()
        if str(key).isdigit() and os.path.isfile(str(value))
    }
    generated_paths = {
        str(key): os.path.abspath(str(value))
        for key, value in (task.get("generated_scene_paths") or {}).items()
        if str(key).isdigit() and os.path.isfile(str(value))
    }
    resolved_paths = {
        str(key): os.path.abspath(str(value))
        for key, value in (task.get("resolved_scene_paths") or {}).items()
        if str(key).isdigit() and os.path.isfile(str(value))
    }
    reusable_paths = {**generated_paths, **resolved_paths, **uploaded_paths}
    missing = [
        scene for scene in scene_plan if str(scene["scene_index"]) not in reusable_paths
    ]
    if not task.get("material_upload_confirmed") or missing:
        required_indexes = sorted(int(scene["scene_index"]) for scene in missing)
        store.update_runtime_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=45,
            status=const.TASK_STATUS_AWAITING_MATERIAL,
            stage="awaiting_material_upload",
            scene_plan=scene_plan,
            narration_units=narration_units,
            director_plan=director_plan,
            uploaded_scene_paths=uploaded_paths,
            missing_scenes=missing,
            resolved_scene_paths=resolved_paths,
            required_upload_scene_indexes=required_indexes,
            material_upload_confirmed=False,
        )
        return {
            "paths": [],
            "missing": missing,
            "errors": [],
            "status": const.TASK_STATUS_AWAITING_MATERIAL,
        }

    paths_by_scene: dict[int, str] = {}
    actual_durations = []
    for scene in scene_plan:
        index = int(scene["scene_index"])
        scene_key = str(index)
        source = reusable_paths[scene_key]
        source_provider = (
            "user_upload"
            if scene_key in uploaded_paths
            else "seedance"
            if scene_key in generated_paths
            else "resolved"
        )
        output_suffix = "uploaded" if source_provider == "user_upload" else "confirmed"
        output = str(task_material_dir / f"scene-{index:04d}-{output_suffix}.mp4")
        _prepare_clip(source, output, float(scene.get("target_duration") or 0.1))
        paths_by_scene[index] = output
        try:
            actual_durations.append(video.get_media_duration(output))
        except Exception:
            actual_durations.append(float(scene.get("target_duration") or 0.0))
        _archive_task_scene(task_id, scene, output, provider=source_provider)

    ordered = [paths_by_scene[index] for index in sorted(paths_by_scene)]
    visual_duration = round(sum(actual_durations), 3)
    required_visual_duration = round(float(audio_duration), 3)
    store.update_runtime_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=45,
        status=const.TASK_STATUS_PROCESSING,
        stage="materials",
        selected_materials=ordered,
        selected_material_durations=actual_durations,
        generated_scene_paths=generated_paths,
        missing_scenes=[],
        resolved_scene_paths={str(key): value for key, value in paths_by_scene.items()},
        required_upload_scene_indexes=[],
        material_upload_confirmed=True,
        visual_duration=visual_duration,
        required_visual_duration=required_visual_duration,
        shortfall_duration=max(0.0, required_visual_duration - visual_duration),
    )
    return {"paths": ordered, "missing": [], "status": const.TASK_STATUS_PROCESSING}
