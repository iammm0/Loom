from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.models import const
from app.models.schema import MaterialInfo, VideoParams
from app.services import material_pipeline


def _params():
    return VideoParams(
        video_subject="城市咖啡",
        video_script="第一段。第二段。第三段。第四段。",
        video_sources=["local", "pexels", "pixabay", "coverr"],
        material_strategy="local_first",
        video_clip_duration=3,
    )


def test_configured_sources_use_workspace_online_sources_when_request_omits_them(monkeypatch):
    monkeypatch.setitem(
        material_pipeline.config.app,
        "video_sources",
        ["pexels", "coverr"],
    )
    params = VideoParams(
        video_subject="城市咖啡",
        material_strategy="local_first",
        video_sources=["manual_upload"],
    )

    assert material_pipeline._configured_sources(params) == ["pexels", "coverr"]


def test_scene_plan_preserves_script_order_and_duration():
    scenes = material_pipeline.build_scene_plan(
        "第一段。第二段。第三段。第四段。",
        ["one", "two", "three", "four"],
        audio_duration=12,
        clip_duration=3,
    )

    assert [scene["scene_index"] for scene in scenes] == [0, 1, 2, 3, 4]
    assert [scene["search_query"] for scene in scenes] == [
        "one",
        "two",
        "three",
        "four",
        "four",
    ]
    assert sum(scene["target_duration"] for scene in scenes) == pytest.approx(12.1)


def test_scene_plan_meets_minimum_for_production_failure_script():
    script = (
        "租赁管理还在靠表格、电话和人工催租？"
        "合同多、沟通杂、账单容易遗漏，不仅效率低，还会增加人工成本。"
        "ForentX让租赁业务在线化。"
        "房源、租客和合同统一管理，支持在线签约、在线收租，让租赁流程更清晰、更便捷。"
        "管理人员可以随时查看合同状态和租金收取情况，减少重复录入与人工核对，"
        "把更多时间留给客户服务和业务拓展。"
        "从签约到收租，用ForentX简化日常管理，降低运营成本，让租赁管理更高效。"
    )

    scenes = material_pipeline.build_scene_plan(
        script,
        ["one", "two", "three", "four"],
        audio_duration=37.66,
        clip_duration=6,
    )

    assert len(scenes) == 7
    assert max(scene["target_duration"] for scene in scenes) <= 6
    assert "".join(scene["text"] for scene in scenes) == script


@pytest.mark.parametrize(
    "script",
    [
        "这是一段没有任何标点但必须按音频长度继续拆分的中文长文案内容",
        "Product launch, online signing, online rent collection and AI support.",
        "中文介绍，online signing，在线收租，simple and reliable。",
    ],
)
def test_scene_plan_splits_sparse_punctuation_without_losing_text(script):
    scenes = material_pipeline.build_scene_plan(
        script, [], audio_duration=17.9, clip_duration=3
    )

    assert len(scenes) == 6
    assert "".join(scene["text"] for scene in scenes) == script


def test_scene_plan_adds_visual_only_scenes_for_extremely_short_script():
    scenes = material_pipeline.build_scene_plan(
        "短片", [], audio_duration=11.9, clip_duration=3
    )

    assert len(scenes) == 4
    assert "".join(scene["text"] for scene in scenes) == "短片"
    assert [scene["supplemental_visual"] for scene in scenes] == [False, False, True, True]
    assert all(scene["seedance_prompt"] for scene in scenes)


