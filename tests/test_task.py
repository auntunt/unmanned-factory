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


def test_criteria_gives_the_spec_body_not_the_bare_number(tmp_path):
    """spec_ref 里是编号，递给监工的必须是**正文**。

    这条测试原来断言的正是那个 bug：`criteria == ("AC-1", ...)`。递一行
    `- AC-1` 给规格监工等于没递标准 —— 它不知道 AC-1 要求什么。
    """
    (tmp_path / "prd.md").write_text(
        "## 验收标准\n- AC-1: 超过 n 个词时截断并以 '...' 结尾\n",
        encoding="utf-8",
    )
    p = tmp_path / "t.yaml"
    p.write_text(
        "task_id: T-1\nprompt: x\n"
        "spec_ref: [AC-1]\nspec_doc: prd.md\n"
        "acceptance:\n  - 不超过时原样返回\n",
        encoding="utf-8",
    )
    task = Task.from_yaml(p)
    assert task.spec_ref == ("AC-1",)          # 原始字段不变
    assert task.spec_doc == "prd.md"

    got = task.criteria(tmp_path)
    assert got == ("AC-1: 超过 n 个词时截断并以 '...' 结尾", "不超过时原样返回")
    # 编号留在正文里：人要能对回 PRD 的哪一条
    assert got[0].startswith("AC-1")
    # 而裸编号不能单独成为一条标准
    assert "AC-1" not in [c.strip() for c in got]


def test_criteria_empty_when_neither_given(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("task_id: T-1\nprompt: x\n", encoding="utf-8")
    assert Task.from_yaml(p).criteria(tmp_path) == ()


def test_acceptance_alone_is_enough_for_criteria(tmp_path):
    """这就是口述任务的形状：没有 spec_ref，只有 acceptance。

    它不需要 spec_doc —— acceptance 本身就是正文。
    """
    p = tmp_path / "t.yaml"
    p.write_text(
        "task_id: T-spoken\nprompt: x\nacceptance:\n  - 返回值是 str\n",
        encoding="utf-8",
    )
    task = Task.from_yaml(p)
    assert task.spec_ref == ()
    assert task.criteria(tmp_path) == ("返回值是 str",)
    assert task.resolve_spec(tmp_path).ok, "没有 spec_ref 就没有可失败的解析"


def test_a_spec_ref_without_a_doc_does_not_resolve(tmp_path):
    """光有编号 = 没有标准。这是 dispatcher 拦它的依据。"""
    p = tmp_path / "t.yaml"
    p.write_text("task_id: T-1\nprompt: x\nspec_ref: [AC-1]\n", encoding="utf-8")
    got = Task.from_yaml(p).resolve_spec(tmp_path)
    assert not got.ok
    assert "没有 spec_doc" in got.doc_error
    assert got.bodies == ()
