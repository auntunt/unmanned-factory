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
