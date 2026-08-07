from factory.task import CheckSpec, Task


def test_from_yaml_full(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(
        "task_id: T-100\n"
        "prompt: |\n"
        "  在 greet.py 里加一个 greet(name) 函数\n"
        "spec_ref: [AC-1, AC-2]\n"
        "declared_paths: ['greet.py']\n"
        "declared_ops: []\n"
        "max_rounds: 2\n"
        "checks:\n"
        "  - name: pytest\n"
        "    command: python -m pytest -q\n"
        "  - name: image-id-agrees\n"
        "    command: echo abc\n"
        "    expect: commands_agree\n"
        "    value: echo abc\n"
        "  - name: has-greet\n"
        "    command: cat greet.py\n"
        "    expect: stdout_contains\n"
        "    value: 'def greet'\n",
        encoding="utf-8",
    )
    t = Task.from_yaml(p)
    assert t.task_id == "T-100"
    assert "greet(name)" in t.prompt
    assert t.spec_ref == ("AC-1", "AC-2")
    assert t.declared_paths == ("greet.py",)
    assert t.declared_ops == ()
    assert t.max_rounds == 2
    assert len(t.checks) == 3
    assert t.checks[0] == CheckSpec(name="pytest", command="python -m pytest -q")
    assert t.checks[1].expect == "commands_agree"
    assert t.checks[2].value == "def greet"


def test_from_yaml_minimal_defaults(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("task_id: T-1\nprompt: do it\n", encoding="utf-8")
    t = Task.from_yaml(p)
    assert t.checks == ()
    assert t.declared_paths == ()
    assert t.max_rounds == 3


def test_criteria_unions_spec_ref_and_acceptance(tmp_path):
    """spec_ref 引用外部文档，acceptance 是本任务写明的标准。监工拿并集。

    区分的理由见 Task 的 docstring：口述需求没有外部文档可引，
    只有 spec_ref 会让所有 `factory prd` 产出的任务永久过不了规格监工。
    """
    p = tmp_path / "t.yaml"
    p.write_text(
        "task_id: T-1\nprompt: x\n"
        "spec_ref: [AC-1]\n"
        "acceptance:\n  - 超过 n 个词时截断\n  - 不超过时原样返回\n",
        encoding="utf-8",
    )
    task = Task.from_yaml(p)
    assert task.spec_ref == ("AC-1",)
    assert task.acceptance == ("超过 n 个词时截断", "不超过时原样返回")
    assert task.criteria == ("AC-1", "超过 n 个词时截断", "不超过时原样返回")


def test_criteria_empty_when_neither_given(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("task_id: T-1\nprompt: x\n", encoding="utf-8")
    assert Task.from_yaml(p).criteria == ()


def test_acceptance_alone_is_enough_for_criteria(tmp_path):
    """这就是口述任务的形状：没有 spec_ref，只有 acceptance。"""
    p = tmp_path / "t.yaml"
    p.write_text(
        "task_id: T-spoken\nprompt: x\nacceptance:\n  - 返回值是 str\n",
        encoding="utf-8",
    )
    task = Task.from_yaml(p)
    assert task.spec_ref == ()
    assert task.criteria == ("返回值是 str",)
