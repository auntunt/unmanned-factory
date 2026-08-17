"""Linux 沙箱后端：bubblewrap (bwrap) 包装 worker argv。

设计对标 sandbox.py（macOS Seatbelt），接口形状一致：
  - available() -> bool
  - prepare(argv, workspace, stack) -> (wrapped_argv, env_overrides)
  - tag_version(raw, enabled) -> str
  - SANDBOX_BINARY: str
  - SandboxUnavailable

**挂载顺序是正确性的一部分。** bwrap 按 argv 顺序依次施加挂载，后面的盖前面的。
所以顺序必须是：先 ro-bind 根 → 再 tmpfs 遮蔽（/home、工厂代码）→ 最后 bind 放开
可写点。反过来写的话，tmpfs 会把 workspace 盖没 —— 已实测：worker 报
"Directory nonexistent"，一个字都写不出来，而 bwrap 本身退出码为 0。
这跟 Seatbelt 那边"策略加载成功但少一条禁令"是同一类失效：包装器不报错，
隔离却是错的。本模块用 _MOUNT_PHASES 把顺序固化，并有测试盯住它。

Ubuntu 24.04 的 AppArmor 限制：
  `kernel.apparmor_restrict_unprivileged_userns = 1` 会拦掉非特权 userns，
  bwrap 报 "setting up uid map: Permission denied"。发行版包不带 profile，
  需要自己装一个（见 docs/deploy-linux.md）。available() 因此不能只看文件存在，
  必须真跑一次 --unshare-user 才算数。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

SANDBOX_BINARY = "/usr/bin/bwrap"
VERSION_TAG = "+bwrap"

# 探测 available() 的超时。bwrap 启动是毫秒级，5s 足够宽；
# 真卡住说明环境有问题，不该让它拖住整个派发。
_PROBE_TIMEOUT_S = 5.0


class SandboxUnavailable(RuntimeError):
    """bwrap 不可用或无法创建 user namespace。调用方要么降级要么中止。"""


def available() -> bool:
    """bwrap 是否可用**且能真正隔离**。

    只查 `Path.exists()` 不够：AppArmor 拦 userns 时 bwrap 装着也跑不起来。
    所以真跑一次最小 --unshare-user，退出码为 0 才算可用。
    """
    if not Path(SANDBOX_BINARY).exists():
        return False
    try:
        proc = subprocess.run(
            [SANDBOX_BINARY, "--unshare-user", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def git_dir(workspace: Path) -> Path | None:
    """workspace 对应的**公共** .git 目录。

    与 sandbox.py 同源：worktree 里的 `.git` 是指向父仓库的文件，worker 一执行
    `git add` 就要写父仓库的 index.lock / refs。用 --git-common-dir 而非
    --git-dir：后者在 worktree 里返回 `.git/worktrees/<name>`，缺 objects 和 refs。
    """
    proc = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    p = Path(raw)
    return p.resolve() if p.is_absolute() else (Path(workspace) / p).resolve()


def tag_version(raw: str, enabled: bool) -> str:
    """把"这次跑有没有开沙箱"写进 harness_version。

    与 Seatbelt 版用不同后缀（+bwrap vs +sandbox）：审计里要能分清是哪种隔离，
    两者的保证强度不同（bwrap 有 pid/ipc/uts 命名空间，Seatbelt 没有；
    Seatbelt 能做细粒度路径规则，bwrap 只有挂载粒度）。
    harness_version 列宽 64，截断保证不写爆。
    """
    if not enabled:
        return raw[:64]
    return (raw[: 64 - len(VERSION_TAG)] + VERSION_TAG)[:64]


def build_argv(
    argv: list[str],
    workspace: Path,
    tmp_dir: Path,
    *,
    factory_root: Path | None = None,
    git_common: Path | None = None,
) -> list[str]:
    """拼出完整的 bwrap 命令行。独立出来是为了让测试能直接检查顺序。

    三段式，顺序不可换（见模块 docstring）：
      1. 根只读 + 命名空间
      2. tmpfs 遮蔽（/home、/root、工厂代码）
      3. bind 放开可写点（workspace、tmp、git-common-dir）

    第 3 段必须在第 2 段之后：workspace 常常就在工厂仓库底下
    （worktree 池默认 `<repo>/.factory-worktrees/<slug>`），先 bind 后 tmpfs
    会让 workspace 消失。
    """
    ws = Path(workspace).resolve()
    tmp = Path(tmp_dir).resolve()
    root = Path(factory_root).resolve() if factory_root else Path(__file__).resolve().parents[2]

    # --- 第 1 段：命名空间 + 根只读 ---
    # 不 --unshare-net：worker 必须能连 API。与 Seatbelt 版的取舍一致，
    # 这一层管文件副作用，不管网络。
    out = [
        SANDBOX_BINARY,
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
    ]

    # --- 第 2 段：tmpfs 遮蔽 ---
    # /home 整个盖掉而不是只盖 $HOME：worker 不该看见任何用户的家目录。
    # 工厂代码盖掉的理由同 Seatbelt 版的 denied：worker 有权改代码，而
    # oracle_rules.yaml 里的 D 类硬闸门也是代码。能改它就等于下次派发没闸门。
    out += ["--tmpfs", "/home", "--tmpfs", "/root"]
    if root.exists():
        out += ["--tmpfs", str(root)]

    # --- 第 3 段：放开可写点（必须最后）---
    out += ["--bind", str(ws), str(ws)]
    out += ["--bind", str(tmp), str(tmp)]
    common = git_common if git_common is not None else git_dir(ws)
    if common:
        out += ["--bind", str(common), str(common)]

    return [*out, *argv]


def prepare(argv: list[str], workspace: Path, stack: object) -> tuple[list[str], dict[str, str]]:
    """adapter 用的入口：给 argv 套沙箱，并给出要覆盖的环境变量。

    返回 env 覆盖而不是直接改 os.environ：TMPDIR 必须指到本次派发私有的目录，
    并且沙箱里只 bind 了这一个 tmp —— 指向别处的话 worker 的工具链
    （pytest、编译器、包管理器）会往只读的 /tmp 写，失败信息跟"沙箱"毫无字面关联。
    """
    if not available():
        raise SandboxUnavailable(
            f"{SANDBOX_BINARY} 不可用或无法创建 user namespace。\n"
            "  1) 装：sudo apt-get install -y bubblewrap\n"
            "  2) Ubuntu 24.04 还需豁免 AppArmor 的 userns 限制"
            "（sysctl kernel.apparmor_restrict_unprivileged_userns=1 时必须做）：\n"
            "     见 docs/deploy-linux.md 的 bwrap profile 一节。\n"
            "  不静默降级：要么修好，要么显式 --no-sandbox。"
        )

    ws = Path(workspace).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="factory-work-"))
    cleanup = getattr(stack, "callback", None)
    if cleanup:
        cleanup(shutil.rmtree, tmp, ignore_errors=True)

    return build_argv(argv, ws, tmp), {"TMPDIR": str(tmp)}
