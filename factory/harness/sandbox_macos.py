"""macOS 沙箱后端：把 worker 的 argv 包一层 Seatbelt（`sandbox-exec`）。

本模块原是 sandbox.py 的全部内容。补 Linux 后端时拆出来，sandbox.py 变成
平台分发层。接口形状与 sandbox_linux 一致（available / prepare / tag_version /
SANDBOX_BINARY / SandboxUnavailable），额外导出 macOS 专有的 SandboxPolicy、
policy_for、wrap、git_dir —— 测试直接用它们检查策略文本。

worktree 隔离的是**工作树**，这里隔离的是**副作用** —— 装包、写 workspace
以外的路径、改系统配置。两者是不同的失效模式，都要有。

选 Seatbelt 不选 Docker 的理由（两者本机都可用，已实测）：
  - Docker 要把 worker 二进制**和它的认证**烤进镜像。`claude` 的凭据在
    Keychain 里，镜像里没有 Keychain —— 等于要另发一套凭据进容器，
    为了隔离副作用反而多造了一个密钥分发面。
  - Seatbelt 是 argv 前缀，adapter 不用改结构，两个 harness 都能接。
    已实测：真 `claude -p` 在策略下正常完成任务，文件落在 workspace 里。
  - 代价说清楚：Seatbelt 只管**文件系统与进程**，不管网络。worker 必须能
    连 API，所以网络是放开的。要断网得换容器 —— 那是另一个决定。

**这一层不是 D 类硬闸门的替代，也不能成为它的理由。**
硬闸门在派发**之前**判，压根不启动 worker；沙箱在 worker **运行时**兜底。
不允许出现「有沙箱了所以 D 类可以放宽」的推理：沙箱挡不住已经拿到
生产凭据的进程去调用远端 API，它只挡本机文件。两层各挡各的。

策略里的路径一律走 `-D` 参数，不做字符串拼接。拼接的话，一个含 `"` 或 `)`
的目录名就能提前闭合 sbpl 表达式，把 `(deny ...)` 挤出策略 —— 那正好是
最危险的一种失效：策略仍然加载成功，但少了一条禁令。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SANDBOX_BINARY = "/usr/bin/sandbox-exec"
VERSION_TAG = "+sandbox"

# 最后匹配的规则胜出（已实测），所以顺序有意义：
#   allow default → deny file-write* → 逐个 allow 回来 → 最后 deny 黑名单。
# 黑名单放最后，才能盖掉前面 allow 的子树。
_PROFILE = """(version 1)
(allow default)
(deny file-write*)
{allows}
{denies}
"""

_DEV_WRITES = (
    "/dev/null",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/dtracehelper",
    "/dev/tty",
    "/dev/urandom",
)


class SandboxUnavailable(RuntimeError):
    """本机不支持沙箱。调用方要么降级要么中止，不许静默跳过。"""


def available() -> bool:
    """`sandbox-exec` 是否可用。非 macOS 上恒为 False。"""
    return Path(SANDBOX_BINARY).exists()


@dataclass(frozen=True)
class SandboxPolicy:
    """一次派发的写权限白名单。

    workspace 必须给绝对路径：sbpl 的 subpath 不认相对路径，给相对路径会
    静默匹配不到任何东西 —— 策略照样加载，worker 却一个字都写不出来。
    """

    workspace: Path
    writable: tuple[Path, ...] = ()
    denied: tuple[Path, ...] = ()
    allow_tmp: bool = True
    # 给这次派发专用的临时目录。为 None 时退回放开**整个** TMPDIR 根 ——
    # 那是个真的洞：/var/folders 下住着所有别的任务的临时目录，并行派发时
    # 一个 worker 能写进另一个 worker 的临时文件。实测发现（测试里
    # pytest 的 tmp_path 就在那底下，越界写居然成功了）。
    # 所以 adapter 一律传私有目录进来，这个 None 分支只为单独用本模块的人保留。
    tmp_dir: Path | None = None

    def __post_init__(self) -> None:
        if not self.workspace.is_absolute():
            raise ValueError(f"workspace 必须是绝对路径：{self.workspace}")
        for p in (*self.writable, *self.denied):
            if not Path(p).is_absolute():
                raise ValueError(f"沙箱策略里的路径必须是绝对路径：{p}")
    def _write_roots(self) -> tuple[Path, ...]:
        roots = [self.workspace.resolve(), *(Path(p).resolve() for p in self.writable)]
        if self.allow_tmp:
            # worker 和它调的工具链（pytest、编译器、包管理器）都往 TMPDIR 写。
            # 不放开的话失败方式很难认：报错来自某个子工具的临时文件，
            # 跟"沙箱"三个字毫无字面关联。
            roots.append(
                (self.tmp_dir or Path(tempfile.gettempdir())).resolve()
            )
        seen: dict[str, Path] = {}
        for r in roots:
            seen.setdefault(str(r), r)
        return tuple(seen.values())

    def profile(self) -> tuple[str, tuple[str, ...]]:
        """返回 (sbpl 源文本, -D 参数列表)。路径只经 -D 传，绝不进模板。"""
        params: list[str] = []
        allow_lines: list[str] = []

        for i, root in enumerate(self._write_roots()):
            key = f"W{i}"
            params += ["-D", f"{key}={root}"]
            allow_lines.append(f'(allow file-write* (subpath (param "{key}")))')

        devs = " ".join(f'(literal "{d}")' for d in _DEV_WRITES)
        allow_lines.append(f"(allow file-write* {devs})")

        deny_lines: list[str] = []
        for i, root in enumerate(Path(p).resolve() for p in self.denied):
            key = f"D{i}"
            params += ["-D", f"{key}={root}"]
            deny_lines.append(f'(deny file-write* (subpath (param "{key}")))')

        src = _PROFILE.format(
            allows="\n".join(allow_lines), denies="\n".join(deny_lines)
        )
        return src, tuple(params)


def git_dir(workspace: Path) -> Path | None:
    """workspace 对应的**公共** .git 目录。

    worktree 里的 `.git` 是个指向父仓库的文件，不是目录。worker 一执行
    `git add`（capture_diff 也依赖它）就要写父仓库的 index.lock / refs，
    只放开 workspace 会让 git 直接 fatal —— 已实测。
    用 --git-common-dir 而不是 --git-dir：后者在 worktree 里返回的是
    `.git/worktrees/<name>`，缺了 objects 和 refs，commit 仍然会失败。
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


