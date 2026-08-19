from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from typing import Any


CALCULATION_METHOD_LABELS = {
    "api_usage": "接口返回",
    "official_formula_estimate": "官方公式估算",
    "unavailable": "无法估算",
    "not_billable": "不计费",
}
ACTIVE_TASK_STATUSES = {
    "queued",
    "processing",
    "cancellation_requested",
    "awaiting_approval",
    "awaiting_material",
}


def load_all_tasks(store, *, page_size: int = 200) -> list[dict[str, Any]]:
    """读取任务库中的全部任务，避免账单只覆盖任务列表第一页。"""
    page = 1
    tasks: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    while True:
        page_tasks, total = store.list_tasks(page=page, page_size=page_size)
        new_task_count = 0
        for task in page_tasks:
            task_id = str(task.get("task_id") or "")
            if task_id and task_id in seen_task_ids:
                continue
            if task_id:
                seen_task_ids.add(task_id)
            tasks.append(task)
            new_task_count += 1
        if not page_tasks or len(tasks) >= int(total or 0) or new_task_count == 0:
            break
        page += 1
    return tasks


def _numeric(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value) -> int | None:
    numeric = _numeric(value)
    return int(numeric) if numeric is not None else None


def collect_seedance_billing(tasks: Iterable[Mapping]) -> dict[str, Any]:
    """把历史任务中的 Seedance 分镜记录整理为项目生命周期账单。"""
    rows: list[dict[str, Any]] = []
    untracked_task_ids: list[str] = []

    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        report = task.get("generation_report")
        report = report if isinstance(report, Mapping) else {}
        billing = report.get("seedance_billing")
        billing = billing if isinstance(billing, Mapping) else {}
        scenes = billing.get("scenes")
        scenes = scenes if isinstance(scenes, list) else []
        valid_scenes = [scene for scene in scenes if isinstance(scene, Mapping)]

        provider_tasks = task.get("seedance_provider_task_ids")
        if (
            isinstance(provider_tasks, Mapping)
            and any(str(value or "").strip() for value in provider_tasks.values())
            and not valid_scenes
            and str(task.get("status") or "") not in ACTIVE_TASK_STATUSES
        ):
            untracked_task_ids.append(str(task.get("task_id") or ""))

        recorded_at = str(
            report.get("completed_at")
            or report.get("updated_at")
            or task.get("updated_at")
            or task.get("created_at")
            or ""
        )
        task_id = str(task.get("task_id") or "-")
        subject = str(task.get("video_subject") or task_id)
        for position, scene in enumerate(valid_scenes):
            try:
                scene_number = int(scene.get("scene_index", position)) + 1
            except (TypeError, ValueError):
                scene_number = position + 1
            resolution = str(scene.get("resolution") or "").strip()
            aspect_ratio = str(scene.get("aspect_ratio") or "").strip()
            calculation_method = str(scene.get("calculation_method") or "")
            rows.append(
                {
                    "recorded_at": recorded_at,
                    "subject": subject,
                    "task_id": task_id,
                    "task_status": str(task.get("status") or "-"),
                    "task_stage": str(task.get("stage") or "-"),
                    "task_updated_at": str(task.get("updated_at") or ""),
                    "scene_number": scene_number,
                    "provider_task_id": str(scene.get("provider_task_id") or "-"),
                    "model": str(scene.get("model") or "-"),
                    "specification": " / ".join(
                        value for value in (resolution, aspect_ratio) if value
                    )
                    or "-",
                    "duration_seconds": _numeric(
                        scene.get("provider_requested_duration_seconds")
                    ),
                    "status": str(scene.get("status") or "-"),
                    "billable": scene.get("billable"),
                    "tokens": _integer(scene.get("tokens")),
                    "unit_price_cny": _numeric(
                        scene.get("unit_price_cny_per_million_tokens")
                    ),
                    "cost_cny": _numeric(scene.get("estimated_cost_cny")),
                    "calculation_method": calculation_method,
                    "calculation_label": CALCULATION_METHOD_LABELS.get(
                        calculation_method, calculation_method or "-"
                    ),
                }
            )

    rows.sort(
        key=lambda row: (
            row["recorded_at"],
            row["task_id"],
            row["scene_number"],
            row["provider_task_id"],
        ),
        reverse=True,
    )
    billable_rows = [row for row in rows if row["billable"] is True]
    unknown_rows = [
        row
        for row in rows
        if row["billable"] is None
        or (row["billable"] is True and row["cost_cny"] is None)
    ]
    return {
        "rows": rows,
        "total_cost_cny": round(
            sum(row["cost_cny"] or 0.0 for row in billable_rows), 4
        ),
        "total_tokens": sum(row["tokens"] or 0 for row in billable_rows),
        "billable_scene_count": len(billable_rows),
        "estimated_scene_count": sum(
            row["calculation_method"] == "official_formula_estimate"
            for row in billable_rows
        ),
        "unknown_cost_count": len(unknown_rows),
        "untracked_task_ids": [task_id for task_id in untracked_task_ids if task_id],
    }


def display_rows(rows: Iterable[Mapping]) -> list[dict[str, Any]]:
    """生成适合 Streamlit 表格和 CSV 下载的中文列。"""
    result = []
    for row in rows:
        billable = row.get("billable")
        result.append(
            {
                "账单记录时间": row.get("recorded_at") or "-",
                "作品主题": row.get("subject") or "-",
                "项目任务 ID": row.get("task_id") or "-",
                "任务状态": row.get("task_status") or "-",
                "任务阶段": row.get("task_stage") or "-",
                "分镜": row.get("scene_number"),
                "Seedance 任务 ID": row.get("provider_task_id") or "-",
                "模型": row.get("model") or "-",
                "规格": row.get("specification") or "-",
                "请求时长（秒）": row.get("duration_seconds"),
                "状态": row.get("status") or "-",
                "是否计费": (
                    "是" if billable is True else "否" if billable is False else "待确认"
                ),
                "Token": row.get("tokens"),
                "单价（元/百万 Token）": row.get("unit_price_cny"),
                "金额（元）": row.get("cost_cny"),
                "计算依据": row.get("calculation_label") or "-",
            }
        )
    return result


def billing_csv(rows: Iterable[Mapping]) -> bytes:
    table_rows = display_rows(rows)
    if not table_rows:
        return b""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(table_rows[0]))
    writer.writeheader()
    writer.writerows(table_rows)
    return output.getvalue().encode("utf-8-sig")