def test_ordered_materials_persist_scene_count_metrics(tmp_path, monkeypatch):
    state = {}

    class Store:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)

    library = MagicMock()
    library.find_matching_segments.return_value = []
    monkeypatch.setattr(material_pipeline, "get_task_store", Store)
    monkeypatch.setattr(material_pipeline, "get_material_library", lambda: library)
    monkeypatch.setattr(material_pipeline.utils, "task_dir", lambda _: str(tmp_path))
    monkeypatch.setattr(material_pipeline, "_online_candidates", lambda *a, **k: [])

    result = material_pipeline.prepare_ordered_materials(
        "task", _params(), "一段足够长的旁白。", ["one"], 8.9
    )

    assert result["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert state["required_scene_count"] == 3
    assert state["planned_scene_count"] == 3
    assert state["required_visual_duration"] == pytest.approx(9.0)


def test_visual_shortfall_adds_only_required_upload_scenes(monkeypatch):
    state = {
        "scene_plan": [
            {"scene_index": index, "text": f"原分镜{index}", "target_duration": 9.5}
            for index in range(4)
        ],
        "resolved_scene_paths": {str(index): f"scene-{index}.mp4" for index in range(4)},
    }

    class Store:
        def get_task(self, _task_id):
            return dict(state)

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)

    monkeypatch.setattr(material_pipeline, "get_task_store", Store)
    params = VideoParams(
        video_subject="ForentX",
        video_clip_duration=6,
        material_strategy="local_first",
    )

    result = material_pipeline.pause_for_visual_duration_shortfall(
        "task",
        params,
        "完整营销旁白。",
        visual_duration=24.0,
        required_visual_duration=37.76,
    )

    assert result["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert state["required_upload_scene_indexes"] == [4, 5, 6]
    assert state["supplemental_scene_count"] == 3
    assert state["shortfall_duration"] == 13.76
    assert len(state["scene_plan"]) == 7
    assert state["resolved_scene_paths"] == {
        str(index): f"scene-{index}.mp4" for index in range(4)
    }
    assert sum(scene["target_duration"] for scene in result["missing"]) >= 13.76


def test_online_candidates_merge_providers_in_stable_round_robin(monkeypatch):
    calls = []

    def search(source, query, duration, aspect):
        calls.append(source)
        shared = MaterialInfo(
            provider=source, url="https://cdn/shared.mp4?token=x", duration=5
        )
        unique = MaterialInfo(
            provider=source, url=f"https://cdn/{source}.mp4", duration=5
        )
        return [shared, unique]

    monkeypatch.setattr(material_pipeline, "_search_provider", search)

    candidates = material_pipeline._online_candidates(
        "coffee", 3, _params().video_aspect, ["pexels", "pixabay", "coverr"]
    )

    assert set(calls) == {"pexels", "pixabay", "coverr"}
    assert [item.url for item in candidates] == [
        "https://cdn/shared.mp4?token=x",
        "https://cdn/pexels.mp4",
        "https://cdn/pixabay.mp4",
        "https://cdn/coverr.mp4",
    ]


def test_completed_scene_is_archived_with_task_metadata(tmp_path, monkeypatch):
    scene_path = tmp_path / "scene.mp4"
    scene_path.write_bytes(b"video")
    library = MagicMock()
    monkeypatch.setattr(material_pipeline, "get_material_library", lambda: library)

    material_pipeline._archive_task_scene(
        "task-id",
        {
            "scene_index": 3,
            "text": "咖啡师冲泡咖啡",
            "search_query": "coffee brewing",
        },
        str(scene_path),
        provider="pexels",
        source_url="https://example.com/video.mp4",
    )

    library.import_task_scene.assert_called_once_with(
        "task-id",
        3,
        str(scene_path),
        scene_text="咖啡师冲泡咖啡",
        search_query="coffee brewing",
        provider="pexels",
        source_url="https://example.com/video.mp4",
    )


def test_local_match_prevents_online_search(tmp_path, monkeypatch):
    store = MagicMock()
    store.get_task.return_value = {}
    library = MagicMock()
    library.find_matching_segments.side_effect = [
        [
            {
                "segment_id": "local-1",
                "material_id": "material-1",
                "file_path": str(tmp_path / "local-1.mp4"),
            }
        ],
        [
            {
                "segment_id": "local-2",
                "material_id": "material-2",
                "file_path": str(tmp_path / "local-2.mp4"),
            }
        ],
    ]
    monkeypatch.setattr(material_pipeline, "get_task_store", lambda: store)
    monkeypatch.setattr(material_pipeline, "get_material_library", lambda: library)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _: str(tmp_path / "task")
    )
    monkeypatch.setattr(
        material_pipeline, "_prepare_clip", lambda source, output, duration: output
    )
    online = MagicMock()
    monkeypatch.setattr(material_pipeline, "_online_candidates", online)

    result = material_pipeline.prepare_ordered_materials(
        "task", _params(), "只有一段。", ["coffee"], 3
    )

    assert result["status"] == const.TASK_STATUS_PROCESSING
    assert len(result["paths"]) == 2
    online.assert_not_called()


