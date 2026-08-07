"""TaskExtractor 测试。用 fake claude binary，不调真模型。

fake_claude 的设计和 test_shell_harness.py / test_model_supervisors.py 一致：
它是一个真正在磁盘上的脚本，由 tmp_path 拿到路径，写成 --json-schema 返回
结构化输出的格式，可以精确控制输出内容。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from factory.intake.extract import DraftTask, IntakeError, TaskExtractor


# ── fake binary helpers ──────────────────────────────────────────────────

def make_fake_claude(tmp_path: Path, stdout_json: dict | str) -> str:
    """写一个可执行脚本：无论 argv 是什么，把 stdout_json 打到 stdout。"""
    payload = (
        stdout_json if isinstance(stdout_json, str) else json.dumps(stdout_json)
    )
    script = tmp_path / "fake_claude"
    script.write_text(
        f"#!/bin/sh\ncat <<'__END__'\n{payload}\n__END__\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def claude_response(structured: dict, *, tokens_in=10, tokens_out=20, cost=0.001):
    """模拟真实的 claude --output-format json 外层包装。"""
    return {
        "is_error": False,
        "structured_output": structured,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        "total_cost_usd": cost,
    }


def error_response(subtype="error", stop_reason="max_tokens", result="模型错误"):
    return {"is_error": True, "subtype": subtype,
            "stop_reason": stop_reason, "result": result}


# ── 基础：正常抽取 ────────────────────────────────────────────────────────

def test_basic_extraction(tmp_path):
    payload = claude_response({
        "task_id": "T-add-retry",
        "prompt": "在 dispatcher.py 里给 run() 加指数退避重试，最多 3 次。",
        "declared_paths": ["factory/dispatcher.py"],
        "declared_ops": [],
        "checks": [{"name": "smoke", "command": "python -c 'import factory'"}],
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("dispatcher 加重试")
    assert draft.task_id == "T-add-retry"
    assert "dispatcher.py" in draft.prompt or "dispatcher" in draft.prompt
    assert "factory/dispatcher.py" in draft.declared_paths
    assert len(draft.checks) == 1
    assert draft.checks[0]["name"] == "smoke"
    assert draft.tokens == 30
    assert draft.cost_usd == pytest.approx(0.001)


def test_guard_adds_prod_deploy_the_model_missed(tmp_path):
    """模型提取了，但忘记加 prod_deploy。guard 必须补上。

    这是 extract + guard 整合的主测试 —— 所有安全保证的终点。
    """
    payload = claude_response({
        "task_id": "T-retry-and-release",
        "prompt": "加指数退避，改完直接上线。",
        "declared_ops": [],      # 模型漏了
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("加指数退避，改完直接上线")
    assert "prod_deploy" in draft.declared_ops
    assert any(f.op == "prod_deploy" for f in draft.guard_findings)


def test_guard_does_not_duplicate_ops_model_already_declared(tmp_path):
    payload = claude_response({
        "task_id": "T-deploy",
        "prompt": "上线",
        "declared_ops": ["prod_deploy"],
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("上线")
    assert draft.declared_ops.count("prod_deploy") == 1
    assert draft.guard_findings == (), "模型已报的不算 guard 的功劳"


def test_model_ops_the_guard_cannot_see_are_kept(tmp_path):
    """模型能读懂绕弯的说法，结果保留，不被 guard 抹掉。"""
    payload = claude_response({
        "task_id": "T-cleanup",
        "prompt": "处理掉那批老东西",
        "declared_ops": ["data_delete"],   # guard 扫不到
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("处理掉那批老东西")
    assert "data_delete" in draft.declared_ops


# ── 错误处理 ─────────────────────────────────────────────────────────────

def test_raises_on_model_error(tmp_path):
    binary = make_fake_claude(tmp_path, error_response())
    with pytest.raises(IntakeError, match="模型调用失败"):
        TaskExtractor(binary=binary).run("随便什么")


def test_raises_on_invalid_json(tmp_path):
    script = tmp_path / "bad_claude"
    script.write_text("#!/bin/sh\necho 'not json'\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(IntakeError, match="非 JSON"):
        TaskExtractor(binary=str(script)).run("随便什么")


def test_raises_when_task_id_missing(tmp_path):
    payload = claude_response({"prompt": "加重试"})   # 缺 task_id
    binary = make_fake_claude(tmp_path, payload)
    with pytest.raises(IntakeError, match="task_id"):
        TaskExtractor(binary=binary).run("随便什么")


def test_raises_on_missing_binary(tmp_path):
    with pytest.raises(IntakeError, match="无法启动"):
        TaskExtractor(binary=str(tmp_path / "nope")).run("x")


# ── DraftTask.to_yaml ────────────────────────────────────────────────────

def test_draft_task_yaml_is_valid_yaml(tmp_path):
    payload = claude_response({
        "task_id": "T-test-yaml",
        "prompt": "测试 YAML 序列化。",
        "declared_ops": ["prod_deploy"],
        "checks": [{"name": "ok", "command": "true", "expect": "exit_zero"}],
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("改完上线")
    # 注释是合法 YAML，safe_load 直接吃。这一点很重要：注释头不能破坏
    # Task.from_yaml —— 否则草稿就得先手动删注释才能派发。
    doc = yaml.safe_load(draft.to_yaml())
    assert doc["task_id"] == "T-test-yaml"
    assert doc["declared_ops"] == ["prod_deploy"]
    assert doc["checks"][0]["command"] == "true"


def test_draft_task_write_creates_file(tmp_path):
    payload = claude_response({
        "task_id": "T-write-test",
        "prompt": "写文件测试。",
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("写文件测试")
    out = tmp_path / "out.yaml"
    path = draft.write(out)
    assert path == out
    content = out.read_text(encoding="utf-8")
    assert "T-write-test" in content
    assert "factory prd" in content, "提醒人要确认的注释头必须在"


def test_draft_guard_findings_appear_in_yaml_comments(tmp_path):
    payload = claude_response({
        "task_id": "T-deploy-miss",
        "prompt": "改完上线",
        "declared_ops": [],    # 模型漏了
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("改完上线")
    raw = draft.to_yaml()
    # 注释里必须有 guard 的解释，否则人不知道为什么出现了一个 prod_deploy
    assert "guard" in raw
    assert "prod_deploy" in raw


def test_unclear_items_appear_in_yaml_comments(tmp_path):
    payload = claude_response({
        "task_id": "T-unclear",
        "prompt": "改那个东西",
        "unclear": ["改哪个文件？", "验收标准是什么？"],
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("改那个东西")
    raw = draft.to_yaml()
    assert "改哪个文件" in raw
    assert "验收标准" in raw


# ── 安全：描述文字里的密钥不进 prompt ────────────────────────────────────

def test_secrets_in_description_are_redacted(tmp_path):
    """redact_text 必须在把描述拼进 prompt 之前跑。"""
    captured_argv: list[list[str]] = []

    script = tmp_path / "capture_claude"
    log = tmp_path / "argv.json"
    script.write_text(
        f"#!/bin/sh\necho \"$@\" >> {log}\n"
        + "cat <<'__END__'\n"
        + json.dumps(claude_response({"task_id": "T-x", "prompt": "x"}))
        + "\n__END__\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    desc_with_secret = "把 PASSWORD=s3cr3tV@lue 的配置改掉"
    TaskExtractor(binary=str(script)).run(desc_with_secret)

    logged = log.read_text(encoding="utf-8")
    assert "s3cr3tV@lue" not in logged, "密码原文不能出现在传给模型的 argv 里"
    assert "REDACTED" in logged


# ── CLI prd 命令 ──────────────────────────────────────────────────────────

def test_cli_prd_dry_run(tmp_path):
    payload = claude_response({
        "task_id": "T-greet",
        "prompt": "加一个 greet 函数。",
    })
    binary = make_fake_claude(tmp_path, payload)
    from factory.cli import main
    rc = main(["prd", "--text", "加一个 greet 函数",
               "--dry-run", "--binary", binary])
    assert rc == 0


def test_cli_prd_writes_file(tmp_path):
    payload = claude_response({
        "task_id": "T-cli-write",
        "prompt": "测试 cli 写文件。",
    })
    binary = make_fake_claude(tmp_path, payload)
    out = tmp_path / "task.yaml"
    from factory.cli import main
    rc = main(["prd", "--text", "测试", "--output", str(out), "--binary", binary])
    assert rc == 0
    assert out.exists()
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert doc["task_id"] == "T-cli-write"


def test_cli_prd_output_is_loadable_by_task_from_yaml(tmp_path):
    """闭环：prd 的产物必须能直接被 Task.from_yaml 读进来。

    入口层和派发层之间只有这一个接口。它对不上的话，整条
    「说话 → 派发」的链路就是断的，而两边的单测都会绿。
    """
    payload = claude_response({
        "task_id": "T-roundtrip",
        "prompt": "抽出 slugify 到 text.py。",
        "spec_ref": ["AC-3"],
        "declared_paths": ["text.py"],
        "declared_ops": [],
        "checks": [{"name": "pytest", "command": "python -m pytest -q"}],
    })
    binary = make_fake_claude(tmp_path, payload)
    out = tmp_path / "rt.yaml"
    from factory.cli import main
    assert main(["prd", "--text", "抽出 slugify",
                 "--output", str(out), "--binary", binary]) == 0

    from factory.task import Task
    task = Task.from_yaml(out)
    assert task.task_id == "T-roundtrip"
    assert task.spec_ref == ("AC-3",)
    assert task.declared_paths == ("text.py",)
    assert task.checks[0].name == "pytest"
    assert task.checks[0].expect == "exit_zero"
    assert task.max_rounds == 3


def test_acceptance_reaches_the_spec_supervisor(tmp_path):
    """acceptance 存在的全部理由：口述任务能过规格监工。

    真跑发现的：一句口述需求没有外部文档可引，spec_ref 必然为空，
    于是规格监工每次都判 supervisor-spec-no-criteria —— 所有口述来源的
    任务永久失败，而那不是任务的错，是入口层和验收层的接口对不上。
    这条测试钉住修法：Task.criteria = spec_ref + acceptance。
    """
    payload = claude_response({
        "task_id": "T-accept",
        "prompt": "加 truncate_words。",
        "spec_ref": [],
        "acceptance": [
            "truncate_words 词数超过 n 时返回前 n 个词并以 '...' 结尾",
            "词数不超过 n 时原样返回",
        ],
    })
    binary = make_fake_claude(tmp_path, payload)
    out = tmp_path / "a.yaml"
    from factory.cli import main
    assert main(["prd", "--text", "加 truncate_words",
                 "--output", str(out), "--binary", binary]) == 0

    from factory.task import Task
    task = Task.from_yaml(out)
    assert task.spec_ref == ()
    assert len(task.acceptance) == 2
    # 这一条才是重点：规格监工拿到的不是空的
    assert len(task.criteria) == 2
    assert "'...'" in task.criteria[0]


def test_empty_acceptance_is_flagged_in_the_yaml_header(tmp_path):
    """acceptance 空 → 注释里必须警告，否则人要花一轮派发才发现。"""
    payload = claude_response({
        "task_id": "T-no-accept",
        "prompt": "随便改点东西",
        "acceptance": [],
    })
    binary = make_fake_claude(tmp_path, payload)
    raw = TaskExtractor(binary=binary).run("随便改点东西").to_yaml()
    assert "acceptance 为空" in raw
    assert "spec-review" in raw


def test_acceptance_present_means_no_warning(tmp_path):
    payload = claude_response({
        "task_id": "T-ok-accept",
        "prompt": "x",
        "acceptance": ["函数返回前 n 个词"],
    })
    binary = make_fake_claude(tmp_path, payload)
    raw = TaskExtractor(binary=binary).run("x").to_yaml()
    assert "acceptance 为空" not in raw


def test_blank_acceptance_entries_dropped(tmp_path):
    payload = claude_response({
        "task_id": "T-blank",
        "prompt": "x",
        "acceptance": ["  ", "", "真的一条标准"],
    })
    binary = make_fake_claude(tmp_path, payload)
    draft = TaskExtractor(binary=binary).run("x")
    assert draft.acceptance == ("真的一条标准",)


def test_cli_prd_rejects_two_input_sources(tmp_path):
    from factory.cli import main
    rc = main(["prd", "--text", "a", "--text-file", str(tmp_path / "b.txt")])
    assert rc == 2


def test_cli_prd_rejects_zero_input_sources():
    from factory.cli import main
    assert main(["prd"]) == 2


def test_cli_prd_reads_text_file(tmp_path):
    payload = claude_response({"task_id": "T-from-file", "prompt": "x"})
    binary = make_fake_claude(tmp_path, payload)
    src = tmp_path / "need.txt"
    src.write_text("把重试改成指数退避", encoding="utf-8")
    from factory.cli import main
    rc = main(["prd", "--text-file", str(src), "--dry-run", "--binary", binary])
    assert rc == 0


def test_cli_prd_missing_audio_binary_returns_2(tmp_path):
    """whisper 没装 → 退出码 2 加清楚的提示，不是 traceback。"""
    audio = tmp_path / "rec.m4a"
    audio.write_bytes(b"\x00" * 16)
    from factory.cli import main
    rc = main(["prd", "--audio", str(audio),
               "--whisper-binary", str(tmp_path / "no-such-whisper")])
    assert rc == 2
