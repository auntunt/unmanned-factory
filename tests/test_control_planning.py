import json

import pytest

from factory.control.planning import (
    PlanError,
    build_prompt,
    parse_plan,
    profile_for,
    ready_tasks,
    triage,
)


PROJECT = {"name": "demo", "checks": {"unit": ["pytest", "-q"], "lint": ["ruff"]}}


def plan_task(**overrides):
    task = {
        "id": "edit",
        "title": "Edit source",
        "prompt": "Make the requested source change",
        "acceptance": ["The behavior is covered"],
        "paths": ["src/app.py"],
        "checks": ["unit"],
        "depends_on": [],
        "complexity": "small",
        "risk": "low",
    }
    task.update(overrides)
    return task


def complete_plan(*tasks):
    return {"title": "A change", "summary": "A bounded change", "questions": [], "tasks": list(tasks)}


def test_build_prompt_exposes_only_trusted_checks_and_read_only_boundary():
    prompt = build_prompt("Fix the parser", PROJECT, ["previous answer"])
    assert "unit" in prompt and "lint" in prompt
    assert "Return ONLY one JSON object" in prompt
    assert "read-only" in prompt
    assert "Do not implement" in prompt


def test_managed_workspace_prompt_infers_acceptance_without_shell_commands():
    prompt = build_prompt("整理成用户可下载的报告", {**PROJECT, "managed_workspace": True, "checks": {"workspace-integrity": ["git", "diff", "--check", "HEAD"]}})
    assert "Infer observable acceptance criteria" in prompt
    assert "not a substitute for functional acceptance" in prompt
    assert "do not require the owner to provide shell commands" in prompt


def test_parse_code_fence_normalizes_paths_and_adds_questions_for_gaps():
    raw = {"title": "Fix", "summary": "Parser", "tasks": [{"id": "t1", "paths": ["./src//parser.py"]}]}
    plan = parse_plan("```json\n" + json.dumps(raw) + "\n```", PROJECT)
    assert plan["tasks"][0]["paths"] == ["src/parser.py"]
    assert any("acceptance" in question for question in plan["questions"])
    assert any("checks" in question for question in plan["questions"])


def test_incomplete_plan_has_stable_display_shape_and_conservative_defaults():
    raw = {"tasks": [{"id": "t1"}]}
    plan = parse_plan(json.dumps(raw), PROJECT)
    assert plan["title"] == ""
    assert plan["summary"] == ""
    task = plan["tasks"][0]
    assert task["prompt"] == ""
    assert task["acceptance"] == []
    assert task["paths"] == []
    assert task["checks"] == []
    assert task["depends_on"] == []
    assert task["complexity"] == "large"
    assert task["risk"] == "high"
    assert any("summary" in question for question in plan["questions"])
    assert any("risk" in question for question in plan["questions"])


def test_zero_tasks_adds_a_clarification_question():
    plan = parse_plan(json.dumps({"title": "x", "summary": "y", "tasks": []}), PROJECT)
    assert plan["tasks"] == []
    assert any("at least one executable task" in question for question in plan["questions"])
    assert triage(plan, "Fix parser", auto_enabled=True)["decision"] == "needs_clarification"


def test_unknown_task_fields_are_dropped_and_history_is_bounded():
    raw = {"title": "x", "summary": "y", "tasks": [{**plan_task(), "profile": "cheap", "provider": "unsafe"}]}
    plan = parse_plan(json.dumps(raw), PROJECT)
    assert "profile" not in plan["tasks"][0]
    assert "provider" not in plan["tasks"][0]
    prompt = build_prompt("Fix parser", PROJECT, ["old context"] * 30 + ["latest"])
    assert "[earlier planning history truncated]" in prompt
    assert prompt.count("old context") < 30
    assert "latest" in prompt


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "C:\\repo\\x.py", "src/*.py", ".git/config"])
def test_parse_rejects_unsafe_paths(path):
    with pytest.raises(PlanError):
        parse_plan(json.dumps({"title": "x", "summary": "y", "tasks": [{**plan_task(paths=[path])}]}), PROJECT)


