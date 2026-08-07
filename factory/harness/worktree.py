"""每个任务一个 git worktree。P1 并行派发的隔离单元。

为什么是 worktree 而不是 clone 或容器：

  - clone 每次拷全量对象库，大仓库上单任务就要几十秒；worktree 共享 .git，
    创建是常数时间。
  - 容器隔离的是**副作用**（装包、改系统），worktree 隔离的是**工作树**。
    并行派发第一个撞的是后者：两个 agent 同时改同一棵树，diff 会互相污染，
    审计里的 diff_hash 就不再对应任何一个任务的改动。容器留给 P1 后半段。
  - 已实测：worktree 里的 `git add -A -N`（capture_diff 用的）不碰父仓库
    index，父仓库 `git status` 保持干净。这是隔离能成立的前提。

不做自动合并回主分支：每个 worktree 落在自己的分支上，合谁、什么时候合是人的
决定 —— spec 里 merge 是「编排层判定全绿」，不是「编排层执行 git merge」。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def branch_name(task_id: str, *, prefix: str = "factory") -> str:
    """任务 id 转分支名。

    task_id 来自 YAML，可能带空格、斜杠、中文。斜杠在 git 里是层级分隔符，
    `a/b` 和 `a` 不能同时存在为分支 —— 直接用会在第二个任务上莫名失败。
    """
    slug = _SAFE.sub("-", task_id).strip("-") or "task"
    return f"{prefix}/{slug}"


@dataclass(frozen=True)
class Worktree:
    """一个任务的隔离工作树。path 就是传给 adapter 的 workspace。"""

    path: Path
    branch: str
    repo: Path

    def head(self) -> str | None:
        proc = _git(self.path, "rev-parse", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else None


class WorktreeError(RuntimeError):
    pass


class WorktreePool:
    """按任务开 worktree，跑完按策略回收。

    keep 策略是默认，因为**有改动的树不能默认删**：agent 干完活、监工判了绿，
    但合并是人的动作。自动删等于把还没人看过的产出扔了。
    """

    def __init__(
        self,
        repo: Path,
        *,
        root: Path | None = None,
        prefix: str = "factory",
    ) -> None:
        self._repo = Path(repo).resolve()
        # 默认放在仓库外的兄弟目录：放仓库内会被 agent 的 `git add -A` 扫进 diff，
        # 也会被自己的 capture_diff 当成改动文件。
        self._root = Path(root).resolve() if root else self._repo.parent / f".{prefix}-worktrees"
        self._prefix = prefix

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_repo(self) -> None:
        if _git(self._repo, "rev-parse", "--verify", "HEAD").returncode != 0:
            raise WorktreeError(
                f"{self._repo} 没有任何 commit，无法开 worktree。先建一个基线 commit。"
            )

    def acquire(self, task_id: str, *, base: str = "HEAD") -> Worktree:
        """给任务开一棵树。同名分支/目录已存在就先清掉再建。

        清掉而不是复用：复用会让上一次任务的残留改动混进这次的 diff，
        审计里就分不清哪些改动属于哪个 task_attempt。
        """
        self._ensure_repo()
        branch = branch_name(task_id, prefix=self._prefix)
        path = self._root / _SAFE.sub("-", task_id).strip("-")

        self._root.mkdir(parents=True, exist_ok=True)
        self._force_release(path, branch)

        proc = _git(self._repo, "worktree", "add", "-q", "-b", branch, str(path), base)
        if proc.returncode != 0:
            raise WorktreeError(
                f"git worktree add 失败（task={task_id}）：{proc.stderr.strip()}"
            )
        return Worktree(path=path, branch=branch, repo=self._repo)

    def _force_release(self, path: Path, branch: str) -> None:
        """把可能残留的目录/分支/注册项一起清干净。

        三样东西可以各自单独残留（上次崩在中途、目录被手删、分支被留下），
        所以三条命令都无条件跑一遍，谁失败都不算错。
        """
        _git(self._repo, "worktree", "remove", "--force", str(path))
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        _git(self._repo, "worktree", "prune")
        _git(self._repo, "branch", "-D", branch)

    def release(self, wt: Worktree, *, discard: bool = False) -> bool:
        """回收一棵树。

        discard=False（默认）时，树里有未提交改动就**拒绝删**并返回 False ——
        那是 agent 的产出，还没人验收过。返回值让调用方能把路径打给人。
        """
        if not discard and self.has_changes(wt):
            return False
        self._force_release(wt.path, wt.branch)
        return True

    def has_changes(self, wt: Worktree) -> bool:
        if not wt.path.exists():
            return False
        return bool(_git(wt.path, "status", "--porcelain").stdout.strip())

    def list_paths(self) -> tuple[Path, ...]:
        if not self._root.is_dir():
            return ()
        return tuple(sorted(p for p in self._root.iterdir() if p.is_dir()))
