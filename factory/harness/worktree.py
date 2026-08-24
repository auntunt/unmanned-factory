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

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 注意 `.` **不在**安全集里。曾经在，那是个数据全毁级的洞：
# task_id='..' 原样穿过清洗，`root / '..'` 指向 worktree 池的父目录
# —— 也就是用户主仓库 —— 然后 _force_release() 的 shutil.rmtree 把它清空。
# task_id='.' 则指向池本身，一次删掉所有并行任务的未合并产出。
# 两者都实测复现过。现在 `.` 被清洗掉，且 _slug() 另有兜底校验。
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")

# slug 的清洗部分留多长。够读就行，唯一性由哈希后缀保证。
_SLUG_STEM_MAX = 48
# 哈希后缀长度。6 个 hex = 16M 种，任务量级下碰撞概率可忽略。
_SLUG_HASH_LEN = 6


def _slug(task_id: str) -> str:
    """task_id → 既安全又**单射**的短标识。

    单射是必须的，不是锦上添花：slug 同时用作目录名和分支名，而 acquire()
    开头就无条件 _force_release() 掉同名的树。两个 task_id 撞到同一个 slug，
    后来的那个会删掉前一个还没合并的产出，并且审计里没有任何痕迹。

    纯清洗做不到单射 —— `_SAFE.sub('-', ...)` 是多对一的。实测过的塌缩：
      `feat/login`、`feat-login`、`feat login` → 全都是 `feat-login`
      `重构/队列`、`修复/超时`               → 全都是 `task`（中文整体被吃掉）
    后者尤其糟：中文 task_id 是常规用法，等于所有中文任务共用一棵树。

    所以 slug = 清洗后的可读部分 + 原始 task_id 的哈希后缀。哈希取自
    **原始**字符串，清洗前，这样上面那些例子彼此区分。可读部分只为人眼服务，
    唯一性完全由哈希承担。
    """
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:_SLUG_HASH_LEN]
    stem = _SAFE.sub("-", task_id).strip("-")[:_SLUG_STEM_MAX].strip("-")
    # stem 可能整体被清洗成空（纯中文、纯符号），那就只留哈希。
    # 另外挡掉 git 不接受的分支名和路径穿越残留：'.' / '..' 已被 _SAFE 吃掉，
    # 这里是第二道 —— 万一日后有人放宽 _SAFE，这条还在。
    if not stem or stem in {".", ".."}:
        return f"task-{digest}"
    return f"{stem}-{digest}"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def branch_name(task_id: str, *, prefix: str = "factory") -> str:
    """任务 id 转分支名。

    task_id 来自 YAML，可能带空格、斜杠、中文。斜杠在 git 里是层级分隔符，
    `a/b` 和 `a` 不能同时存在为分支 —— 直接用会在第二个任务上莫名失败。

    走 _slug() 而不是自己清洗：分支名和 worktree 目录名必须**同源**，
    否则会出现"目录复用了但分支没复用"这种半残状态。
    """
    return f"{prefix}/{_slug(task_id)}"


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
        path = self._root / _slug(task_id)

        # 兜底：确认算出来的路径真的落在池内。_slug() 已经保证了这点，这里是
        # 第二道 —— 下一行就是 _force_release() 的 shutil.rmtree，它删什么
        # 完全取决于这个 path。曾经 task_id='..' 能让它删掉用户主仓库。
        # 一道 rmtree 前的 assert 值这个钱。
        self._assert_inside_pool(path)

        self._root.mkdir(parents=True, exist_ok=True)
        self._force_release(path, branch)

        proc = _git(self._repo, "worktree", "add", "-q", "-b", branch, str(path), base)
        if proc.returncode != 0:
            raise WorktreeError(
                f"git worktree add 失败（task={task_id}）：{proc.stderr.strip()}"
            )
        return Worktree(path=path, branch=branch, repo=self._repo)

    def _assert_inside_pool(self, path: Path) -> None:
        """确认 path 严格落在 worktree 池内部，否则拒绝动它。

        `strict=False` 的 resolve：路径此刻通常还不存在（正要创建），
        strict 模式会直接抛 FileNotFoundError。我们要判的是**归一化之后的
        字符串归属**，不是存在性。

        用 is_relative_to 而不是 str.startswith：后者会把
        `/pool-evil` 当成 `/pool` 的子路径（前缀匹配的经典坑）。
        另外显式排除 path == root 本身：那是 task_id='.' 的情形，
        删掉它等于清空整个池。
        """
        root = self._root.resolve()
        target = path.resolve()
        if target == root or not target.is_relative_to(root):
            raise WorktreeError(
                f"拒绝操作 worktree 池外的路径：{target}（池根={root}）。"
                "这通常意味着 task_id 含路径穿越成分，或 _slug() 被改坏了。"
            )

    def _force_release(self, path: Path, branch: str) -> None:
        """把可能残留的目录/分支/注册项一起清干净。

        三样东西可以各自单独残留（上次崩在中途、目录被手删、分支被留下），
        所以三条命令都无条件跑一遍，谁失败都不算错。

        进 rmtree 之前再验一次归属。调用方（acquire / release）都已经验过，
        这里重复是因为**这个函数是唯一真正执行删除的地方** —— 谁日后新增一条
        调用路径，都不会绕过这道检查。检查失败时抛而不是静默返回：
        走到这儿说明有更上游的东西已经错了，静默会把 bug 藏起来。
        """
        self._assert_inside_pool(path)
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