def test_ordered_materials_use_each_library_material_only_once(tmp_path, monkeypatch):
    state = {
        "scene_plan": [
            {
                "scene_index": 0,
                "text": "第一段",
                "search_query": "lease",
                "target_duration": 3,
                "material_prompt": "第一段素材提示词",
            },
            {
                "scene_index": 1,
                "text": "第二段",
                "search_query": "lease",
                "target_duration": 3,
                "material_prompt": "第二段素材提示词",
            },
        ]
    }

    class StatefulStore:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)

    library = MagicMock()
    library.find_matching_segments.side_effect = [
        [
            {
                "segment_id": "segment-1",
                "material_id": "same-material",
                "file_path": str(tmp_path / "source-1.mp4"),
            }
        ],
        [
            {
                "segment_id": "segment-2",
                "material_id": "same-material",
                "file_path": str(tmp_path / "source-2.mp4"),
            }
        ],
    ]
    monkeypatch.setattr(material_pipeline, "get_task_store", StatefulStore)
    monkeypatch.setattr(material_pipeline, "get_material_library", lambda: library)
    monkeypatch.setattr(material_pipeline.utils, "task_dir", lambda _: str(tmp_path))
    monkeypatch.setattr(material_pipeline, "_online_candidates", lambda *a, **k: [])

    def prepare(_source, output, _duration):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"prepared")
        return output

    monkeypatch.setattr(material_pipeline, "_prepare_clip", prepare)

    params = VideoParams(
        video_subject="租约",
        video_sources=["local"],
        material_strategy="local_first",
        video_clip_duration=3,
    )
    result = material_pipeline.prepare_ordered_materials(
        "task", params, "第一段。第二段。", ["lease"], 6
    )

    assert result["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert state["required_upload_scene_indexes"] == [1]
    assert state["scene_plan"][1]["material_prompt"] == "第二段素材提示词"
    assert "没有尚未在本作品使用过" in state["scene_plan"][1][
        "material_missing_reason"
    ]
    second_call = library.find_matching_segments.call_args_list[1]
    assert second_call.kwargs["exclude_material_ids"] == {"same-material"}


def test_prepare_clip_does_not_loop_short_video(tmp_path, monkeypatch):
    source = tmp_path / "short.mp4"
    output = tmp_path / "prepared.mp4"
    source.write_bytes(b"video")
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"prepared")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(material_pipeline.subprocess, "run", run)

    material_pipeline._prepare_clip(str(source), str(output), 5)

    assert output.is_file()
    assert "-stream_loop" not in commands[0]
    assert commands[0][commands[0].index("-i") + 1] == str(source)



