from app.services.billing import (
    billing_csv,
    collect_seedance_billing,
    display_rows,
    load_all_tasks,
)


def test_collect_seedance_billing_keeps_lifetime_costs_and_data_gaps():
    tasks = [
        {
            "task_id": "new-task",
            "video_subject": "新品介绍",
            "updated_at": "2026-07-22T02:00:00+00:00",
            "generation_report": {
                "completed_at": "2026-07-22T10:00:00+08:00",
                "seedance_billing": {
                    "scenes": [
                        {
                            "scene_index": 0,
                            "provider_task_id": "provider-1",
                            "model": "seedance-1-5-pro",
                            "resolution": "720p",
                            "aspect_ratio": "9:16",
                            "provider_requested_duration_seconds": 5,
                            "status": "completed",
                            "billable": True,
                            "tokens": 100_000,
                            "unit_price_cny_per_million_tokens": 46,
                            "estimated_cost_cny": 4.6,
                            "calculation_method": "api_usage",
                        },
                        {
                            "scene_index": 0,
                            "provider_task_id": "provider-retry",
                            "status": "failed",
                            "billable": False,
                            "tokens": None,
                            "estimated_cost_cny": 0,
                            "calculation_method": "not_billable",
                        },
                        {
                            "scene_index": 1,
                            "provider_task_id": "provider-2",
                            "status": "completed",
                            "billable": True,
                            "tokens": 90_000,
                            "estimated_cost_cny": 4.14,
                            "calculation_method": "official_formula_estimate",
                        },
                        {
                            "scene_index": 2,
                            "provider_task_id": "provider-3",
                            "status": "failed",
                            "billable": None,
                            "tokens": None,
                            "estimated_cost_cny": None,
                            "calculation_method": "unavailable",
                        },
                    ]
                },
            },
        },
        {
            "task_id": "legacy-task",
            "seedance_provider_task_ids": {"0": "legacy-provider"},
        },
        {
            "task_id": "running-task",
            "status": "processing",
            "seedance_provider_task_ids": {"0": "running-provider"},
        },
    ]

    summary = collect_seedance_billing(tasks)

    assert len(summary["rows"]) == 4
    assert summary["total_cost_cny"] == 8.74
    assert summary["total_tokens"] == 190_000
    assert summary["billable_scene_count"] == 2
    assert summary["estimated_scene_count"] == 1
    assert summary["unknown_cost_count"] == 1
    assert summary["untracked_task_ids"] == ["legacy-task"]
    assert summary["rows"][0]["task_status"] == "-"
    assert summary["rows"][0]["task_stage"] == "-"
    assert [row["provider_task_id"] for row in summary["rows"]] == [
        "provider-3",
        "provider-2",
        "provider-retry",
        "provider-1",
    ]


def test_display_rows_and_csv_preserve_chinese_billing_columns():
    rows = [
        {
            "recorded_at": "2026-07-22T10:00:00+08:00",
            "subject": "测试视频",
            "task_id": "task-1",
            "task_status": "completed",
            "task_stage": "completed",
            "scene_number": 1,
            "provider_task_id": "provider-1",
            "model": "seedance",
            "specification": "720p / 9:16",
            "duration_seconds": 5.0,
            "status": "completed",
            "billable": True,
            "tokens": 100,
            "unit_price_cny": 46.0,
            "cost_cny": 0.0046,
            "calculation_label": "接口返回",
        }
    ]

    table = display_rows(rows)
    assert table[0]["是否计费"] == "是"
    assert table[0]["任务状态"] == "completed"
    assert table[0]["任务阶段"] == "completed"
    assert table[0]["金额（元）"] == 0.0046
    decoded = billing_csv(rows).decode("utf-8-sig")
    assert "账单记录时间,作品主题,项目任务 ID" in decoded
    assert "测试视频" in decoded


def test_load_all_tasks_reads_every_page_without_duplicates():
    class FakeStore:
        def list_tasks(self, page, page_size):
            assert page_size == 2
            return {
                1: ([{"task_id": "3"}, {"task_id": "2"}], 3),
                2: ([{"task_id": "2"}, {"task_id": "1"}], 3),
            }[page]

    assert [task["task_id"] for task in load_all_tasks(FakeStore(), page_size=2)] == [
        "3",
        "2",
        "1",
    ]
