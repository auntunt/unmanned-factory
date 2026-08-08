import json
import stat
import subprocess
from pathlib import Path

import pytest

from factory.harness.base import ExitStatus, Limits
from factory.harness.claude_code import ClaudeCodeAdapter
from factory.task import Task


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")
    return ws


def _fake_claude(tmp_path, payload: dict, *, body: str = "") -> str:
    """造一个假 claude：把 argv 落盘、可选改 workspace、打印一段 JSON。"""
    script = tmp_path / "fake_claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, pathlib\n"
        f"argv_log = pathlib.Path({str(tmp_path / 'argv.json')!r})\n"
        "argv_log.write_text(json.dumps(sys.argv[1:]))\n"
        f"{body}\n"
        f"print(json.dumps({payload!r}))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


OK_PAYLOAD = {
    "is_error": False,
    "subtype": "success",
    "session_id": "sess-abc",
    "num_turns": 3,
    "total_cost_usd": 0.0042,
    "duration_ms": 5100,
    "usage": {"input_tokens": 120, "output_tokens": 340,
              "cache_creation_input_tokens": 10,
              "cache_read_input_tokens": 20},
    "result": "done",
}


def test_run_success_captures_everything(tmp_path, repo):
    body = (
        f"pathlib.Path({str(repo / 'greet.py')!r})"
        ".write_text('def greet(n):\\n    return n\\n')\n"
    )
    binary = _fake_claude(tmp_path, OK_PAYLOAD, body=body)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")

    res = adapter.run(
        Task(task_id="T-1", prompt="add greet"),
        repo,
        Limits(max_turns=5, timeout_s=30),
        model="haiku",
    )

    assert res.exit_status == ExitStatus.OK
    assert res.ok is True
    assert res.changed_paths == ("greet.py",)
    assert "def greet" in res.diff
    assert res.diff_hash and len(res.diff_hash) == 64
    assert res.session_id == "sess-abc"
    assert (res.tokens_in, res.tokens_out) == (120, 340)
    assert res.cost_usd == pytest.approx(0.0042)
    assert res.wall_clock_ms == 5100


def test_run_passes_model_and_max_turns(tmp_path, repo):
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    adapter.run(Task(task_id="T-1", prompt="hi"), repo,
                Limits(max_turns=7), model="sonnet")

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--output-format") + 1] == "json"


def test_run_omits_optional_flags_when_unset(tmp_path, repo):
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "--max-turns" not in argv
    assert "--model" not in argv


def test_is_error_true_maps_to_error_even_though_exit_code_is_zero(
    tmp_path, repo
):
    """claude -p 的退出码永远是 0，成功与否只能读 is_error。"""
    payload = {**OK_PAYLOAD, "is_error": True,
               "subtype": "error_max_turns", "result": "turn limit"}
    binary = _fake_claude(tmp_path, payload)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    assert res.exit_status == ExitStatus.ERROR
    assert res.ok is False
    assert "error_max_turns" in res.error_text


def test_unparseable_stdout_is_error(tmp_path, repo):
    script = tmp_path / "noisy_claude"
    script.write_text(
        "#!/usr/bin/env python3\nprint('not json at all')\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    adapter = ClaudeCodeAdapter(binary=str(script),
                                projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())
    assert res.exit_status == ExitStatus.ERROR
    assert "not json" in res.error_text


def test_timeout_maps_to_timeout_status(tmp_path, repo):
    script = tmp_path / "slow_claude"
    script.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    adapter = ClaudeCodeAdapter(binary=str(script),
                                projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo,
                      Limits(timeout_s=1))
    assert res.exit_status == ExitStatus.TIMEOUT


def test_missing_binary_is_error_not_crash(tmp_path, repo):
    adapter = ClaudeCodeAdapter(binary=str(tmp_path / "nonexistent-binary"),
                                projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())
    assert res.exit_status == ExitStatus.ERROR
    assert "cannot launch" in res.error_text


def test_finds_transcript_and_tool_calls(tmp_path, repo):
    proj = tmp_path / "proj" / "-some-lossy-slug"
    proj.mkdir(parents=True)
    (proj / "sess-abc.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit"}]}}) + "\n",
        encoding="utf-8",
    )
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    assert res.transcript_path == str(proj / "sess-abc.jsonl")
    assert [c.name for c in res.tool_calls] == ["Edit"]