def test_partial_online_result_is_preserved_while_missing_scene_is_uploaded(
    tmp_path, monkeypatch
):
    local_source = tmp_path / "local.mp4"
    uploaded_source = tmp_path / "uploaded.mp4"
    local_source.write_bytes(b"local")
    uploaded_source.write_bytes(b"uploaded")
    state = {
        "scene_plan": [
            {
                "scene_index": 0,
                "text": "第一段",
                "search_query": "first",
                "target_duration": 3,
                "seedance_prompt": "first prompt",
            },
            {
                "scene_index": 1,
                "text": "第二段",
                "search_query": "second",
                "target_duration": 3,
                "seedance_prompt": "second prompt",
            },
        ]
    }

    class StatefulStore:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)
            return True

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)

        def is_cancel_requested(self, _task_id):
            return False

    library = MagicMock()
    library.find_matching_segments.side_effect = [
        [{"segment_id": "local-0", "file_path": str(local_source)}],
        [],
    ]
    monkeypatch.setattr(material_pipeline, "get_task_store", StatefulStore)
    monkeypatch.setattr(material_pipeline, "get_material_library", lambda: library)
    monkeypatch.setattr(material_pipeline.utils, "task_dir", lambda _: str(tmp_path))
    monkeypatch.setattr(material_pipeline, "_online_candidates", lambda *a, **k: [])
    monkeypatch.setattr(material_pipeline, "_archive_task_scene", lambda *a, **k: None)

    def prepare(_source, output, _duration):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"prepared")
        return output

    monkeypatch.setattr(material_pipeline, "_prepare_clip", prepare)

    waiting = material_pipeline.prepare_ordered_materials(
        "task", _params(), "第一段。第二段。", ["first", "second"], 6
    )

    assert waiting["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert state["required_upload_scene_indexes"] == [1]
    assert Path(state["resolved_scene_paths"]["0"]).name == "scene-0000-local.mp4"

    state["uploaded_scene_paths"] = {"1": str(uploaded_source)}
    state["material_upload_confirmed"] = True
    completed = material_pipeline.prepare_ordered_materials(
        "task", _params(), "第一段。第二段。", ["first", "second"], 6
    )

    assert completed["status"] == const.TASK_STATUS_PROCESSING
    assert [Path(path).name for path in completed["paths"]] == [
        "scene-0000-local.mp4",
        "scene-0001-uploaded.mp4",
    ]
    assert library.find_matching_segments.call_count == 2


def test_missing_material_waits_for_scene_upload_without_seedance_call(
    tmp_path, monkeypatch
):
    store = MagicMock()
    store.get_task.return_value = {}
    library = MagicMock()
    library.find_matching_segments.return_value = []
    monkeypatch.setattr(material_pipeline, "get_task_store", lambda: store)
    monkeypatch.setattr(material_pipeline, "get_material_library", lambda: library)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _: str(tmp_path / "task")
    )
    monkeypatch.setattr(
        material_pipeline, "_online_candidates", lambda *args, **kwargs: []
    )
    result = material_pipeline.prepare_ordered_materials(
        "task", _params(), "没有素材。", ["missing"], 3
    )

    assert result["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert not hasattr(material_pipeline, "seedance")
    update = store.update_runtime_task.call_args.kwargs
    assert update["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert update["stage"] == "awaiting_material_upload"
    assert update["required_upload_scene_indexes"] == [0, 1]


@pytest.mark.skip(reason="Seedance material generation workflow was removed")
def test_seedance_approval_generates_at_most_three_clips(tmp_path, monkeypatch):
    scenes = material_pipeline.build_scene_plan(
        "一。二。三。四。", ["1", "2", "3", "4"], 12, 3
    )
    store = MagicMock()
    store.get_task.return_value = {
        "scene_plan": scenes,
        "seedance_approved": True,
        "approved_seedance_prompts": {},
    }
    library = MagicMock()
    library.find_matching_segments.return_value = []
    monkeypatch.setattr(material_pipeline, "get_task_store", lambda: store)
    monkeypatch.setattr(material_pipeline, "get_material_library", lambda: library)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _: str(tmp_path / "task")
    )
    monkeypatch.setattr(
        material_pipeline, "_online_candidates", lambda *args, **kwargs: []
    )

    def generate(**kwargs):
        Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kwargs["output_path"]).write_bytes(b"video")
        return kwargs["output_path"], f"provider-{Path(kwargs['output_path']).stem}"

    generated = MagicMock(side_effect=generate)
    monkeypatch.setattr(
        material_pipeline.seedance,
        "create_generation_task",
        lambda **kwargs: f"created-{kwargs['prompt'][:1]}",
    )
    monkeypatch.setattr(material_pipeline.seedance, "generate_clip", generated)
    monkeypatch.setitem(material_pipeline.config.seedance, "max_clips_per_task", 3)

    result = material_pipeline.prepare_ordered_materials(
        "task", _params(), "一。二。三。四。", ["1", "2", "3", "4"], 12
    )

    assert generated.call_count == 3
    assert result["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert len(result["missing"]) == 1


@pytest.mark.skip(reason="replaced by prompt-and-upload material workflow")
def test_ai_generated_materials_generate_all_scenes_in_order(tmp_path, monkeypatch):
    params = VideoParams(
        video_subject="城市咖啡",
        video_script="第一段。第二段。",
        material_strategy="ai_generated",
        video_clip_duration=3,
    )
    store = MagicMock()
    store.get_task.return_value = {}
    monkeypatch.setattr(material_pipeline, "get_task_store", lambda: store)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _: str(tmp_path / "task")
    )
    prompt_aspects = []

    def generate_prompts(**kwargs):
        prompt_aspects.append(kwargs["aspect_ratio"])
        return [
            {
                "scene_index": 0,
                "unit_start": 0,
                "unit_end": 1,
                "text": "第一段。",
                "target_duration": 3,
                "video_prompt": "prompt 0",
                "negative_prompt": "no text",
            },
            {
                "scene_index": 1,
                "unit_start": 2,
                "unit_end": 3,
                "text": "第二段。",
                "target_duration": 3,
                "video_prompt": "prompt 1",
                "negative_prompt": "no text",
            },
        ]

    def generate_clip(**kwargs):
        provider_id = f"provider-{kwargs['prompt'].split()[1]}"
        kwargs["task_created_callback"](provider_id)
        Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kwargs["output_path"]).write_bytes(b"video")
        return (
            kwargs["output_path"],
            provider_id,
            {
                "provider_task_id": provider_id,
                "status": "completed",
                "billable": True,
            },
        )

    monkeypatch.setattr(
        material_pipeline.llm, "generate_video_scene_plan", generate_prompts
    )
    monkeypatch.setattr(material_pipeline.video, "get_media_duration", lambda _: 3.2)
    generate = MagicMock(side_effect=generate_clip)
    monkeypatch.setattr(material_pipeline.seedance, "generate_clip_detailed", generate)

    result = material_pipeline.prepare_ai_generated_materials(
        "task",
        params,
        "第一段。第二段。",
        audio_duration=6,
    )

    assert result["status"] == const.TASK_STATUS_PROCESSING
    assert [Path(path).name for path in result["paths"]] == [
        "scene-0000-ai.mp4",
        "scene-0001-ai.mp4",
    ]
    assert prompt_aspects == ["9:16"]
    assert generate.call_count == 2
    assert [call.kwargs["aspect_ratio"] for call in generate.call_args_list] == [
        "9:16",
        "9:16",
    ]
    assert store.patch_task.call_args.kwargs["selected_materials"] == result["paths"]