def policy_for(
    workspace: Path,
    *,
    factory_root: Path | None = None,
    extra_writable: tuple[Path, ...] = (),
    tmp_dir: Path | None = None,
) -> SandboxPolicy:
    """给一个 workspace 配默认策略。

    factory_root 默认是本包所在的仓库，并且进 denied 而不是"不加进 allow" ——
    这是本模块唯一一条黑名单，理由值得写下来：worker 有权改代码，而编排层
    自己的代码（分级规则、dispatcher）也是代码。放着不管，一个跑飞的 worker
    能把 oracle_rules.yaml 里的 D 类规则删掉，下一次派发就没有硬闸门了。
    白名单本来也不覆盖它，但如果有人日后把 factory_root 加进 extra_writable
    或者 workspace 恰好是工厂仓库本身，这条 deny 会在最后一行盖回来。
    """
    ws = Path(workspace).resolve()
    writable = list(extra_writable)

    common = git_dir(ws)
    if common:
        writable.append(common)

    # worker 的会话记录（transcript）落在 ~/.claude/projects 下，而审计要靠它
    # 还原 tool_calls。不放开的话不会报错 —— transcript_path 变成 None，
    # tool_calls 静默变空。**审计悄悄少了东西比大声失败更糟**，所以必须放开。
    # 只放 projects 子目录，不放整个 ~/.claude：settings.json 在那里，能写它
    # 就能塞 hook，等于在编排层下一次启动时任意执行代码。已实测这条边界成立
    # （projects 可写、settings.json 被拒）。
    projects = Path.home() / ".claude" / "projects"
    if projects.is_dir():
        writable.append(projects.resolve())

    root = Path(factory_root).resolve() if factory_root else Path(__file__).resolve().parents[2]
    return SandboxPolicy(
        workspace=ws,
        writable=tuple(writable),
        denied=(root,),
        tmp_dir=Path(tmp_dir).resolve() if tmp_dir else None,
    )


