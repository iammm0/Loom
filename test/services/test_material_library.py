from io import BytesIO
from pathlib import Path

from PIL import Image

from app.services import material_library
from app.services.material_library import MaterialLibrary
from app.services.task_store import TaskStore


def _image_bytes(color="red"):
    output = BytesIO()
    Image.new("RGB", (640, 480), color=color).save(output, "PNG")
    output.seek(0)
    return output


def _library(tmp_path, monkeypatch):
    storage = tmp_path / "storage"

    def storage_dir(sub_dir="", create=False):
        path = storage / sub_dir if sub_dir else storage
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return str(path)

    monkeypatch.setattr(material_library.utils, "storage_dir", storage_dir)
    return MaterialLibrary(TaskStore(storage / "app.db"))


def test_manual_image_upload_is_ready_and_creates_thumbnail(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)

    saved = library.save_upload(
        "city.png", _image_bytes(), manual_tags="城市, 夜景", ai_tagging=False
    )

    assert saved["status"] == material_library.MATERIAL_STATUS_READY
    assert saved["segments"][0]["manual_tags"] == ["城市", "夜景"]
    assert Path(saved["segments"][0]["thumbnail_path"]).is_file()


def test_ai_upload_is_ready_without_confirmation_and_preserves_tags(
    tmp_path, monkeypatch
):
    library = _library(tmp_path, monkeypatch)
    monkeypatch.setattr(
        material_library.vision,
        "analyze_images",
        lambda _: {
            "description_zh": "夜晚的城市街道",
            "tags_zh": ["城市", "街道"],
            "tags_en": ["city", "street"],
        },
    )

    saved = library.save_upload(
        "night.png", _image_bytes("blue"), manual_tags=["品牌素材"], ai_tagging=True
    )

    segment = saved["segments"][0]
    assert saved["status"] == material_library.MATERIAL_STATUS_READY
    assert segment["manual_tags"] == ["品牌素材"]
    assert segment["ai_tags_zh"] == ["城市", "街道"]
    assert segment["tags_en"] == ["city", "street"]
    assert saved["tags"] == ["品牌素材", "城市", "街道"]