@pytest.mark.skip(reason="replaced by prompt-and-upload material workflow")
def test_ai_generated_materials_stop_before_next_paid_scene_after_cancel(
    tmp_path, monkeypatch
):
    state = {}

    class StatefulStore:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)
            return True

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)
            return True

    monkeypatch.setattr(material_pipeline, "get_task_store", StatefulStore)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _=None: str(tmp_path)
    )
    monkeypatch.setattr(
        material_pipeline.llm,
        "generate_video_scene_plan",
        lambda **_: [
            {
                "scene_index": index,
                "text": f"旁白 {index}",
                "target_duration": 3,
                "video_prompt": f"scene {index}",
                "negative_prompt": "",
            }
            for index in range(2)
        ],
    )
    monkeypatch.setattr(
        material_pipeline,
        "_reserve_seedance_scene",
        lambda *args, **kwargs: (True, {}),
    )
    monkeypatch.setattr(
        material_pipeline.generation_report,
        "record_paid_operation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        material_pipeline.generation_report,
        "record_seedance_scene",
        lambda *args, **kwargs: None,
    )
    generated_indexes = []

    def generated(**kwargs):
        index = int(Path(kwargs["output_path"]).stem.split("-")[1])
        generated_indexes.append(index)
        provider_id = f"provider-{index}"
        kwargs["task_created_callback"](provider_id)
        Path(kwargs["output_path"]).write_bytes(b"video")
        state.update(
            cancel_requested=True,
            status=const.TASK_STATUS_CANCELLATION_REQUESTED,
        )
        return (
            kwargs["output_path"],
            provider_id,
            {
                "provider_task_id": provider_id,
                "status": "completed",
                "billable": True,
            },
        )

    monkeypatch.setattr(material_pipeline.seedance, "generate_clip_detailed", generated)

    result = material_pipeline.prepare_ai_generated_materials(
        "cancel-task",
        VideoParams(video_subject="取消测试", video_clip_duration=3),
        "第一段。第二段。",
        audio_duration=6,
    )

    assert generated_indexes == [0]
    assert result["status"] == const.TASK_STATUS_CANCELLATION_REQUESTED
    assert [scene["scene_index"] for scene in result["missing"]] == [1]


@pytest.mark.skip(reason="replaced by prompt-and-upload material workflow")
def test_ai_generated_materials_report_generation_errors(tmp_path, monkeypatch):
    params = VideoParams(
        video_subject="城市咖啡",
        video_script="第一段。",
        material_strategy="ai_generated",
        video_clip_duration=3,
    )
    store = MagicMock()
    store.get_task.return_value = {}
    monkeypatch.setattr(material_pipeline, "get_task_store", lambda: store)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _: str(tmp_path / "task")
    )
    monkeypatch.setattr(
        material_pipeline.llm,
        "generate_video_scene_plan",
        lambda **kwargs: [
            {
                "scene_index": 0,
                "unit_start": 0,
                "unit_end": len(kwargs["narration_units"]) - 1,
                "text": "第一段。",
                "target_duration": 3,
                "video_prompt": "prompt 0",
                "negative_prompt": "",
            }
        ],
    )
    monkeypatch.setattr(
        material_pipeline.seedance,
        "generate_clip_detailed",
        MagicMock(side_effect=RuntimeError("provider failed")),
    )

    result = material_pipeline.prepare_ai_generated_materials(
        "task",
        params,
        "第一段。",
        audio_duration=3,
    )

    assert result["status"] == const.TASK_STATUS_FAILED
    assert result["paths"] == []
    assert result["errors"][0]["scene_index"] == 0
    assert "provider failed" in result["errors"][0]["error"]