def wrap(argv: list[str], policy: SandboxPolicy, *, stack: object) -> list[str]:
    """把 argv 包成 sandbox-exec 调用。

    策略写成临时文件而不是用 `-p` 传字面量：sbpl 有多行结构，塞进单个 argv
    容易被 shell/引号规则改写，而策略被悄悄改写正是最坏的失效。
    stack 收 contextlib.ExitStack，由调用方决定文件活多久 —— 进程还在跑就删
    策略文件的话，Seatbelt 已经加载完了不会报错，但排查时看不到当时的策略。
    """
    if not available():
        raise SandboxUnavailable(
            f"{SANDBOX_BINARY} 不存在（非 macOS?）。要么关掉沙箱，要么换容器。"
        )
    src, params = policy.profile()
    path = _write_profile(src, stack)
    return [SANDBOX_BINARY, "-f", path, *params, *argv]


VERSION_TAG = "+sandbox"


def tag_version(raw: str, enabled: bool) -> str:
    """把"这次跑有没有开沙箱"写进 harness_version。

    审计里必须留痕，否则 `merged` 长得一模一样，事后没人分得清哪些产出是在
    隔离下拿到的。选加后缀而不是加一列：加列要改 schema，而改 schema 是
    D 类不可逆操作 —— 为了记一个布尔值去动硬闸门管辖的东西不值得。
    harness_version 列宽 64，所以截断保证不会写爆。
    """
    if not enabled:
        return raw[:64]
    return (raw[: 64 - len(VERSION_TAG)] + VERSION_TAG)[:64]


def prepare(
    argv: list[str],
    workspace: Path,
    stack: object,
    *,
    extra_writable: tuple[Path, ...] = (),
) -> tuple[list[str], dict[str, str]]:
    """adapter 用的入口：给 argv 套沙箱，并给出要覆盖的环境变量。

    返回 env 覆盖而不是直接改 os.environ：TMPDIR 必须指到本次派发私有的目录，
    否则 worker 的工具链会去写 /var/folders 根下 —— 那里住着所有别的任务的
    临时目录，并行派发时互相可写。同时策略也只放开这个私有目录。

    extra_writable 走 policy_for 的同名参数（权限门 broker 目录用它）。
    签名必须和 sandbox_linux.prepare 一致：sandbox.py 那层是按名字转发的，
    这边少一个关键字参数的话，Linux 上能跑的调用到了 macOS 直接 TypeError。
    """
    tmp = Path(tempfile.mkdtemp(prefix="factory-work-"))
    cleanup = getattr(stack, "callback", None)
    if cleanup:
        cleanup(shutil.rmtree, tmp, ignore_errors=True)
    policy = policy_for(
        Path(workspace), tmp_dir=tmp, extra_writable=tuple(extra_writable)
    )
    return wrap(argv, policy, stack=stack), {"TMPDIR": str(tmp)}


def _write_profile(src: str, stack: object) -> str:
    tmpdir = tempfile.mkdtemp(prefix="factory-sb-")
    cleanup = getattr(stack, "callback", None)
    if cleanup:
        cleanup(shutil.rmtree, tmpdir, ignore_errors=True)
    path = Path(tmpdir) / "policy.sb"
    path.write_text(src, encoding="utf-8")
    return str(path)