def _cwd_recording_binary(tmp_path, log: str):
    """假 harness：把自己被执行时的 cwd 记到 log，并往 cwd 里写一个文件。

    真的 `claude --version` 只打印版本号，所以这个 bug 用真 binary 看不见。
    """
    script = tmp_path / "cwd_probe"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({log!r}).open('a').write(os.getcwd() + '\\n')\n"
        "pathlib.Path('LEAKED.txt').write_text('i was here\\n')\n"
        "print(json.dumps({'is_error': False}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_version_probe_never_runs_in_the_caller_cwd(tmp_path, monkeypatch):
    """version() 必须在空临时目录里探针，不能在调用方 cwd。

    没设 cwd 时，被探的可执行体会在**编排层自己的仓库**里跑一遍。
    实际后果：out.py 被 git add -A 提交进了 10a0d9f。
    """
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)

    log = str(tmp_path / "cwds.txt")
    adapter = ClaudeCodeAdapter(binary=_cwd_recording_binary(tmp_path, log))
    adapter.version()

    seen = [ln for ln in open(log, encoding="utf-8").read().splitlines() if ln]
    assert seen, "探针没跑起来，这个测试就没测到东西"
    for cwd in seen:
        assert Path(cwd).resolve() != caller.resolve()
        # 探针目录必须是干净的临时目录，且用完就没了
        assert not (caller / "LEAKED.txt").exists()
    assert list(caller.iterdir()) == [], "探针把文件写进了调用方 cwd"


def test_version_probe_dir_is_removed_after_the_call(tmp_path, monkeypatch):
    """探针目录用完即删，不靠 GC。长跑的工厂不能靠泄漏临时目录活着。"""
    monkeypatch.chdir(tmp_path / "..")
    log = str(tmp_path / "cwds2.txt")
    adapter = ClaudeCodeAdapter(binary=_cwd_recording_binary(tmp_path, log))
    adapter.version()

    probe_dir = Path(open(log, encoding="utf-8").read().splitlines()[0])
    assert not probe_dir.exists()


# ---------- 格式漂移（spec §10 风险 6）----------
#
# 这份 JSON 没有文档、没有版本号。而 adapter 里全是 `payload.get(k, 默认)`：
# 字段改名不会报错，会静默降级 —— 而且降级的方向全都是「看起来更好」：
# 花费变 0、失败变成功。所以漂移必须判红，不能只记个警告。

def test_a_renamed_cost_field_is_caught_not_silently_zero(tmp_path, repo):
    """total_cost_usd 改名 → 每次派发都记 $0 → 预算上限形同虚设。"""
    payload = dict(OK_PAYLOAD)
    payload["cost_usd_total"] = payload.pop("total_cost_usd")   # 改名
    r = ClaudeCodeAdapter(binary=_fake_claude(tmp_path, payload)).run(
        Task(task_id="T", prompt="x"), repo, Limits())

    assert r.exit_status is ExitStatus.ERROR, "读不懂的输出不能当成功"
    assert "total_cost_usd" in r.error_text
    assert "harness-format-drift" in r.error_text


def test_a_renamed_is_error_field_does_not_become_a_silent_success(
        tmp_path, repo):
    """最恶劣的一种：is_error 改名 → 失败的任务被当成功。

    没有这个检查的话，status 是 OK，任务一路走到监工、拿着一个可能是空的
    diff 判绿、然后 merge。而 `claude -p` 的退出码永远是 0，没有第二个
    信号能兜住。
    """
    payload = {k: v for k, v in OK_PAYLOAD.items() if k != "is_error"}
    payload["error"] = False
    r = ClaudeCodeAdapter(binary=_fake_claude(tmp_path, payload)).run(
        Task(task_id="T", prompt="x"), repo, Limits())

    assert r.exit_status is ExitStatus.ERROR
    assert "is_error" in r.error_text


def test_a_renamed_usage_field_is_caught(tmp_path, repo):
    payload = {k: v for k, v in OK_PAYLOAD.items() if k != "usage"}
    r = ClaudeCodeAdapter(binary=_fake_claude(tmp_path, payload)).run(
        Task(task_id="T", prompt="x"), repo, Limits())
    assert r.exit_status is ExitStatus.ERROR
    assert "usage" in r.error_text


def test_all_missing_fields_are_reported_at_once(tmp_path, repo):
    """一次报全。一个字段一个字段地发现等于每次升级 harness 都要撞好几轮。"""
    payload = {"session_id": "s", "result": "done"}
    r = ClaudeCodeAdapter(binary=_fake_claude(tmp_path, payload)).run(
        Task(task_id="T", prompt="x"), repo, Limits())
    for name in ("is_error", "total_cost_usd", "usage"):
        assert name in r.error_text