@pytest.mark.skip(reason="replaced by prompt-and-upload material workflow")
def test_ai_generated_materials_replace_confirmed_failed_provider_task(
    tmp_path, monkeypatch
):
    state = {
        "scene_plan": [
            {
                "scene_index": 0,
                "unit_start": 0,
                "unit_end": 0,
                "text": "旁白",
                "target_duration": 5.3,
                "video_prompt": "replacement scene",
                "duration_margin_applied": True,
            }
        ],
        "seedance_provider_task_ids": {"0": "failed-provider"},
    }

    class StatefulStore:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)
            return True

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)
            return True

    monkeypatch.setattr(material_pipeline, "get_task_store", StatefulStore)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _=None: str(tmp_path)
    )
    monkeypatch.setattr(
        material_pipeline.seedance,
        "get_generation_task",
        lambda provider_id: (
            {"status": "failed", "error": "provider rejected generation"}
            if provider_id == "failed-provider"
            else {"status": "succeeded", "usage": {"completion_tokens": 108900}}
        ),
    )

    def generated(**kwargs):
        assert kwargs["provider_task_id"] == ""
        kwargs["task_created_callback"]("replacement-provider")
        Path(kwargs["output_path"]).write_bytes(b"video")
        return (
            kwargs["output_path"],
            "replacement-provider",
            {
                "provider_task_id": "replacement-provider",
                "status": "completed",
                "billable": True,
                "tokens": 108900,
                "estimated_cost_cny": 5.0094,
                "calculation_method": "api_usage",
            },
        )

    monkeypatch.setattr(material_pipeline.seedance, "generate_clip_detailed", generated)
    monkeypatch.setattr(material_pipeline.video, "get_media_duration", lambda _: 5.3)

    result = material_pipeline.prepare_ai_generated_materials(
        "task",
        VideoParams(video_subject="咖啡", video_clip_duration=5),
        "旁白",
        audio_duration=5,
    )

    assert result["status"] == const.TASK_STATUS_PROCESSING
    assert state["seedance_provider_task_ids"] == {"0": "replacement-provider"}
    assert state["seedance_failed_provider_task_ids"] == {"0": ["failed-provider"]}


def test_narration_units_split_long_sentence_by_audio_pacing():
    units = material_pipeline.build_narration_units(
        "这是一段没有任何标点但是需要覆盖完整三十秒配音的长文案内容",
        audio_duration=30,
        preferred_scene_duration=3,
    )

    assert len(units) >= 10
    assert units[0]["start"] == 0
    assert units[-1]["end"] == 30
    assert "".join(unit["text"] for unit in units).startswith("这是一段")


@pytest.mark.skip(reason="replaced by prompt-and-upload material workflow")
def test_ai_generated_materials_wait_when_real_duration_is_short(tmp_path, monkeypatch):
    params = VideoParams(video_subject="城市咖啡", video_clip_duration=3)
    store = MagicMock()
    store.get_task.return_value = {}
    monkeypatch.setattr(material_pipeline, "get_task_store", lambda: store)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _: str(tmp_path / "task")
    )
    monkeypatch.setattr(
        material_pipeline.llm,
        "generate_video_scene_plan",
        lambda **kwargs: [
            {
                "scene_index": 0,
                "unit_start": 0,
                "unit_end": len(kwargs["narration_units"]) - 1,
                "text": "旁白",
                "target_duration": 6,
                "video_prompt": "coffee closing shot",
                "negative_prompt": "text",
            }
        ],
    )
    monkeypatch.setitem(
        material_pipeline.config.seedance, "auto_approve_within_duration", False
    )

    def generated(**kwargs):
        kwargs["task_created_callback"]("p")
        Path(kwargs["output_path"]).write_bytes(b"video")
        return (
            kwargs["output_path"],
            "p",
            {
                "provider_task_id": "p",
                "status": "completed",
                "billable": True,
            },
        )

    monkeypatch.setattr(material_pipeline.seedance, "generate_clip_detailed", generated)
    monkeypatch.setattr(material_pipeline.video, "get_media_duration", lambda _: 2.0)

    result = material_pipeline.prepare_ai_generated_materials(
        "task", params, "旁白", audio_duration=6
    )

    assert result["status"] == const.TASK_STATUS_AWAITING_APPROVAL
    waiting = store.update_runtime_task.call_args.kwargs
    assert waiting["stage"] == "awaiting_supplemental_scenes"
    assert waiting["shortfall_duration"] == 4.1
    assert waiting["supplemental_scenes"]


@pytest.mark.skip(reason="replaced by prompt-and-upload material workflow")
def test_ai_generated_materials_auto_approve_supplement_within_30_seconds(
    tmp_path, monkeypatch
):
    state = {}

    class StatefulStore:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)
            return True

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)
            return True

    monkeypatch.setattr(material_pipeline, "get_task_store", StatefulStore)
    monkeypatch.setattr(
        material_pipeline.utils, "task_dir", lambda _=None: str(tmp_path)
    )
    monkeypatch.setitem(
        material_pipeline.config.seedance, "auto_approve_within_duration", True
    )
    monkeypatch.setitem(
        material_pipeline.config.seedance, "max_auto_approved_duration", 30
    )
    monkeypatch.setattr(
        material_pipeline.llm,
        "generate_video_scene_plan",
        lambda **kwargs: [
            {
                "scene_index": 0,
                "unit_start": 0,
                "unit_end": len(kwargs["narration_units"]) - 1,
                "text": "旁白",
                "target_duration": 6,
                "video_prompt": "opening",
                "negative_prompt": "text",
            }
        ],
    )
    generated_indexes = []

    def generated(**kwargs):
        index = int(Path(kwargs["output_path"]).stem.split("-")[1])
        generated_indexes.append(index)
        provider_id = f"provider-{index}"
        kwargs["task_created_callback"](provider_id)
        Path(kwargs["output_path"]).write_bytes(b"video")
        return (
            kwargs["output_path"],
            provider_id,
            {
                "provider_task_id": provider_id,
                "status": "completed",
                "billable": True,
            },
        )

    monkeypatch.setattr(material_pipeline.seedance, "generate_clip_detailed", generated)
    monkeypatch.setattr(
        material_pipeline.seedance,
        "get_generation_task",
        lambda _: {
            "status": "succeeded",
            "usage": {"completion_tokens": 108900},
        },
    )
    monkeypatch.setattr(
        material_pipeline.video,
        "get_media_duration",
        lambda path: 2.0 if "scene-0000" in path else 4.5,
    )

    result = material_pipeline.prepare_ai_generated_materials(
        "task",
        VideoParams(video_subject="咖啡", video_clip_duration=4),
        "旁白",
        audio_duration=6,
    )

    assert result["status"] == const.TASK_STATUS_PROCESSING, result
    assert generated_indexes == [0, 1]
    assert state["automatic_supplement_round"] == 1
    assert state["supplemental_scenes"] == []


