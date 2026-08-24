"""hook 客户端 + broker 的接线测试。

这一层的失效方式几乎全是**静默放行**：hook 起不来、socket 连不上、
判决 JSON 形状不对，表现都是「动作照常执行」，而我们自己的 gate.stats
可能连一条记录都没有 —— 从统计里看不出门断了。所以这里的测试重点不是
「deny 能拦住」，是「各种坏情况下行为是已知的」。
"""

from __future__ import annotations

import ast
import json
import os
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from pathlib import Path

import pytest

from factory.permission import hook
from factory.permission.broker import GateBroker
from factory.permission.gate import PermissionGate
from factory.permission.rules import Decision

HOOK_PY = Path(hook.__file__)


def _run_hook(payload: dict, env: dict[str, str]) -> str:
    """用**子进程**跑 hook，和真实调用路径一致。

    不直接调 hook.decide()：那样测不到「import 了 factory 会不会炸」，
    而那恰恰是沙箱模式下最容易坏的地方。
    """
    proc = subprocess.run(
        [sys.executable, str(HOOK_PY)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_hook_is_stdlib_only():
    """hook.py 绝不能 import factory.*。

    沙箱把工厂代码 tmpfs 掉了，一旦 import 就在沙箱模式下必然失败，
    而失败的表现是静默放行 —— 也就是没人会来报的那一侧。

    用 AST 而不是子串搜 "import factory"：文档字符串里就在解释这条规则
    （"一旦 import factory 就……"），子串匹配会被自己的注释绊倒。
    第一版就是这么挂的 —— 而那种假红会让人倾向于把断言放宽，
    真出问题时反而看不见。
    """
    tree = ast.parse(HOOK_PY.read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported, "解析不到任何 import，测试自己坏了"
    offenders = [m for m in imported if m.split(".")[0] == "factory"]
    assert not offenders, f"hook.py 不能依赖工厂代码：{offenders}"


def test_hook_runs_under_system_python():
    """用 /usr/bin/python3（没装本项目依赖）也能跑起来。

    这是上面那条规则的**行为验证**：光 grep import 不够，间接依赖也会炸。
    沙箱模式下 claude_code 就是拿这个解释器起 hook 的。
    """
    if not Path("/usr/bin/python3").exists():
        pytest.skip("no /usr/bin/python3")
    proc = subprocess.run(
        ["/usr/bin/python3", str(HOOK_PY)],
        input='{"tool_name":"Read","tool_input":{}}',
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != hook.SOCKET_ENV},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_socket_env_allows_silently():
    """没有 socket 环境变量 → 空输出（放行）。不能崩、不能拦。"""
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}},
        {},
    )
    assert out == ""


def test_dead_socket_allows_silently():
    """socket 路径存在但没人listen → 放行（fail-open）。

    fail-closed 的代价是一个 socket 抖动让所有无人值守任务全灭，
    比放过一个动作贵得多。
    """
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}},
        {hook.SOCKET_ENV: "/tmp/definitely-not-a-socket-12345"},
    )
    assert out == ""


def test_broker_denies_dangerous_command():
    """真 broker + 真子进程 hook：危险命令拿到 deny JSON。"""
    gate = PermissionGate()
    with GateBroker(gate) as broker:
        out = _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD"}},
            {hook.SOCKET_ENV: broker.socket_path},
        )
    payload = json.loads(out)
    spec = payload["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    assert spec["permissionDecisionReason"]
    assert gate.stats.denied == 1


def test_broker_allows_safe_command_without_emitting_allow():
    """安全命令 → **空输出**，不是显式 allow。

    显式回 permissionDecision:"allow" 会跳过 CLI 自己后续的权限检查，
    等于这道补充的门把主防线关掉了。空输出 = 交回正常流程。
    """
    gate = PermissionGate()
    with GateBroker(gate) as broker:
        out = _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
            {hook.SOCKET_ENV: broker.socket_path},
        )
    assert out == ""
    assert gate.stats.allowed == 1


def test_broker_denies_protected_path_write():
    """写受保护路径（闸门自己）→ deny。"""
    gate = PermissionGate()
    with GateBroker(gate) as broker:
        out = _run_hook(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "oracle_rules.yaml", "content": "x"},
            },
            {hook.SOCKET_ENV: broker.socket_path},
        )
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert gate.stats.denied == 1


