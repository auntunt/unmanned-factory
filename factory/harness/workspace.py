"""workspace diff 捕获。

用 `git add -A -N` + `git diff HEAD`：
  - -N 只登记 intent-to-add，不 stage 内容 → 无副作用，人后续照常 commit
  - 这样才能拿到「新增文件」和「新目录里的新文件」的内容，单纯 git diff 拿不到
前提：workspace 至少有 1 个 commit，否则 HEAD 不存在。

**四道闸门（后分级、范围监工、runbook、架构监工）共用 changed_paths 这一个
视野，而这个视野是 git 的，不是文件系统的。** 落在 .gitignore 覆盖路径下的
新文件不进 diff、不进 changed_paths、`git status --porcelain` 也不报，
于是四道闸门全都看不见 —— 但 check 命令跑在真实文件树上，照样会执行它。
`shadow_code` 就是补这个视野差，见它的 docstring。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path, PurePosixPath


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def has_baseline(root: Path) -> bool:
    return _git(root, "rev-parse", "--verify", "HEAD").returncode == 0


def head_commit(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else None


def capture_diff(root: Path) -> tuple[str, tuple[str, ...]]:
    if not has_baseline(root):
        raise RuntimeError(f"{root} 没有任何 commit，无法 diff。先 git commit 一个基线。")
    _git(root, "add", "-A", "-N")
    diff = _git(root, "diff", "HEAD").stdout
    names = _git(root, "diff", "HEAD", "--name-only").stdout
    paths = tuple(line for line in names.splitlines() if line.strip())
    return diff, paths


#: 一定是别人的代码或机器生成物的目录 —— 这里面有 .py 不代表 worker 写了什么。
#: **刻意不含 build/ 和 dist/**：那是 _SKIP_DIRS 的成员，但两张表的语义相反 ——
#: _SKIP_DIRS 说「这里没有值得读的代码」（给架构监工找邻居用），这张表要答的是
#: 「这里有不该出现的代码」。build/ 恰好既是常见生成物目录，又是最好藏代码的
#: 地方，所以它必须落在检测范围里。合表就等于给攻击留了一个白名单。
_VENDOR_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", "site-packages", ".eggs",
})

#: runner 会**自动发现并加载**的配置文件名 —— 存在本身就改变 check 的行为，
#: 内容里没有任何关键词可查。只判「这一轮新增」，不判内容。
#:
#: 刻意只收「自动发现」这一类，不收 pyproject.toml / package.json / Makefile：
#: 那些是任务合法要改的东西，列进来会让这道检测在正常任务上响。判据是
#: 「本来不存在、runner 会自己去找它」，不是「能影响构建」。
_RUNNER_HOOKS = frozenset({
    "conftest.py", "sitecustomize.py", "usercustomize.py",
    "pytest.ini", ".pytest.ini", "tox.ini",
    "jest.config.js", "jest.config.ts", "jest.setup.js",
    "vitest.config.js", "vitest.config.ts",
    ".mocharc.json", ".mocharc.yml", ".mocharc.yaml", "karma.conf.js",
})


def added_paths(root: Path) -> tuple[str, ...]:
    """这一轮**新增**的文件。`capture_diff` 只给「改动过的」，不分新增和修改。

    `add -A -N` 这一步不能省，也不能指望调用方做过：未追踪的文件根本不在
    `git diff HEAD` 里，少了这行就永远返回空 —— 而返回空长得和「这一轮很干净」
    一模一样。第一版漏了它，测试当场抓到（在 dispatcher 里恰好因为
    capture_diff 先跑过而看不出来，那是最难查的那种巧合）。
    """
    _git(root, "add", "-A", "-N")
    proc = _git(root, "diff", "HEAD", "--name-only", "--diff-filter=A")
    if proc.returncode != 0:
        return ()
    return tuple(l for line in proc.stdout.splitlines() if (l := line.strip()))


def runner_hooks(root: Path) -> tuple[str, ...]:
    """这一轮新增的 runner 自动加载文件 —— worker 给自己出卷子的最短路径。

    实测（/tmp 里构造过）：一个必然失败的 `tests/test_real.py`，worker 新建

        conftest.py:
            def pytest_collection_modifyitems(config, items):
                items.clear()

    `pytest -q` 输出「no tests ran」，**退出码 0**。而 check 的判据就是退出码。

    为什么现有的闸门都拦不住：
      - `conftest.py` **在** changed_paths 里（不是影子代码），
        但范围监工 `declared_paths` 为空时一律 PASS，而口述来源的任务
        绝大多数为空（见 supervisors/scope.py 的 docstring）
      - 后分级：conftest.py 不匹配任何分级规则 → A 类
      - runbook：规则查的是文件**内容**里的关键词，而这里的危害在于
        「这个文件出现了」，内容完全合法

    这是 runbook「项目规则不许放在 workspace 里」的同一条道理（worker 能写
    workspace，让它出卷子等于没有卷子），只不过那条封的是我们自己读的规则
    文件，这条封的是 runner 自己会去找的文件 —— 后者我们没在读，所以漏了。

    只判**新增**：项目里本来就有 conftest.py 是完全正常的（本仓库
    tests/conftest.py 就是），改它也可能是任务的正当内容。
    """
    hooks = []
    for p in added_paths(root):
        pp = PurePosixPath(p)
        if any(part in _VENDOR_DIRS for part in pp.parts):
            continue
        if pp.name in _RUNNER_HOOKS:
            hooks.append(p)
    return tuple(hooks)


_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java"}

def shadow_code(root: Path) -> tuple[str, ...]:
    """被 .gitignore 挡住、四道闸门都看不见的**代码**文件。

    形状（实测构造过）：worker 改 src/app.py 加一行 `import build.hook`，
    同时新建 build/hook.py（.gitignore 里有 build/）。结果：

        changed_paths            → ('src/app.py',)      ← 只有这个
        diff 里能看到那行 import → 是                    ← 监工看得见调用
        diff 里能看到 hook.py    → **不能**              ← 看不见被调用的东西
        git status --porcelain   → 只报 src/app.py
        check 命令实际执行 hook.py → **会**（实测打印出来了）

    `land` 用 `add -- *paths` 只提交审过的那组，所以这个文件不会进 commit ——
    危险不在出货，在**检查**：check 全绿这件事是在一个含有未审代码的文件树上
    得出的，而那份绿是四道闸门放行的唯一依据。

    过滤到只剩代码后缀 + 排除依赖目录之后，本仓库命中 0 条（实测）。这个数字
    是这道检测能用的前提：每次都响的闸门等于没有闸门。
    """
    proc = _git(root, "ls-files", "--others", "--ignored", "--exclude-standard")
    if proc.returncode != 0:
        return ()
    out = []
    for line in proc.stdout.splitlines():
        if not (line := line.strip()):
            continue
        p = PurePosixPath(line)
        if p.suffix not in _CODE_SUFFIXES:
            continue
        if any(part in _VENDOR_DIRS for part in p.parts):
            continue
        out.append(line)
    return tuple(out)


def diff_hash(diff: str) -> str | None:
    if not diff:
        return None
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "dist", "build"}


def neighbour_context(
    root: Path,
    changed_paths: tuple[str, ...],
    *,
    max_files: int = 12,
    max_bytes: int = 40_000,
) -> str:
    """改动文件的同目录既有代码，给架构监工判断约定和重复实现用。

    只取同目录、只取未改动的文件：改动本身在 diff 里已经给过一遍，
    重复给会让「哪些是新写的」变模糊，而这正是判重复实现要分清的。
    """
    changed = set(changed_paths)
    picked: list[str] = []
    for p in changed_paths:
        directory = (root / p).parent
        if not directory.is_dir():
            continue
        for f in sorted(directory.iterdir()):
            if not f.is_file() or f.suffix not in _CODE_SUFFIXES:
                continue
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            try:
                rel = str(f.relative_to(root))
            except ValueError:
                continue
            if rel in changed or rel in picked:
                continue
            picked.append(rel)

    chunks: list[str] = []
    budget = max_bytes
    for rel in picked[:max_files]:
        try:
            body = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if budget <= 0:
            break
        chunks.append(f"--- {rel} ---\n{body[:budget]}")
        budget -= len(body)
    return "\n\n".join(chunks)
