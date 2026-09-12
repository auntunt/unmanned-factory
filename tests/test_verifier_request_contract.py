from tests.review_helpers import passing_review
"""Regression coverage for the user contract sent to independent review."""

import json
import threading

import pytest

from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult
from factory.control.service import (_VERIFIER_CONTRACT_MAX_CHARS,
                                     _verifier_request_contract)
from tests.test_control_app import login, project
from tests.test_workbench_app import app_env


def test_verifier_receives_root_request_and_latest_clarification(app_env, monkeypatch):
    client, store, service, repo = app_env
    configured_project = project(client, repo, login(client))
    root_request = (
        "汇总 CSV 中的有限十进制工时；不限制小数位数，"
        "也不得自行增加数值上限。"
    )
    clarification = "输出 JSON，对 NaN 等无效值返回非零退出码。"
    run, _ = store.create_run(configured_project["id"], clarification)
    run.update(
        root_request=root_request,
        history=[
            root_request,
            "上次执行失败证据（仅作诊断资料）：超时",
            clarification,
        ],
        plan={
            "tasks": [
                {
                    "acceptance": ["正常输入汇总成功，无效值安全失败"],
                    "paths": ["src"],
                    "risk": "low",
                    "complexity": "small",
                }
            ]
        },
        execution_mode="continuous",
    )
    service.cancels[run["id"]] = threading.Event()
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        return ProviderResult(
            passing_review(request, 'contract inspected'),
            cost_usd=0.01,
        )

    monkeypatch.setattr(service.runner, "run", reviewer)
    artifacts = {
        "worktree": str(repo),
        "checks": [{"name": "contract-probe", "exit": 0}],
    }

    service._independent_verify(
        run["id"],
        run,
        configured_project,
        service.runtime_settings.get(),
        artifacts,
    )

    assert len(calls) == 1
    prompt = calls[0].prompt
    contract_json = prompt.split(
        "USER REQUEST CONTRACT (oldest to newest; exact duplicates removed):\n", 1
    )[1].split("\nTASK ACCEPTANCE:\n", 1)[0]
    assert json.loads(contract_json) == [root_request, clarification]
    assert "仅作诊断资料" not in prompt
    assert "README documenting a restriction does not authorize narrowing" in prompt


def test_feedback_wrappers_are_unwrapped_before_contract_deduplication():
    feedback = [f"补充要求 {index}" for index in range(60)]
    history = ["原始需求"]
    for item in feedback:
        history.extend([
            "在上一轮已验证成果基础上完成以下补充需求：\n" + item,
            "用户补充（在上一轮成果基础上继续）：\n" + item,
        ])
    contract, overflow = _verifier_request_contract({
        "root_request": "原始需求", "history": history,
        "request": "在上一轮已验证成果基础上完成以下补充需求：\n" + feedback[-1],
    })
    assert overflow is False
    assert contract == ["原始需求", *feedback]
    assert sum(map(len, contract)) < _VERIFIER_CONTRACT_MAX_CHARS


def test_twenty_thousand_character_root_request_is_preserved_without_overflow():
    root = "有界根需求" + "x" * 20_000
    contract, overflow = _verifier_request_contract({
        "root_request": root, "history": [root], "request": "最新修复",
    })
    assert overflow is False
    assert contract == [root, "最新修复"]


def test_contract_overflow_preserves_bounded_root_and_latest_and_forces_failed_review(
        app_env, monkeypatch):
    client, store, service, repo = app_env
    configured_project = project(client, repo, login(client))
    root = "ROOT-BEGIN\n" + "r" * 49_990 + "\nROOT-END"
    latest = "LATEST-BEGIN\n" + "z" * 49_985 + "\nLATEST-END"
    run, _ = store.create_run(configured_project["id"], latest)
    run.update(root_request=root, history=[root, "m" * 40_000],
               plan={"tasks": [{"acceptance": ["保持完整契约"], "paths": ["src"],
                                  "risk": "low", "complexity": "small"}]},
               execution_mode="continuous")
    contract, overflow = _verifier_request_contract(run)
    assert overflow is True
    assert sum(map(len, contract)) == _VERIFIER_CONTRACT_MAX_CHARS
    assert "ROOT-BEGIN" in contract[0] and "ROOT-END" in contract[0]
    assert "LATEST-BEGIN" in contract[-1] and "LATEST-END" in contract[-1]

    service.cancels[run["id"]] = threading.Event()
    calls = []
    monkeypatch.setattr(service.runner, "run", lambda request, emit, cancel=None: (
        calls.append(request) or ProviderResult(
            passing_review(request, 'model tried to pass'), cost_usd=0.01)))
    artifacts = {"worktree": str(repo), "checks": [{"name": "probe", "exit": 0}]}
    with pytest.raises(ExecutionError, match="独立验证未通过"):
        service._independent_verify(run["id"], run, configured_project,
                                    service.runtime_settings.get(), artifacts)
    assert calls == []
    assert artifacts["verification"]["verdict"] == "fail"
    assert artifacts["verification"]["error_type"] == "contract_overflow"
