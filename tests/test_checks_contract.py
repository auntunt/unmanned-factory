"""`.checks.json` 契约：prompt 注入、读取、空判据识别。

这组测试守的是一个真实的断链：读取端（`_load_worktree_checks`）在 `ba72d46`
之后一直在等 worktree 里的 `.checks.json`，而 worker 从没被告知这个文件的
存在 —— `_prompt()` 第一轮只发 `task.prompt`。结果是所有无 check 任务确定
三轮全红在 no-checks-defined 上（audit.db 里两条实测轨迹，$4.96 / 900s 换回
一句「你没写判据」）。
"""

from __future__ import annotations

import json
from pathlib import Path

from factory.checks_contract import (
    CHECKS_FILENAME,
    contract_text,
    load,
    vacuous_checks,
)
from factory.task import CheckSpec, Task


def _c(command: str, *, expect: str = "exit_zero", value: str = "") -> CheckSpec:
    return CheckSpec(name="c", command=command, expect=expect, value=value)


# ---------- 契约文本必须真的把读取端的形状说全 ----------


def test_contract_names_the_file_the_reader_reads():
    """契约里必须出现读取端真正读的那个文件名。

    这条看着像同义反复，但它守的正是这次的 bug：两端各自正确、名字对不上
    （或一端压根没提），worker 就永远猜不到该往哪写。
    """
    text = contract_text()
    assert CHECKS_FILENAME in text


def test_contract_documents_every_expect_mode():
    """三种 expect 都要在契约里出现，否则 worker 只会用它猜到的那种。"""
    text = contract_text()
    for mode in ("exit_zero", "stdout_contains", "commands_agree"):
        assert mode in text, f"契约没说 {mode}"


def test_contract_example_json_actually_parses_into_checks():
    """契约里给的 JSON 例子必须能被 load() 吃下去。

    例子和解析器分头演化是文档类 bug 的常见形状 —— 这里直接把例子抠出来
    喂给真解析器，对不上就红。
    """
    text = contract_text()
    start = text.index("{", text.index("```json"))
    end = text.index("```", start)
    blob = text[start:end].strip()
    data = json.loads(blob)  # 例子本身得是合法 JSON
    assert data["checks"], "例子里得有 check"
    # 例子里的字段名必须是 load() 认的那些
    for c in data["checks"]:
        assert set(c) <= {"name", "command", "expect", "value", "timeout_s"}


def test_contract_warns_against_vacuous_checks():
    """契约必须明说什么不算判据。

    只说「请写判据」的话，最省力的满足方式是 `echo ok` —— 而那比没有 check
    更坏：no-checks-defined 会打回，伪造的绿会 merge。
    """
    text = contract_text()
    assert "echo ok" in text
    assert "|| true" in text


# ---------- load()：worker 写下的文件 ----------


def test_load_returns_empty_when_file_absent(tmp_path):
    assert load(tmp_path) == ()


def test_load_parses_all_fields(tmp_path):
    (tmp_path / CHECKS_FILENAME).write_text(json.dumps({"checks": [
        {"name": "unit", "command": "pytest -q"},
        {"name": "ver", "command": "t --version",
         "expect": "stdout_contains", "value": "1.2", "timeout_s": 30},
    ]}), encoding="utf-8")
    got = load(tmp_path)
    assert got == (
        CheckSpec(name="unit", command="pytest -q"),
        CheckSpec(name="ver", command="t --version",
                  expect="stdout_contains", value="1.2", timeout_s=30),
    )


def test_load_survives_garbage(tmp_path):
    """写坏了不抛，走 no-checks-defined 那条既有路径。

    抛异常会让整次派发以异常收场，审计记录都不完整 —— 而「worker 写坏了
    JSON」和「worker 没留判据」对人来说是同一件事，该拿到同一种打回。
    """
    for blob in ("{not json", "[]", '{"checks": "nope"}', '{"checks": [1, 2]}'):
        (tmp_path / CHECKS_FILENAME).write_text(blob, encoding="utf-8")
        assert load(tmp_path) == (), blob


def test_load_skips_entries_without_command(tmp_path):
    """没有 command 的条目跳过，不要让它变成一条空命令 check。

    空命令在 shell 里退出 0 —— 静默变成一条永真判据，正是这套机制要防的东西。
    """
    (tmp_path / CHECKS_FILENAME).write_text(json.dumps({"checks": [
        {"name": "empty"},
        {"name": "ok", "command": "pytest -q"},
    ]}), encoding="utf-8")
    assert load(tmp_path) == (CheckSpec(name="ok", command="pytest -q"),)


def test_load_defaults_missing_name(tmp_path):
    (tmp_path / CHECKS_FILENAME).write_text(
        json.dumps({"checks": [{"command": "pytest"}]}), encoding="utf-8")
    assert load(tmp_path)[0].name == "unnamed"


# ---------- vacuous_checks()：拦掉恒为真的判据 ----------


def test_always_true_commands_are_vacuous():
    for cmd in ("true", ":", "exit 0", "/bin/true", "echo ok", "echo 'done'"):
        assert vacuous_checks((_c(cmd),)), cmd


def test_existence_only_commands_are_vacuous():
    """只证明「文件在」不证明「文件对」。"""
    for cmd in ("ls", "ls -la dist/", "cat README.md", "test -f out.txt",
                "stat out.txt", "pwd", "date"):
        assert vacuous_checks((_c(cmd),)), cmd


def test_failure_swallowing_tails_are_vacuous():
    """`... || true` 把非零洗成 0，前面怎么红都无所谓。"""
    for cmd in ("pytest -q || true", "pytest ; true", "make test || :",
                "pytest -q ; exit 0", "pytest -q||true"):
        assert vacuous_checks((_c(cmd),)), cmd


def test_swallowing_tail_does_not_apply_to_stdout_contains():
    """stdout_contains 比对输出、不看退出码，所以吞退出码的尾巴吞不掉它。"""
    assert vacuous_checks(
        (_c("mytool --version || true", expect="stdout_contains", value="1.2"),)
    ) == ()


def test_real_checks_are_not_vacuous():
    """正当判据一条都不能被拦。误拦一切等于拦不住任何东西。"""
    for cmd in ("pytest -q", "python -m pytest tests/", "make test",
                "python -c 'import mypkg'", "npm test", "cargo test",
                "ruff check .", "./scripts/verify.sh"):
        assert vacuous_checks((_c(cmd),)) == (), cmd


def test_echo_inside_a_compound_command_is_not_vacuous():
    """`echo x && pytest` 的判定力在后半段，别按前缀否决。"""
    for cmd in ("echo start && pytest -q", "cat f | grep -q needle",
                "ls dist/ && python -m pytest"):
        assert vacuous_checks((_c(cmd),)) == (), cmd


def test_empty_command_is_vacuous():
    """空命令在 shell 里退出 0 —— 一条隐形的永真判据。"""
    assert vacuous_checks((_c(""),))
    assert vacuous_checks((_c("   "),))


def test_one_real_check_redeems_the_group():
    """一组里混一条 ls 配一条真测试是正当写法，不该整组否决。"""
    assert vacuous_checks((_c("ls dist/"), _c("pytest -q"))) == ()


def test_all_vacuous_group_is_reported_in_full():
    """全都恒为真时整组返回，好让打回理由能列出每一条。"""
    checks = (_c("echo ok"), _c("true"), _c("ls"))
    assert vacuous_checks(checks) == checks


def test_no_checks_is_not_vacuous():
    """空组走 no-checks-defined，不是 vacuous-checks —— 两种打回理由不同。"""
    assert vacuous_checks(()) == ()
