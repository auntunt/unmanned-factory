"""check 命令的环境隔离（H-3 回归测试）。

原问题：probe_check 和 regression._sh 都以 shell=True 跑「没人逐字审过的
shell」，且不传 env —— 子进程继承工厂进程的完整环境。实测
`echo LEAKED=$FAKE_API_KEY` 能读到值；审计日志里会脱敏，但那是记录层的
脱敏，值已经进了子进程，可以被写文件或发网络。
"""

import tempfile
from pathlib import Path

import pytest

from factory.harness.checkenv import check_env

# 每一项都是真实世界里会出现在工厂进程环境里的凭据形态
CREDENTIAL_VARS = {
    "FAKE_API_KEY": "sk-leak-1",
    "ANTHROPIC_API_KEY": "sk-ant-leak-2",
    "OPENAI_API_KEY": "sk-openai-leak-3",
    "AWS_SECRET_ACCESS_KEY": "aws-leak-4",
    "AWS_SESSION_TOKEN": "aws-token-leak-5",
    "GITHUB_TOKEN": "ghp_leak_6",
    "NPM_TOKEN": "npm-leak-7",
    "DATABASE_URL": "postgres://u:pw@h/db",
    "MY_APP_SECRET": "leak-8",
    "SOME_PASSWORD": "leak-9",
    "SESSION_COOKIE": "leak-10",
    # SSH agent socket：有它等于把 agent 里的私钥借给 check
    "SSH_AUTH_SOCK": "/tmp/ssh-agent-leak.sock",
}

# 不给就跑不起来的
REQUIRED_VARS = ("PATH", "HOME")


@pytest.fixture
def polluted_env(monkeypatch):
    """把假凭据塞进 os.environ，模拟真实工厂进程。"""
    for name, value in CREDENTIAL_VARS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    return CREDENTIAL_VARS


def test_credentials_are_stripped(polluted_env):
    """所有凭据形态的变量都不能出现在 check 环境里。"""
    env = check_env()
    leaked = sorted(name for name in CREDENTIAL_VARS if name in env)
    assert not leaked, f"凭据泄漏到 check 环境：{leaked}"


def test_credential_values_absent(polluted_env):
    """不只是变量名 —— 值也不能以任何形式出现（防换名透传）。"""
    env = check_env()
    blob = "\n".join(f"{k}={v}" for k, v in env.items())
    for name, value in CREDENTIAL_VARS.items():
        assert value not in blob, f"{name} 的值出现在 check 环境里"


def test_required_vars_survive(polluted_env):
    """剥离不能剥到跑不起来 —— 空 PATH 下连 pytest 都找不到。"""
    env = check_env()
    for name in REQUIRED_VARS:
        assert name in env, f"必需变量 {name} 被剥掉了"
    assert env["PATH"], "PATH 是空的"


def test_path_has_fallback(monkeypatch):
    """宿主没有 PATH 时也要给一个能用的默认值。"""
    monkeypatch.delenv("PATH", raising=False)
    env = check_env()
    assert env.get("PATH"), "PATH 缺失时没有兜底"


def test_deny_fragment_beats_whitelist(monkeypatch):
    """名字里带 KEY/TOKEN 的，即使碰巧在白名单里也要拦。

    防的是以后有人往 _PASSTHROUGH 里加了个看起来无害、实际带凭据的名字。
    """
    # PYTHONPATH 在白名单里；构造一个白名单内但含 deny 片段的名字
    monkeypatch.setenv("CI", "true")           # 白名单，无 deny 片段 → 应保留
    env = check_env()
    assert env.get("CI") == "true"
    for name in env:
        upper = name.upper()
        for frag in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"):
            assert frag not in upper, f"{name} 带 {frag} 却过了过滤"


def test_extra_is_applied(polluted_env):
    """extra 显式追加要生效（测试注入 fixture 路径用）。"""
    env = check_env({"MY_FIXTURE_DIR": "/tmp/fixtures"})
    assert env["MY_FIXTURE_DIR"] == "/tmp/fixtures"


def test_env_is_much_smaller_than_host(polluted_env):
    """整体形状检查：白名单式过滤后应该远小于宿主环境。"""
    import os
    env = check_env()
    assert len(env) < len(os.environ), "过滤后不比宿主小，白名单可能没生效"


# ---------- 端到端：真的跑一条 check ----------


def test_probe_check_cannot_read_credentials(polluted_env):
    """走 probe_check 真跑一条读凭据的 shell，必须读不到。

    这是本文件最重要的一条：check_env() 单元测试通过、但调用点忘了传 env
    的话，上面那些断言全绿而洞还在。
    """
    from factory.intake.checkgen import probe_check

    with tempfile.TemporaryDirectory() as td:
        probe = probe_check(
            {"command": 'echo "VAL=$FAKE_API_KEY"', "expect": "exit_zero"},
            Path(td),
            timeout_s=30,
        )
        assert "sk-leak-1" not in repr(probe), "凭据值出现在探针结果里"


def test_regression_check_cannot_read_credentials(polluted_env, tmp_path):
    """回归监工侧的同一条断言。"""
    from factory.supervisors.regression import _sh

    done = _sh('echo "VAL=$AWS_SECRET_ACCESS_KEY"', tmp_path, 30)
    assert "aws-leak-4" not in (done.stdout or ""), "凭据值被 check 读到了"