def test_ai_upload_reports_progress(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    monkeypatch.setattr(
        material_library.vision,
        "analyze_images",
        lambda _: {
            "description_zh": "夜晚的城市街道",
            "tags_zh": ["城市"],
            "tags_en": ["city"],
        },
    )
    progress = []

    library.save_upload(
        "night.png",
        _image_bytes("blue"),
        ai_tagging=True,
        progress_callback=lambda done, total, message: progress.append(
            (done, total, message)
        ),
    )

    assert progress[0][0] == 0
    assert progress[-1][0] == progress[-1][1] == 1


def test_duplicate_upload_merges_manual_tags_without_new_file(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    first = library.save_upload("same.png", _image_bytes(), manual_tags=["一"])

    duplicate = library.save_upload("copy.png", _image_bytes(), manual_tags=["二"])

    assert duplicate["duplicate"] is True
    assert duplicate["material_id"] == first["material_id"]
    assert duplicate["segments"][0]["manual_tags"] == ["一", "二"]
    materials, total = library.list()
    assert total == 1
    assert len(materials) == 1


def test_confirm_and_match_ready_segment(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    saved = library.save_upload("coffee.png", _image_bytes(), manual_tags=None)
    segment_id = saved["segments"][0]["segment_id"]
    library.update_segment(
        segment_id,
        manual_tags=["咖啡", "coffee shop"],
        description_zh="咖啡店里正在制作咖啡",
    )
    library.confirm(saved["material_id"])

    matches = library.find_matching_segments("coffee shop 咖啡", limit=2)

    assert [item["segment_id"] for item in matches] == [segment_id]


def test_matching_excludes_materials_already_used_in_same_video(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    first = library.save_upload("first.png", _image_bytes("red"), manual_tags=["租约"])
    second = library.save_upload(
        "second.png", _image_bytes("blue"), manual_tags=["租约"]
    )

    matches = library.find_matching_segments(
        "租约", exclude_material_ids=[first["material_id"]]
    )

    assert [item["material_id"] for item in matches] == [second["material_id"]]


def test_matching_ignores_local_task_derivative_materials(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    original = library.save_upload(
        "original.png", _image_bytes("red"), manual_tags=["租约"]
    )
    derived_path = tmp_path / "derived.png"
    derived_path.write_bytes(_image_bytes("blue").getvalue())
    derived = library.import_task_scene(
        "task-id",
        0,
        str(derived_path),
        search_query="租约",
        provider="local",
    )

    matches = library.find_matching_segments("租约")

    assert original["material_id"] in {item["material_id"] for item in matches}
    assert derived["material_id"] not in {item["material_id"] for item in matches}


def test_matching_excludes_material_archived_by_current_task(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    archived_path = tmp_path / "generated.png"
    archived_path.write_bytes(_image_bytes("green").getvalue())
    archived = library.import_task_scene(
        "current-task",
        0,
        str(archived_path),
        search_query="租约",
        provider="seedance",
    )

    matches = library.find_matching_segments(
        "租约", exclude_task_id="current-task"
    )

    assert archived["material_id"] not in {item["material_id"] for item in matches}


def test_retag_reports_progress(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    saved = library.save_upload("coffee.png", _image_bytes(), manual_tags=["咖啡"])
    monkeypatch.setattr(
        material_library.vision,
        "analyze_images",
        lambda _: {
            "description_zh": "咖啡店",
            "tags_zh": ["咖啡"],
            "tags_en": ["coffee"],
        },
    )
    progress = []

    library.retag(
        saved["material_id"],
        progress_callback=lambda done, total, message: progress.append(
            (done, total, message)
        ),
    )

    assert progress[0][0] == 0
    assert progress[-1][0] == progress[-1][1] == 1


def test_import_legacy_registers_file_without_moving_it(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    legacy_dir = Path(material_library.utils.storage_dir("local_videos", create=True))
    legacy_file = legacy_dir / "old.png"
    legacy_file.write_bytes(_image_bytes().getvalue())

    imported = library.import_legacy()

    assert imported == 1
    materials, total = library.list()
    assert total == 1
    assert materials[0]["original_path"] == str(legacy_file)
    assert legacy_file.is_file()


def test_scene_ranges_split_long_ranges_and_merge_short_tail(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)

    class Result:
        stderr = "pts_time:4.0\npts_time:13.0\n"
        returncode = 0

    monkeypatch.setattr(
        material_library.subprocess, "run", lambda *args, **kwargs: Result()
    )

    ranges = library._scene_ranges("video.mp4", 25.0)

    assert ranges[0] == (0.0, 4.0)
    assert ranges[-1][1] == 25.0
    assert all(end > start for start, end in ranges)


def test_task_scene_is_copied_tagged_and_linked(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    scene = tmp_path / "scene.png"
    scene.write_bytes(_image_bytes("green").getvalue())

    saved = library.import_task_scene(
        "task-12345678",
        2,
        str(scene),
        scene_text="城市公园里的绿色植物",
        search_query="城市 公园",
        provider="pexels",
        source_url="https://example.com/scene.mp4",
    )

    assert saved["original_path"] != str(scene)
    assert Path(saved["original_path"]).is_file()
    assert saved["segments"][0]["description_zh"] == "城市公园里的绿色植物"
    assert "任务分镜" in saved["segments"][0]["manual_tags"]
    assert saved["task_scenes"][0]["task_id"] == "task-12345678"
    assert saved["task_scenes"][0]["scene_index"] == 2
    assert saved["task_scenes"][0]["provider"] == "pexels"


def test_duplicate_task_scenes_share_material_but_keep_task_links(
    tmp_path, monkeypatch
):
    library = _library(tmp_path, monkeypatch)
    scene = tmp_path / "same.png"
    scene.write_bytes(_image_bytes("purple").getvalue())

    first = library.import_task_scene("task-a", 0, str(scene), provider="local")
    second = library.import_task_scene("task-b", 1, str(scene), provider="local")

    assert first["material_id"] == second["material_id"]
    refreshed = library.get(first["material_id"])
    assert {
        (item["task_id"], item["scene_index"]) for item in refreshed["task_scenes"]
    } == {
        ("task-a", 0),
        ("task-b", 1),
    }


def test_historical_task_scenes_are_backfilled(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    scene = tmp_path / "historical.png"
    scene.write_bytes(_image_bytes("yellow").getvalue())
    library.store.enqueue(
        {
            "video_subject": "历史任务",
            "video_source": "pexels",
            "material_strategy": "local_first",
        },
        task_id="historical-task",
    )
    library.store.patch_task(
        "historical-task",
        scene_plan=[{"scene_index": 0, "text": "历史分镜", "search_query": "history"}],
        selected_materials=[str(scene)],
    )

    imported = library.import_task_history()

    assert imported == 1
    assert library.get_task_scene("historical-task", 0)["provider"] == "pexels"
    assert library.import_task_history() == 0


def test_tag_management_aggregates_renames_and_deletes(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    saved = library.save_upload(
        "coffee.png", _image_bytes(), manual_tags=["咖啡", "室内"]
    )

    tags = {item["name"]: item for item in library.list_tags()}
    assert tags["咖啡"]["material_count"] == 1
    matches, total = library.list(query="咖啡")
    assert total == 1
    assert matches[0]["material_id"] == saved["material_id"]
    assert library.rename_tag("咖啡", "咖啡馆") == 1
    assert "咖啡馆" in library.get(saved["material_id"])["segments"][0]["manual_tags"]
    assert library.delete_tag("室内") == 1
    assert "室内" not in library.get(saved["material_id"])["segments"][0]["manual_tags"]


def test_one_tag_can_manage_multiple_material_relations(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    first = library.save_upload("first.png", _image_bytes("red"))
    second = library.save_upload("second.png", _image_bytes("blue"))

    assert (
        library.set_tag_materials(
            "产品展示",
            [first["material_id"], second["material_id"]],
        )
        == 2
    )

    tag = {item["name"]: item for item in library.list_tags()}["产品展示"]
    assert tag["material_count"] == 2
    assert set(tag["material_ids"]) == {
        first["material_id"],
        second["material_id"],
    }
    assert {item["name"] for item in tag["materials"]} == {
        "first.png",
        "second.png",
    }

    library.set_tag_materials("产品展示", [second["material_id"]])

    tag = {item["name"]: item for item in library.list_tags()}["产品展示"]
    assert tag["material_ids"] == [second["material_id"]]
    assert "产品展示" not in library.get(first["material_id"])["tags"]
    assert "产品展示" in library.get(second["material_id"])["tags"]


def test_one_material_can_replace_multiple_tags_at_once(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    saved = library.save_upload("product.png", _image_bytes())

    updated = library.update_material(
        saved["material_id"],
        tags=["产品", "室内", "品牌"],
        description_zh="产品在室内展示",
    )

    assert updated["tags"] == ["产品", "室内", "品牌"]
    assert updated["description_zh"] == "产品在室内展示"
    assert updated["segments"][0]["manual_tags"] == ["产品", "室内", "品牌"]


def test_review_materials_are_migrated_to_ready_on_startup(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    saved = library.save_upload("legacy.png", _image_bytes())
    with library.store.connection() as connection:
        connection.execute(
            "UPDATE materials SET status = 'review' WHERE material_id = ?",
            (saved["material_id"],),
        )
        connection.execute(
            "UPDATE material_segments SET status = 'review' WHERE material_id = ?",
            (saved["material_id"],),
        )

    migrated = MaterialLibrary(library.store).get(saved["material_id"])

    assert migrated["status"] == material_library.MATERIAL_STATUS_READY
    assert migrated["segments"][0]["status"] == material_library.MATERIAL_STATUS_READY
