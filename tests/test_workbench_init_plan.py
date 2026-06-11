import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_workbench_init_task_plan_normalizes_llm_payload():
    from webui.routes import _workbench_coerce_init_task_plan

    fallback = [{"title": "fallback", "goal": "fallback", "priority": "medium"}]
    plan = _workbench_coerce_init_task_plan(
        {
            "tasks": [
                {
                    "title": "  明确 MVP 范围  ",
                    "goal": "确定首版目标",
                    "priority": "urgent",
                    "constraints": ["  只做 Web 端  ", ""],
                    "acceptanceCriteria": ["范围已确认", ""],
                },
                {"description": "补齐登录注册流程"},
                {"title": ""},
            ]
        },
        fallback,
    )

    assert len(plan) == 2
    assert plan[0]["title"] == "明确 MVP 范围"
    assert plan[0]["priority"] == "medium"
    assert plan[0]["constraints"] == ["只做 Web 端"]
    assert plan[0]["acceptanceCriteria"] == ["范围已确认"]
    assert plan[1]["title"] == "补齐登录注册流程"


def test_workbench_init_tool_creates_task_sessions_from_major_plan():
    from webui.routes import _workbench_create_sessions_from_init_plan

    project = {"id": "project_1", "sessions": [{"id": "init_1", "kind": "init"}]}
    created = _workbench_create_sessions_from_init_plan(
        project,
        [
            {
                "title": "明确范围",
                "goal": "整理需求边界",
                "priority": "high",
                "constraints": ["范围限制：不做移动端"],
                "acceptanceCriteria": ["需求边界已确认"],
            },
            {"title": "实现核心功能", "goal": "交付 MVP", "priority": "medium"},
        ],
        "2026-06-11T00:00:00+00:00",
    )

    assert [session["title"] for session in created] == ["明确范围", "实现核心功能"]
    assert project["sessions"][0]["title"] == "明确范围"
    assert project["sessions"][1]["title"] == "实现核心功能"
    assert project["sessions"][2]["id"] == "init_1"
    assert created[0]["kind"] == "task"
    assert created[0]["priority"] == "high"
    assert created[0]["constraints"] == ["范围限制：不做移动端"]
    assert created[0]["acceptanceCriteria"][0]["text"] == "需求边界已确认"
    assert created[0]["events"][0]["type"] == "CreatedFromInitPlan"