@pytest.mark.skip(reason="replaced by prompt-and-upload material workflow")
def test_approved_supplement_generates_only_new_scene(tmp_path, monkeypatch):
    initial = tmp_path / "scene-0000-ai.mp4"
    initial.write_bytes(b"initial")
    scene_plan = [
        {
            "scene_index": 0,
            "target_duration": 3.3,
            "video_prompt": "opening",
            "duration_margin_applied": True,
        }
    ]
    supplemental = [
        {
            "scene_index": 1,
            "target_duration": 3.0,
            "video_prompt": "new closing shot",
            "negative_prompt": "text",
        }
    ]
    store = MagicMock()
    store.get_task.return_value = {
        "scene_plan": scene_plan,
        "generated_scene_paths": {"0": str(initial)},
        "supplemental_scenes": supplemental,
        "supplemental_scenes_approved": True,
    }
    monkeypatch.setattr(material_pipeline, "get_task_store", lambda: store)
    monkeypatch.setattr(material_pipeline.utils, "task_dir", lambda _: str(tmp_path))
    generated_calls = []

    def generated(**kwargs):
        generated_calls.append(kwargs["prompt"])
        kwargs["task_created_callback"]("p1")
        Path(kwargs["output_path"]).write_bytes(b"new")
        return (
            kwargs["output_path"],
            "p1",
            {
                "provider_task_id": "p1",
                "status": "completed",
                "billable": True,
            },
        )

    monkeypatch.setattr(material_pipeline.seedance, "generate_clip_detailed", generated)
    monkeypatch.setattr(material_pipeline.video, "get_media_duration", lambda _: 3.3)

    result = material_pipeline.prepare_ai_generated_materials(
        "task",
        VideoParams(video_subject="咖啡", video_clip_duration=3),
        "完整旁白",
        audio_duration=6,
    )

    assert result["status"] == const.TASK_STATUS_PROCESSING
    assert len(result["paths"]) == 2
    assert generated_calls == ["new closing shot\n\nNegative prompt: text"]
    final_patch = store.patch_task.call_args.kwargs
    assert final_patch["supplemental_scenes"] == []
    assert final_patch["supplemental_scenes_approved"] is False


