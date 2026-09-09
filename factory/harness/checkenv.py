"""跑「用户/模型写的 shell」时该给什么环境（H-3）。

两个地方要用：`intake.checkgen.probe_check`（探针跑模型刚提议的 check）和
`supervisors.regression.run_check`（回归监工跑 task YAML 里的 check）。
两者跑的都是**没人逐字审过的 shell**，却都在继承工厂进程的完整环境。

实测泄漏：check 写成 `echo LEAKED=$FAKE_API_KEY` 能读到值。审计日志里会脱敏，
但那是**记录**层的脱敏 —— 值已经进了子进程，可以被写文件、发网络。

为什么不做命令白名单：check 的形态太多（`pytest -q && npm test`、
`cargo test --all --features x`、`./scripts/ci.sh`、`make check`），白名单要么
拦死正常用法（用户第一件事就是绕过它），要么宽到没有意义。真正的边界是
「这个进程能看到什么、能写什么」，不是「命令长什么样」。
"""

from __future__ import annotations

import os

#: 允许透传的环境变量名。只有「不给就跑不起来」的才进来。
#:
#: 刻意**不**包含：任何 *_KEY / *_TOKEN / *_SECRET / AWS_* / SSH_*、
#: PYPI_*、NPM_TOKEN、GITHUB_TOKEN、DATABASE_URL……
#: 也不含 SSH_AUTH_SOCK —— 有它就等于把 agent 里的私钥借给了 check。
_PASSTHROUGH = frozenset({
    # 基本
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TZ",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    # Python
    "PYTHONPATH", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE",
    "PYENV_ROOT", "UV_CACHE_DIR",
    # Node / 前端
    "NODE_PATH", "NVM_DIR", "NPM_CONFIG_CACHE",
    # 其他语言的工具链根目录（不含凭据）
    "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOROOT", "GOCACHE",
    "JAVA_HOME", "MAVEN_HOME", "GRADLE_USER_HOME",
    # CI 里常见的无害标记，测试可能据此调整行为
    "CI",
})

#: 即使名字过了白名单也要拦的模式（防 `PATH_TO_MY_SECRET` 这类）。
_DENY_FRAGMENTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
                   "CREDENTIAL", "AUTH", "COOKIE", "SESSION")


def check_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """给 check 命令用的最小环境。

    白名单式，不是黑名单式 —— 黑名单永远追不上新的凭据变量名（下个月
    多一个 ANTHROPIC_ADMIN_KEY，黑名单就漏了）。

    `extra` 用于显式追加（比如测试要注入 fixture 路径）。它不过白名单，
    调用方自己负责。
    """
    env = {}
    for name, value in os.environ.items():
        if name not in _PASSTHROUGH:
            continue
        upper = name.upper()
        if any(frag in upper for frag in _DENY_FRAGMENTS):
            continue    # 白名单里本不该有这种，但双重保险
        env[name] = value

    # VIRTUAL_ENV belongs to the host checkout, not a newly created worktree.
    # Explicit caller overrides remain available through extra.
    # PATH 必须有 —— 空 PATH 下连 sh 都找不到 pytest。
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")

    if extra:
        env.update(extra)
    return env
