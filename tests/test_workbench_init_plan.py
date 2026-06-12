import sys
import subprocess
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


def test_workbench_plan_revision_preserves_existing_steps_when_feedback_is_supplemental():
    from webui.routes import _workbench_new_plan_step, _workbench_reconcile_revised_plan

    existing = [
        _workbench_new_plan_step("读取项目上下文", "理解当前实现", 1, "task_1"),
        _workbench_new_plan_step("执行验证", "运行相关检查", 2, "task_1"),
    ]
    generated = [
        _workbench_new_plan_step("使用 torch 环境执行验证", "通过 conda run -n torch 运行检查", 1, "task_1"),
    ]

    merged = _workbench_reconcile_revised_plan(existing, generated, "你可以用 conda 环境 torch")

    assert [step["title"] for step in merged] == ["读取项目上下文", "执行验证", "使用 torch 环境执行验证"]
    assert merged[0]["id"] == existing[0]["id"]
    assert merged[2]["status"] == "pending"


def test_workbench_plan_revision_allows_explicit_replacement():
    from webui.routes import _workbench_new_plan_step, _workbench_reconcile_revised_plan

    existing = [_workbench_new_plan_step("旧计划", "", 1, "task_1")]
    generated = [_workbench_new_plan_step("新计划", "", 1, "task_1")]

    merged = _workbench_reconcile_revised_plan(existing, generated, "重新规划，替换原计划")

    assert [step["title"] for step in merged] == ["新计划"]


def test_workbench_file_changes_from_write_and_edit_events(tmp_path):
    from webui.routes import _workbench_file_changes_from_tool_event

    write_changes = _workbench_file_changes_from_tool_event(
        {"tool": "Write", "args": {"path": str(tmp_path / "notes.md")}, "result": ""},
        tmp_path,
    )
    edit_changes = _workbench_file_changes_from_tool_event(
        {"tool": "Edit", "args": {"path": str(tmp_path / "src/app.py")}, "result": ""},
        tmp_path,
    )

    assert write_changes[0]["path"] == "notes.md"
    assert write_changes[0]["status"] == "created/updated"
    assert edit_changes[0]["path"] == "src/app.py"
    assert edit_changes[0]["status"] == "modified"


def test_workbench_file_changes_parse_tool_result_fallback(tmp_path):
    from webui.routes import _workbench_file_changes_from_tool_event

    changes = _workbench_file_changes_from_tool_event(
        {"tool": "custom_write", "args": {}, "result": f"Wrote {tmp_path / 'out.txt'}"},
        tmp_path,
    )

    assert changes[0]["path"] == "out.txt"
    assert changes[0]["status"] == "created/updated"


def test_workbench_git_status_delta_and_step_related_files():
    from webui.routes import _workbench_apply_step_file_changes, _workbench_git_status_delta

    changes = _workbench_git_status_delta({"old.py": " M"}, {"old.py": " M", "new.py": "??", "app.py": " M"})
    assert [(item["path"], item["status"]) for item in changes] == [("new.py", "created"), ("app.py", "modified")]

    session = {"plan": [{"id": "s1", "relatedFiles": [{"path": "old.py", "status": "modified"}]}]}
    _workbench_apply_step_file_changes(session, "s1", changes)
    assert [item["path"] for item in session["plan"][0]["relatedFiles"]] == ["old.py", "new.py", "app.py"]


import pytest


@pytest.mark.asyncio
async def test_workbench_git_diff_for_tracked_and_untracked_files(tmp_path):
    from webui.routes import _workbench_git_diff_for_path

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    tracked = tmp_path / "app.py"
    tracked.write_text("print('old')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    tracked.write_text("print('new')\n", encoding="utf-8")

    tracked_diff = await _workbench_git_diff_for_path(tmp_path, "app.py")
    assert tracked_diff["path"] == "app.py"
    assert "-print('old')" in tracked_diff["diff"]
    assert "+print('new')" in tracked_diff["diff"]

    untracked = tmp_path / "notes.md"
    untracked.write_text("# Notes\n", encoding="utf-8")
    untracked_diff = await _workbench_git_diff_for_path(tmp_path, "notes.md")
    assert untracked_diff["path"] == "notes.md"
    assert "--- /dev/null" in untracked_diff["diff"]
    assert "+++ b/notes.md" in untracked_diff["diff"]
    assert "+# Notes" in untracked_diff["diff"]


@pytest.mark.asyncio
async def test_workbench_git_diff_rejects_paths_outside_workspace(tmp_path):
    from webui.routes import _workbench_git_diff_for_path

    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError):
        await _workbench_git_diff_for_path(tmp_path, outside)


@pytest.mark.asyncio
async def test_workbench_init_task_plan_reports_llm_success(monkeypatch):
    from webui import routes as R

    async def fake_call_llm(messages, tools=None, max_tokens=None, secondary=False, thinking="auto"):
        return {"content": '{"tasks": [{"title": "拆解需求", "goal": "明确范围", "priority": "high"}]}'}

    monkeypatch.setattr(R, "_call_llm", fake_call_llm)
    plan, from_llm = await R._workbench_generate_init_task_plan(
        {"id": "p1", "name": "Demo", "template": "blank"}, {"answers": {}},
    )
    assert from_llm is True
    assert plan[0]["title"] == "拆解需求"


@pytest.mark.asyncio
async def test_workbench_init_task_plan_reports_fallback_on_failure(monkeypatch):
    from webui import routes as R

    async def failing_call_llm(messages, tools=None, max_tokens=None, secondary=False, thinking="auto"):
        raise RuntimeError("model down")

    monkeypatch.setattr(R, "_call_llm", failing_call_llm)
    plan, from_llm = await R._workbench_generate_init_task_plan(
        {"id": "p1", "name": "Demo", "template": "blank"},
        {"answers": {"goal": "做一个 CLI 工具"}},
    )
    assert from_llm is False
    assert plan, "fallback plan must not be empty"