def test_a_zero_cost_is_not_drift(tmp_path, repo):
    """`total_cost_usd: 0` 完全正常（缓存命中、极短任务）。

    把 0 当漂移会让熔断器天天误报，而天天误报的熔断器等于被关掉的熔断器。
    判的是**键在不在**，不是值合不合理。
    """
    payload = dict(OK_PAYLOAD, total_cost_usd=0.0)
    r = ClaudeCodeAdapter(binary=_fake_claude(tmp_path, payload)).run(
        Task(task_id="T", prompt="x"), repo, Limits())
    assert r.exit_status is ExitStatus.OK
    assert "drift" not in r.error_text.lower()
    assert r.cost_usd == 0.0


def test_a_new_unknown_field_is_not_drift(tmp_path, repo):
    """harness 加字段是常事，不该判红 —— 只有承重字段**消失**才是问题。"""
    payload = dict(OK_PAYLOAD, some_new_thing={"a": 1})
    r = ClaudeCodeAdapter(binary=_fake_claude(tmp_path, payload)).run(
        Task(task_id="T", prompt="x"), repo, Limits())
    assert r.exit_status is ExitStatus.OK


# ---------- token 记账：usage 是个低估的数 ----------
#
# 加这几条之前，改 tokens 的来源不会让任何测试变红 —— 没有一条测试在测
# token 记账。而 token 是单位成本指标的分母。

def test_model_usage_wins_over_usage():
    """`usage` 只算最后一轮，`modelUsage` 是累计。真跑实测两边不一致：
    同一次调用 usage.in=27415，modelUsage 累计 77856。

    判 modelUsage 是权威的依据：它各模型 costUSD 之和与顶层 total_cost_usd
    **完全相等**（0.40520999999999996 两边一致），而 usage 和它对不上。
    """
    from factory.harness.drift import token_split, total_tokens

    p = {"usage": {"input_tokens": 27415, "output_tokens": 10},
         "modelUsage": {"m": {"inputTokens": 77856, "outputTokens": 249}}}
    assert token_split(p) == (77856, 249)
    assert total_tokens(p) == 78105


def test_the_supervisor_flag_combo_zeroes_usage_but_not_model_usage():
    """真跑抓到的那一种：监工带上四个独立性 flag 时 usage 全是 0。

    键在、值为 0 —— `missing_fields` 按设计看不见（判值会天天误报），
    所以这个洞只能在取数这一侧堵。原来监工那行读 usage，于是**所有**
    模型监工的 token 都记 0。
    """
    from factory.harness.drift import total_tokens

    p = {"usage": {"input_tokens": 0, "output_tokens": 0},
         "modelUsage": {"gpt-5.6-luna": {"inputTokens": 2719,
                                         "outputTokens": 249}}}
    assert total_tokens(p) == 2968


def test_usage_is_the_fallback_not_the_dead_end():
    """没有 modelUsage 时仍读 usage —— 不能因为换了来源就把老格式判成 0。"""
    from factory.harness.drift import token_split

    assert token_split({"usage": {"input_tokens": 5, "output_tokens": 3}}) == (5, 3)
    assert token_split({}) == (0, 0)
    assert token_split(None) == (0, 0)


def test_an_empty_model_usage_falls_back_instead_of_reporting_zero():
    """`modelUsage: {}` 要回落到 usage。

    只判「键在不在」的话空 dict 会让 token 记 0，而 usage 里明明有数。
    """
    from factory.harness.drift import token_split

    p = {"usage": {"input_tokens": 7, "output_tokens": 2}, "modelUsage": {}}
    assert token_split(p) == (7, 2)


def test_the_adapter_records_model_usage_tokens_end_to_end(tmp_path, repo):
    """接线测试：drift.token_split 真的接到了 AttemptResult 上。

    上面那几条只测了取数函数本身。函数对了但 adapter 还在读 usage 的话，
    它们全绿而记账照旧低估 —— 「一道谁都没接上的闸门在报表上和接上了
    长得一样」的又一次。
    """
    payload = dict(OK_PAYLOAD)
    payload["usage"] = {"input_tokens": 0, "output_tokens": 0}
    payload["modelUsage"] = {"m": {"inputTokens": 2719, "outputTokens": 249}}

    binary = _fake_claude(tmp_path, payload)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="x"), repo,
                      Limits(max_turns=5, timeout_s=30), model="haiku")

    assert (res.tokens_in, res.tokens_out) == (2719, 249)
