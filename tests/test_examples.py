"""examples/ 下的任务 YAML 必须真的能派发。

这个文件存在的理由：`examples/greet_task.yaml` 是 README 的第一条命令，
它带着 `spec_ref: [AC-1]` 而仓库里没有任何文档定义 AC-1 —— 也就是说
照 README 抄的第一次运行会被闸门拦下（在闸门存在之前：烧三轮再上人）。
没人发现，因为**没有任何测试碰过 examples/**。

所以这里不测「文件长什么样」，测「拿它去派发会不会被拦」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.task import Task

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _yamls():
    return sorted(EXAMPLES.glob("*.yaml"))


def test_there_are_examples_to_check():
    """glob 空了的话下面那些参数化测试会静默零条 —— 那和全绿长得一样。"""
    assert _yamls(), f"{EXAMPLES} 下没有任务 YAML"


@pytest.mark.parametrize("path", _yamls(), ids=lambda p: p.name)
def test_an_example_parses(path):
    task = Task.from_yaml(path)
    assert task.task_id and task.prompt.strip()


@pytest.mark.parametrize("path", _yamls(), ids=lambda p: p.name)
def test_an_example_has_acceptance_criteria_that_resolve(path):
    """每个例子都得能给规格监工递出**正文**。

    这一条同时覆盖两种合法形状：写死 acceptance（口述形状），
    或 spec_ref + spec_doc（引文档形状）。前者不需要 root，
    后者的文档就在 examples/ 里，所以 root 取那个目录。
    """
    task = Task.from_yaml(path)
    got = task.resolve_spec(EXAMPLES)
    assert got.ok, f"{path.name} 的规格引用解析不了：{got.doc_error or got.missing}"

    criteria = task.criteria(EXAMPLES)
    assert criteria, f"{path.name} 没有任何验收标准，规格监工无从判定"
    for c in criteria:
        assert len(c.strip()) > 4, f"{path.name} 有一条标准短得像编号：{c!r}"


@pytest.mark.parametrize("path", _yamls(), ids=lambda p: p.name)
def test_an_example_would_pass_the_intake_gate(path):
    """例子该是「够格进队」的样板。被自家闸门拦下的样板会教坏人。"""
    from factory.intake.extract import DraftTask
    from factory.intake.gate import admit

    task = Task.from_yaml(path)
    draft = DraftTask(
        task_id=task.task_id, prompt=task.prompt,
        spec_ref=task.spec_ref, spec_doc=task.spec_doc,
        acceptance=task.acceptance,
        declared_paths=task.declared_paths,
        declared_ops=task.declared_ops,
        checks=tuple({"name": c.name, "command": c.command} for c in task.checks),
    )
    v = admit(draft)
    assert v.admitted, f"{path.name} 会被闸门拦：{v.reasons}"


@pytest.mark.parametrize("path", _yamls(), ids=lambda p: p.name)
def test_an_example_has_checks(path):
    """没有 check 的任务在回归监工那里拿 no-checks-defined FAIL。"""
    assert Task.from_yaml(path).checks, f"{path.name} 没有 check"