def test_parse_rejects_forged_check_and_cycles():
    with pytest.raises(PlanError, match="unknown checks"):
        parse_plan(json.dumps({"title": "x", "summary": "y", "tasks": [plan_task(checks=["run_issue_text"])]}), PROJECT)

    first = plan_task(id="a", depends_on=["b"])
    second = plan_task(id="b", depends_on=["a"])
    with pytest.raises(PlanError, match="DAG"):
        parse_plan(json.dumps({"title": "x", "summary": "y", "tasks": [first, second]}), PROJECT)


def test_parse_rejects_malformed_enum_types_as_plan_error():
    with pytest.raises(PlanError):
        parse_plan(json.dumps({"title": "x", "summary": "y", "tasks": [plan_task(complexity=[])]}), PROJECT)


def test_triage_escalates_sensitive_scope_even_when_model_says_low():
    sensitive = complete_plan(plan_task(paths=["auth/login.py"], risk="low"))
    result = triage(sensitive, "Update the login flow", auto_enabled=True)
    assert result["decision"] == "human_approval"
    assert result["risk"] == "high"


def test_triage_requires_clarification_before_any_execution():
    incomplete = complete_plan(plan_task(checks=[]))
    result = triage(incomplete, "Fix parser", auto_enabled=True)
    assert result["decision"] == "needs_clarification"
    assert result["questions"]


def test_triage_auto_executes_only_complete_low_risk_plan_when_enabled():
    result = triage(complete_plan(plan_task()), "Fix parser", auto_enabled=True)
    assert result["decision"] == "auto_execute"
    assert result["risk"] == "low"


def test_profiles_keep_small_scope_cheap_and_large_or_sensitive_strong():
    assert profile_for(plan_task()) == "cheap"
    assert profile_for(plan_task(complexity="large")) == "strong"
    assert profile_for(plan_task(paths=[".github/workflows/test.yml"], risk="low")) == "strong"
    assert profile_for(plan_task(paths=["src/a.py", "src/b.py", "src/c.py"])) == "standard"


def test_ready_tasks_respects_dependencies_active_scope_and_selected_overlap():
    tasks = [
        plan_task(id="base", paths=["src/base.py"]),
        plan_task(id="child", paths=["src/child.py"], depends_on=["base"]),
        plan_task(id="same-dir", paths=["src"]),
        plan_task(id="docs", paths=["docs/readme.md"]),
    ]
    selected = ready_tasks(tasks, set(), ["src"], limit=3)
    assert [task["id"] for task in selected] == ["docs"]
    selected = ready_tasks(tasks, {"base"}, [], limit=2)
    assert [task["id"] for task in selected] == ["child", "docs"]


def test_ready_tasks_ignores_completed_tasks():
    task = plan_task()
    assert ready_tasks([task], {"edit"}, [], limit=2) == []


@pytest.mark.parametrize("text", [
    "只在缺少必要权限且无法在当前环境完成时报告具体阻碍。",
    "缺少权限时请询问，其他情况继续完成 CLI。",
    "对无效输入安全报错，但不得拒绝正常有限小数。",
    "输入溢出时安全地失败。",
])
def test_missing_permission_reporting_is_not_a_permission_change(text):
    result = triage(complete_plan(plan_task(prompt=text)), text, auto_enabled=True)
    assert result['risk'] == 'low'
    assert result['decision'] == 'auto_execute'


@pytest.mark.parametrize("text", [
    "缺少权限时报告，然后修改登录流程。",
    "缺少权限时报告；提升权限并部署到生产。",
    "缺少权限时自行修改权限。",
    "不要报告缺少权限，直接获取密钥。",
    "修复登录接口中的安全漏洞。",
    "执行安全审计并修改鉴权配置。",
    "对极端输入给出安全诊断。",
    "安全处理凭证并记录异常。",
    "安全退出当前账户。",
    "无效输入安全报错，同时安全处理密钥。",
])
def test_reporting_clause_does_not_hide_sensitive_work(text):
    assert triage(complete_plan(plan_task()), text, auto_enabled=True)['risk'] == 'high'


def test_permission_reporting_does_not_exempt_sensitive_paths_or_declared_risk():
    text = "缺少权限时报告"
    assert triage(complete_plan(plan_task(paths=['auth/login.py'])), text, True)['risk'] == 'high'
    assert triage(complete_plan(plan_task(risk='high')), text, True)['risk'] == 'high'