def test_unknown_tool_allowed():
    """不认识的工具放行。白名单式拦截会让新版本 CLI 的无害工具卡死流水线。"""
    gate = PermissionGate()
    with GateBroker(gate) as broker:
        out = _run_hook(
            {"tool_name": "SomeFutureTool", "tool_input": {"x": 1}},
            {hook.SOCKET_ENV: broker.socket_path},
        )
    assert out == ""


def test_settings_json_shape():
    """--settings 的形状必须是 CLI 认的那个（matcher + hooks[].command）。"""
    # 断言必须在 with 里：broker 退出会把 socket 目录连 hook 脚本一起删掉，
    # 出了块再检查文件存在必然假红。
    with GateBroker(PermissionGate()) as broker:
        cfg = json.loads(broker.settings_json())
        entry = cfg["hooks"]["PreToolUse"][0]
        assert entry["matcher"] == "*"
        inner = entry["hooks"][0]
        assert inner["type"] == "command"
        # command 必须指向已复制出去的 hook 脚本，且文件真的在
        script = inner["command"].split()[-1]
        assert Path(script).exists()


def test_broker_cleans_up_after_exit():
    """退出后 socket 目录清掉，但 stats 留着给 dispatcher 落库。"""
    gate = PermissionGate()
    with GateBroker(gate) as broker:
        d = broker.mount_dir
        _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "git clean -fd"}},
            {hook.SOCKET_ENV: broker.socket_path},
        )
        assert d.exists()
    assert not d.exists()
    # 事件必须活过 broker 的生命周期
    assert gate.stats.denied == 1
    assert gate.stats.events[0].decision == Decision.DENY


def test_hook_interpreter_survives_the_sandbox():
    """hook 的解释器不能落在 factory_root 下面。

    沙箱第 2 段 `--tmpfs <factory_root>` 把整个仓库连 .venv 一起盖掉。
    如果 hook 命令用 sys.executable（= `<repo>/.venv/bin/python`），
    沙箱里那个路径不存在 → hook 启动失败 → **静默放行**。

    这个失效方式极其隐蔽：settings 里有 hook、socket 也绑进沙箱了，
    看起来门装好了，实际一个动作都拦不住。实测踩过。
    """
    repo_root = Path(__file__).resolve().parent.parent
    with GateBroker(PermissionGate()) as broker:
        cmd = json.loads(broker.settings_json())["hooks"]["PreToolUse"][0]
        interp = Path(cmd["hooks"][0]["command"].split()[0])

        assert interp.is_absolute(), f"解释器必须绝对路径，得到 {interp}"
        assert interp.exists(), f"解释器不存在：{interp}"
        # 核心断言：解释器不能在会被 tmpfs 盖掉的仓库目录下
        assert repo_root not in interp.parents, (
            f"解释器 {interp} 在 factory_root 下，沙箱里会被 tmpfs 盖掉 "
            f"→ hook 起不来 → 静默放行"
        )


def test_concurrent_checks_lose_no_events():
    """并发工具调用下一条审计事件都不能丢。

    broker 每个连接开一个线程，GateStats.record 会被并发调用，而它要改
    四个计数器 + append 列表 —— 不加锁就会丢记录，丢的正是审计要看的
    非 ALLOW 事件。这条测试锁住 broker._lock 那条串行化路径。

    adapter.drain_gate_events 曾经绕过 broker 直接读 gate.stats，
    等于绕过了这把锁，改成委托 broker.drain_events() 了。
    """
    n = 12
    gate = PermissionGate()
    with GateBroker(gate) as broker:
        env = {hook.SOCKET_ENV: broker.socket_path}
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git reset --hard"},
        }
        with ThreadPoolExecutor(max_workers=n) as pool:
            outs = list(pool.map(lambda _: _run_hook(payload, env), range(n)))

        # 每个请求都必须拿到 deny，没有一个被漏judge
        assert all(
            json.loads(o)["hookSpecificOutput"]["permissionDecision"] == "deny"
            for o in outs
        )
        assert gate.stats.denied == n
        assert len(broker.drain_events()) == n


def test_drain_events_clears_and_shapes():
    """drain 后清空，且形状对齐 store.record_permission_event 的 kwargs。"""
    gate = PermissionGate()
    with GateBroker(gate) as broker:
        _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}},
            {hook.SOCKET_ENV: broker.socket_path},
        )
        events = broker.drain_events()
        assert len(events) == 1
        assert set(events[0]) == {
            "tool",
            "target",
            "decision",
            "rule",
            "reason",
            "tokens",
            "cost_usd",
        }
        assert events[0]["decision"] == "deny"
        # 再 drain 一次必须是空的：不清空会让下一轮 attempt 重复落库
        assert broker.drain_events() == ()
