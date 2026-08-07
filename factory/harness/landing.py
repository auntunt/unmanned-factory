"""落地：把判绿的产出提交到任务自己的分支上。

补的是 spec §5 里一个从第一天就空着的字段 —— `commit`。它一直写死 `None`，
于是 spec 写明的漏报回查路径（**发现 bug → git blame → commit → task_id →
当时哪个监工放过了**）没有任何数据可走。diff_hash 顶不上：它是内容指纹，
git blame 给不出来，从一行代码反推不到它。

三条边界，都是刻意的：

1. **只在 linked worktree 里提交，主工作树一律拒绝。** 判据是
   `--git-dir != --git-common-dir`。人的检出目录里冒出一个没人要求过的
   commit，是这一层能造成的最坏后果 —— 而它恰好也是最容易发生的那个
   （`factory run` 不加 `--worktree` 时 workspace 就是人的仓库本身）。
   实测过：在 linked worktree 里提交，父仓库的 HEAD 和 `git status`
   都不动，只有那条 `factory/*` 分支往前走一格。

2. **只提交合并的那一轮，中途打回的不提交。** 两个理由，第二个是硬的：
   - 打回的产出从不出货，git blame 永远落不到它上面，提交了也没人查；
   - `capture_diff` 用的是 `git diff HEAD`。中途提交会让下一轮的 diff
     变成「相对上一轮的增量」，而不是「这个任务改了什么」——
     审计里 diff_hash 的含义会在多轮任务上悄悄换掉。

3. **提交失败不改判决。** 失败的后果是 `commit` 字段仍为 `None`，
   也就是退回这个功能存在之前的状态。让一个已经全绿的任务因为
   `user.email` 没配而变成 escalated，是拿真问题换假问题。

不碰 git config，不加 `--no-verify`：仓库的 pre-commit hook 该跑就跑，
挂了就是提交失败（见第 3 条）。身份缺失时只用 `-c` 临时注入，
不写进任何配置文件。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# 身份没配时的兜底。只通过 `-c` 传给单条命令，不落任何 config。
FALLBACK_NAME = "factory"
FALLBACK_EMAIL = "factory@localhost"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True)


def is_linked_worktree(root: Path) -> bool:
    """这是个 linked worktree 吗（而不是仓库的主工作树）。

    主工作树里 git-dir 就是 git-common-dir；linked worktree 的 git-dir 是
    `<common>/worktrees/<name>`。这个判据比「.git 是文件还是目录」稳：
    submodule 的 .git 也是文件。
    """
    dir_ = _git(root, "rev-parse", "--absolute-git-dir")
    common = _git(root, "rev-parse", "--path-format=absolute",
                  "--git-common-dir")
    if dir_.returncode or common.returncode:
        return False
    return dir_.stdout.strip() != common.stdout.strip()


@dataclass(frozen=True)
class Landing:
    """落地结果。`commit` 为 None 时 `reason` 说明为什么没提交。

    「没提交」不都是错：没有改动、不在 worktree 里，都是正常路径。
    所以这里不抛异常也不区分错误等级 —— 调用方要的只是「有没有 sha」
    加上一句能打给人的话。
    """

    commit: str | None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.commit is not None


def _identity_args(root: Path) -> list[str]:
    """身份缺失时的 `-c` 参数。已配好就返回空列表，用仓库/全局的配置。

    不写 config：无人循环改人的 git 身份是越权的，而且在共享仓库上
    会把后续所有手工提交的作者也改掉。
    """
    if _git(root, "config", "user.email").returncode == 0:
        return []
    # 必须是两个 argv（`-c`, `k=v`）。写成 `-c=k=v` git 会报「未知选项」——
    # 而这条路径只在身份完全没配时才走到，所以配了身份的测试环境永远发现不了。
    return ["-c", f"user.name={FALLBACK_NAME}",
            "-c", f"user.email={FALLBACK_EMAIL}"]


def land(root: Path, *, task_id: str, attempt_no: int,
         paths: tuple[str, ...] = (), message: str | None = None) -> Landing:
    """把审查过的那些文件提交到当前分支。

    调用点只有一处：dispatcher 判绿之后（见模块 docstring 第 2 条）。

    `paths` 就是 `AttemptResult.changed_paths`，也就是 `capture_diff` 算
    diff_hash 时看到的那一组。**只提交这些，不是 `git add -A`。**

    真跑里抓到的：`add -A` 会把 check 命令自己产生的 `__pycache__/*.pyc`
    一起提交进去。后果不是「多了几个垃圾文件」，而是审计的两个字段开始
    描述不同的东西 —— diff_hash 只覆盖 src/text.py，commit 里却多了两个
    .pyc。监工审的是前者，出货的是后者，中间那段差额没有任何人看过。
    实测确认过：把 commit 里 src/text.py 的 diff 单独 sha256，
    正好等于库里记的 diff_hash，两者相差的就是那两个 .pyc。

    paths 为空时退化成 `add -A`：shell adapter 之类不报 changed_paths 的
    harness 仍然能落地。这是刻意的退化而非疏漏 —— 那条路径上「提交多了」
    好过「什么都没提交」，因为 diff_hash 在那里本来也是全量算的。
    """
    root = Path(root)
    if not is_linked_worktree(root):
        # 这是防线不是优化：主工作树意味着这是人的检出目录。
        return Landing(None, "不是 linked worktree，拒绝提交（主工作树是人的）")

    if paths:
        # `--` 分隔：路径里带 `-` 开头的文件不会被当成选项。
        # 删除的文件也要能提交，所以是 add 而不是 add --ignore-removal。
        _git(root, "add", "--", *paths)
    else:
        _git(root, "add", "-A")
    if not _git(root, "diff", "--cached", "--quiet").returncode:
        return Landing(None, "没有改动可提交")

    subject = message or f"{task_id} attempt#{attempt_no}"
    # trailer 是给回查用的：git blame 拿到 sha，`git show` 里就能直接读到
    # task_id，不必先去查审计库。审计库是权威，这个是冗余的近路。
    body = f"\n\nFactory-Task: {task_id}\nFactory-Attempt: {attempt_no}"
    proc = _git(root, *_identity_args(root), "commit", "-q", "-m",
                subject + body)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip().replace("\n", " ")[:200]
        return Landing(None, f"git commit 失败：{err}")

    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        return Landing(None, "提交后读不到 HEAD")
    return Landing(head.stdout.strip())