def test_manual_scene_workflow_generates_prompts_then_waits_for_uploads(
    tmp_path, monkeypatch
):
    state = {}

    class StatefulStore:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)
            return True

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)

    monkeypatch.setattr(material_pipeline, "get_task_store", StatefulStore)
    monkeypatch.setattr(material_pipeline.utils, "task_dir", lambda _: str(tmp_path))
    monkeypatch.setattr(
        material_pipeline.llm,
        "generate_video_scene_plan",
        lambda **_: [
            {"scene_index": 0, "text": "第一段", "target_duration": 3},
            {"scene_index": 1, "text": "第二段", "target_duration": 3},
        ],
    )
    monkeypatch.setattr(
        material_pipeline.llm,
        "generate_video_scene_prompts",
        lambda **kwargs: [
            {
                **scene,
                "video_prompt": f"prompt {scene['scene_index']}",
                "negative_prompt": "text, watermark",
            }
            for scene in kwargs["scenes"]
        ],
    )
    result = material_pipeline.prepare_ai_generated_materials(
        "task",
        VideoParams(video_subject="咖啡", video_clip_duration=3),
        "第一段。第二段。",
        audio_duration=6,
        narration_units=[{"text": "旁白", "start": 0, "end": 6}],
    )

    assert result["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert [scene["scene_index"] for scene in result["missing"]] == [0, 1]
    assert state["stage"] == "awaiting_material_upload"
    assert state["scene_plan"][0]["material_prompt"] == (
        "prompt 0\n\nNegative prompt: text, watermark"
    )
    assert not hasattr(material_pipeline, "seedance")


def test_manual_scene_workflow_uses_confirmed_uploads_in_scene_order(
    tmp_path, monkeypatch
):
    first = tmp_path / "first.mov"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    state = {
        "scene_plan": [
            {
                "scene_index": 0,
                "text": "第一段",
                "target_duration": 2,
                "video_prompt": "first prompt",
            },
            {
                "scene_index": 1,
                "text": "第二段",
                "target_duration": 4,
                "video_prompt": "second prompt",
            },
        ],
        "uploaded_scene_paths": {"0": str(first), "1": str(second)},
        "material_upload_confirmed": True,
    }

    class StatefulStore:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)
            return True

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)

    monkeypatch.setattr(material_pipeline, "get_task_store", StatefulStore)
    monkeypatch.setattr(material_pipeline.utils, "task_dir", lambda _: str(tmp_path))

    prepared_sources = []

    def prepare(source, output, duration):
        prepared_sources.append((source, duration))
        Path(output).write_bytes(b"prepared")
        return output

    monkeypatch.setattr(material_pipeline, "_prepare_clip", prepare)
    monkeypatch.setattr(material_pipeline, "_archive_task_scene", lambda *a, **k: None)
    monkeypatch.setattr(
        material_pipeline.video,
        "get_media_duration",
        lambda path: 2 if "0000" in Path(path).name else 4,
    )
    result = material_pipeline.prepare_ai_generated_materials(
        "task",
        VideoParams(video_subject="咖啡", video_clip_duration=4),
        "第一段。第二段。",
        audio_duration=6,
        narration_units=[{"text": "旁白", "start": 0, "end": 6}],
    )

    assert result["status"] == const.TASK_STATUS_PROCESSING
    assert [Path(path).name for path in result["paths"]] == [
        "scene-0000-uploaded.mp4",
        "scene-0001-uploaded.mp4",
    ]
    assert prepared_sources == [(str(first), 2.0), (str(second), 4.0)]
    assert state["selected_material_durations"] == [2, 4]
    assert not hasattr(material_pipeline, "seedance")


def test_manual_scene_workflow_combines_existing_seedance_clip_and_upload(
    tmp_path, monkeypatch
):
    generated = tmp_path / "scene-0000-ai.mp4"
    uploaded = tmp_path / "scene-0001-upload.mp4"
    generated.write_bytes(b"generated")
    uploaded.write_bytes(b"uploaded")
    state = {
        "scene_plan": [
            {"scene_index": 0, "target_duration": 2, "video_prompt": "existing"},
            {"scene_index": 1, "target_duration": 4, "video_prompt": "replacement"},
        ],
        "generated_scene_paths": {"0": str(generated)},
        "uploaded_scene_paths": {"1": str(uploaded)},
        "required_upload_scene_indexes": [1],
        "material_upload_confirmed": True,
    }

    class StatefulStore:
        def get_task(self, _task_id):
            return dict(state)

        def patch_task(self, _task_id, **fields):
            state.update(fields)
            return True

        def update_runtime_task(self, _task_id, **fields):
            state.update(fields)

    monkeypatch.setattr(material_pipeline, "get_task_store", StatefulStore)
    monkeypatch.setattr(material_pipeline.utils, "task_dir", lambda _: str(tmp_path))
    prepared_sources = []
    archived_providers = []

    def prepare(source, output, duration):
        prepared_sources.append((source, duration))
        Path(output).write_bytes(b"prepared")
        return output

    monkeypatch.setattr(material_pipeline, "_prepare_clip", prepare)
    monkeypatch.setattr(
        material_pipeline,
        "_archive_task_scene",
        lambda *args, **kwargs: archived_providers.append(kwargs["provider"]),
    )
    monkeypatch.setattr(
        material_pipeline.video,
        "get_media_duration",
        lambda path: 2 if "0000" in Path(path).name else 4,
    )

    result = material_pipeline.prepare_ai_generated_materials(
        "task",
        VideoParams(video_subject="咖啡", video_clip_duration=4),
        "第一段。第二段。",
        audio_duration=6,
    )

    assert result["status"] == const.TASK_STATUS_PROCESSING
    assert prepared_sources == [(str(generated.resolve()), 2.0), (str(uploaded.resolve()), 4.0)]
    assert archived_providers == ["seedance", "user_upload"]
    assert state["generated_scene_paths"] == {"0": str(generated.resolve())}
    assert state["required_upload_scene_indexes"] == []
