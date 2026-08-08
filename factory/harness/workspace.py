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
import os
import subprocess
from pathlib import Path, PurePosixPath


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def has_baseline(root: Path) -> bool:
    return _git(root, "rev-parse", "--verify", "HEAD").returncode == 0


def head_commit(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else None


def head_position(root: Path) -> tuple[str, str]:
    """HEAD 的位置：(commit 哈希, 符号 ref)。detached 时 ref 为空串。

    `git diff HEAD` 的**基线端**。前面所有闸门盯的都是这条命令的输入（属性、
    config、索引标记），这个盯的是它对比的那个点本身：worker 自己
    `git commit` 一次，HEAD 一移，diff 就变成「相对 worker 那次提交的增量」，
    后门落在 HEAD 里，监工只看到剩下那点没提交的改动。

    比索引跳过标记（第十二个洞）严重一档：那条路上 `git add -A` 不 stage，
    land 会以「无文件要提交」失败，未审代码出不了货；这条路上代码**已经在
    历史里**，land 只是往上叠一层，直接出货。

    判据取 (哈希, ref) 而不是只取哈希，因为两种移动各自成灾：
      - 提交：哈希变 → 后门进历史，diff 视野被截短
      - 只切分支：哈希**不变**，diff 照样诚实（实测 pdb 可见），但 land 的
        提交落到 worker 那条分支上（实测 `refs/heads/worker-side`，原分支
        `refs/heads/feat` 看不到它）—— 人审的是原分支，货在别处

    ref 用空串表示 detached 而不是 None：detached 是合法状态（`symbolic-ref`
    在那里以 128 退出，不是错误），而「从 detached 变成挂在分支上」和反过来
    都是这一轮动了 HEAD，两个方向都得算差异。
    """
    proc = _git(root, "symbolic-ref", "HEAD")
    ref = proc.stdout.strip() if proc.returncode == 0 else ""
    return head_commit(root) or "", ref


def replace_refs(root: Path) -> frozenset[str]:
    """`refs/replace/*` 的 (ref 名, 替换物) 集合。

    这一条比前面所有闸门都更靠下：它不改 `git diff HEAD` 的输入，也不动 HEAD，
    它改的是 git **回答对象内容时说的话**。设一条 replace ref，git 从此在
    「那个对象是什么」这个问题上撒谎，而所有闸门都是通过问 git 来看世界的。

    实测两种形态，八道闸门全静默、HEAD 哈希不变、`git status` 只报那处干净改动：

      替换 blob   —— `git diff HEAD` 退化成**空字符串**，paths 为 ()。
      替换 commit —— 伪造一个 tree 里带 `evil.py` 的提交顶掉 HEAD，于是那个
                     **全新文件**在 diff 里不存在、不在 changed_paths 里。
                     实测同一个仓库，不替换：paths `('README.md', 'evil.py')`、
                     diff 里有后门；替换后：paths `('README.md',)`、diff 里没有。

    全新文件平时之所以看得见，靠的是 `capture_diff` 开头那句 `git add -A -N`
    （intent-to-add）—— 这条攻击攻的正是它：路径进了索引，但 git 认为 HEAD 的
    tree 里本来就有一份一样的。`shadow_code()` 在这里帮不上忙，它只列被
    gitignore 挡住的文件，`evil.py` 没被 ignore，替换前后它都是 ()（实测）——
    是**不覆盖**，不是被瞎掉。

    危害是**伪造那份绿**，不是出货：`land` 只提交监工审过的那一组路径，实测
    linked worktree 里真跑一遍，`evil.py` 没进历史。但 check 跑在真实文件树上
    （实测能读到它），所以那份绿是假的。和第十二个洞（索引跳过标记）同性质。

    和 hooks、`.git/info/attributes` 一样在 **common dir** 里（实测从 worktree
    设一条，父仓库的 diff 也瞎了），一个 worker 能瞎掉所有并行任务的监工。

    读法是量出来的，三个候选里只有这一个站得住：

      `git replace -l`          只印**被替换的**那一侧。把一条已有的 ref 重新
                                指向另一个替换物，它的输出一字不变（实测），
                                而攻击内容已经换了 —— 这道闸门会瞎掉。
      读 `.git/refs/replace/`   `git pack-refs --all` 把它们挪进 packed-refs，
                                松散目录空掉（实测），两个 git 读法都不受影响。
      for-each-ref 名+对象      两种形态、诱饵自替换、打包前后、
                                `core.useReplaceRefs=false` 下都如实报告（实测）。

    `.git/info/grafts` 是同一族的兄弟机制，**刻意不管**：实测它在场时后门照样
    出现在 diff 里（它只改父子关系，不改 tree 内容），加一道闸门是给一条不存在
    的路修桥。git 自己也已经把它标成废弃。
    """
    proc = _git(
        root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/replace/"
    )
    if proc.returncode != 0:
        return frozenset()
    return frozenset(ln for ln in proc.stdout.splitlines() if ln.strip())


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


def hooks_dir(root: Path) -> Path | None:
    """这个 workspace 的 git hooks 目录（跟着 core.hooksPath 走）。

    worktree 里的 `.git` 是文件不是目录，且 hooks 属于**公共** git dir，
    所以不能拼 `root/".git"/"hooks"` —— 问 git 自己。
    """
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0 or not (base := common.stdout.strip()):
        return None
    cfg = _git(root, "config", "--get", "core.hooksPath")
    if cfg.returncode == 0 and (custom := cfg.stdout.strip()):
        p = Path(custom)
        return p if p.is_absolute() else (Path(root) / p)
    return Path(base) / "hooks"


def hook_fingerprint(root: Path) -> frozenset[tuple[str, str]]:
    """(hook 名, 内容 sha256) 的集合。只看**可执行且非 .sample** 的文件。

    `.sample` 是 `git init` 装的样例，不可执行、不生效，每个仓库都有一堆 ——
    算进来会让每次比对都有一大坨恒定噪音，也让「有没有变」这个信号变钝。

    为什么要指纹而不是「有没有 hook」：仓库本来就有合法 pre-commit 是完全正常的
    （lint、格式化），而 `land` 刻意**不加** `--no-verify`（人的 hook 该跑就跑，
    见 landing 模块 docstring）。要区分的是「这一轮 worker 有没有动过它们」。
    """
    d = hooks_dir(root)
    if d is None or not d.is_dir():
        return frozenset()
    out = []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.name.endswith(".sample"):
            continue
        if not os.access(f, os.X_OK):
            continue
        try:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
        out.append((f.name, digest))
    return frozenset(out)


def git_config(root: Path) -> frozenset[tuple[str, str]]:
    """仓库**本地** git config 的 (键, 值) 集合。

    只读 `--local`，不读 global / system：那两层不在 workspace 里，worker 改
    不到，算进来只会把「机器上的设置」当成「这一轮的改动」。

    为什么要这个基线：`.git/config` 在 sandbox 可写（实测 `os.access(W_OK)`
    为 True —— git_dir 必须可写，capture_diff 要写 index.lock）。config 里有
    好几个开关能直接瞎掉监工的输入，而且它们**不在任何 diff 里**：

      diff.external=./sh        → `git diff HEAD` 返回**空字符串**（实测 len=0），
                                  而 --name-only 不走 external，changed_paths 照常
                                  非空，于是「空 diff」那道判定也不响
      core.attributesFile=...   → 指向仓库外一份 `*.py -diff`，效果和第七个洞
                                  一样，但 `check-attr --source=HEAD` 也会读这个
                                  设置，基线和现状都是「被关掉」→ 第七道闸门
                                  差异为空，被绕过（实测）

    所以判据是「这一轮 config 变了」，不是逐个堵开关名 —— 那是猜机制，而
    git 的 config 键有几百个。

    刻意读 **--local 和 --worktree 两层的并集**，不是只读 --local。
    `config.worktree` 是 git 的第四层，只在 `extensions.worktreeConfig=true`
    时生效，而这个工厂的并行任务**跑在 worktree 里**（见 harness/worktree.py）。
    实测：仓库已开 extensions 时，往 `.git/worktrees/<名>/config.worktree` 写
    一行 `diff.external`，`--local` 完全不变（这道闸门差异为空），而
    `git diff HEAD` 照样返回空字符串 —— 也就是这道闸门自己的视野差。

    「这一轮才开 extensions」那条路本来就被拦住（`extensions.worktreeconfig`
    出现在 --local 里，实测报出来了），所以补的是「仓库本来就开着」——
    sparse-checkout 的常见配法。
    """
    out: set[tuple[str, str]] = set()
    for scope in ("--local", "--worktree"):
        proc = _git(root, "config", scope, "--list", "-z")
        # rc != 0 当**这一层不存在**，不是错误：未开 extensions 的 worktree 上
        # `--worktree` 以 128 退出（实测「无法读取配置文件 .git/config.worktree」）。
        # 非 worktree 仓库上它返回的和 --local 一样，并集去重，无副作用。
        if proc.returncode != 0:
            continue
        # `-z` 的格式：每条记录 `key\nvalue`，记录间 NUL 分隔。用 -z 是因为值里
        # 可以有换行（`git config foo.bar $'a\nb'` 是合法的），按行切会把一条拆成两条。
        for record in proc.stdout.split("\0"):
            if not record:
                continue
            key, _, value = record.partition("\n")
            out.add((key, value))
    return frozenset(out)


def changed_config(
    before: frozenset[tuple[str, str]], after: frozenset[tuple[str, str]]
) -> tuple[str, ...]:
    """两份 config 之间变动过的**键名**。新增、改值、删除都算。

    删除也算，和 changed_hooks 同一个理由：仓库里可能有人设的
    `core.autocrlf`、`diff.algorithm`，worker 删掉它等于改变 diff 的形状。

    取键名不取键值对：给 worker 的指令是「别动 .git/config」，而值里可能
    有路径、有人的邮箱 —— 递进 prompt 是白送信息。
    """
    return tuple(sorted({k for k, _ in before ^ after}))


def index_skipped(root: Path) -> frozenset[str]:
    """索引里被打上 assume-unchanged / skip-worktree 的路径。

    `git ls-files -v` 的首字母：`H` 是正常，**小写**是 assume-unchanged，
    `S` 是 skip-worktree。两个标记的效果一样致命 —— git 对这个文件的工作区
    改动一概不看，`git diff HEAD` 里没有它，`--name-only` 里也没有它。

    比 `.gitattributes`（第七个洞）更彻底：那个只让 diff 正文退化成
    「Binary files … differ」，路径还在 changed_paths 里，范围监工和 runbook
    照常工作。这个把路径本身抹掉，四道闸门全盲，而 check 跑在**真实文件树**
    上 —— 那行代码会真的执行。

    危害不是「未审代码出货」：实测 `git add -A` 不 stage 这类文件，land 那步
    会以「无文件要提交」失败。危害是伪造那份绿 —— 把测试改成 `assert True`
    再挂上标记，监工只看到源码文件的改动，pytest 退出码 0。
    """
    proc = _git(root, "ls-files", "-v")
    if proc.returncode != 0:
        return frozenset()
    out = set()
    for line in proc.stdout.splitlines():
        if len(line) < 3 or line[1] != " ":
            continue
        tag = line[0]
        # 小写 = assume-unchanged，S = skip-worktree。其余（H/M/R/C/K）正常。
        if tag.islower() or tag == "S":
            out.add(line[2:])
    return frozenset(out)


def newly_skipped(before: frozenset[str], after: frozenset[str]) -> tuple[str, ...]:
    """这一轮**新挂上**标记的路径。

    只报新增，不像 changed_hooks 那样取对称差：worker 把一个本来挂着标记的
    文件**取消**标记，效果是让 git 重新看见它的改动 —— 那是往闸门视野里
    送东西，不是藏东西。报它只会在合法情形上响。
    """
    return tuple(sorted(after - before))


def changed_hooks(
    before: frozenset[tuple[str, str]], after: frozenset[tuple[str, str]]
) -> tuple[str, ...]:
    """两份指纹之间变动过的 hook **名字**。新增、改内容、删除都算。

    删除也报，而不只报新增：仓库里那条合法的 pre-commit（lint、格式化）
    是人装的一道闸门，worker 删掉它等于关掉这道闸门，和自己加一个
    篡改内容的 hook 是同一类越权。

    对称差取名字而不是取 (名字, sha) 对：给 worker 的指令是「别动
    .git/hooks/pre-commit」，递一串 sha 过去他什么也做不了。
    """
    return tuple(sorted({name for name, _ in before ^ after}))


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


#: 每个邻居至少给这么多字节。低于这个数只剩几行 import，读不出约定，
#: 不如干脆归到「没放进来」里说清楚。
_MIN_PER_FILE = 2_000


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

    # 每个文件先分到一份**配额**，而不是先到先得把预算吃光。
    # 洞的形状：上一轮正常提交进版本控制的一个 12000 行文件，名字排在
    # `sorted()` 前面（`aaa_big.py`），就能把真正写着仓库约定的
    # `zz_conventions.py` 完全挤出监工视野。实测 len(ctx)=40023，
    # 「已有实现」一个字都不在里面，而**架构监工判重复实现的唯一依据就是
    # 这段 context**。它会如实判 pass —— 推理没错，输入不完整。
    #
    # 这一轮没有任何改动，所以前面四道闸门全瞎：不在 changed_paths 里、
    # 不是新增文件、不被 .gitignore 挡、diff 属性正常。
    keep = picked[:max_files]
    dropped_files = picked[max_files:]
    per_file = max(_MIN_PER_FILE, max_bytes // max(1, len(keep))) if keep else 0

    chunks: list[str] = []
    truncated: list[str] = []
    budget = max_bytes
    for rel in keep:
        if budget <= 0:
            dropped_files.append(rel)
            continue
        try:
            body = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        allow = min(per_file, budget)
        if len(body) > allow:
            truncated.append(rel)
            body = body[:allow]
        chunks.append(f"--- {rel} ---\n{body}")
        budget -= len(body)

    # 少给东西必须**说出来**。原来的版本静默截断：ctx 里没有任何标记说
    # 「这里还有别的文件」，于是「邻居只有这些」和「邻居被挤掉了」在监工
    # 眼里长得一模一样 —— 和 JSONL 改名那件事同一个形状，静默降级永远更
    # 好看（这里是「看起来没有重复实现」）。
    notes = []
    if truncated:
        notes.append(f"以上文件被截断（只给了开头）：{', '.join(sorted(truncated))}")
    if dropped_files:
        notes.append(
            f"同目录还有 {len(dropped_files)} 个文件没放进来："
            f"{', '.join(sorted(dropped_files))}"
        )
    if notes:
        chunks.append(
            "--- 上下文不完整 ---\n"
            + "\n".join(notes)
            + "\n判断「是否已有等价实现」时把这一条算进去：没看到不等于不存在。"
        )
    return "\n\n".join(chunks)


#: `git check-attr diff` 的正常回答。除此之外都意味着这个路径的 diff 被
#: 改过 driver —— `unset` 是 `-diff`（输出退化成 "Binary files differ"），
#: 任何别的值是自定义 driver（配 textconv 能让整段 diff **消失**）。
def info_attributes(root: Path) -> str:
    """`.git/info/attributes` 的内容指纹。不存在时返回空串。

    这是给 diff 属性设值的**第三条路**，前两条各有闸门：

      仓库树里的 .gitattributes  → diff_suppressed（第七个洞）。它的基线是
                                   `check-attr --source=HEAD`，只换掉「树里那份」
      core.attributesFile        → changed_config（第十三个洞）
      .git/info/attributes       → 这里

    第三条为什么必须单独判：`--source=HEAD` **不影响**这个文件，git 读它读的
    永远是磁盘上的当前内容。于是 diff_suppressed 的基线侧和现状侧都是「已被
    关掉」，差异为空 —— 那道闸门在这条路上恒不响（实测 `diff_suppressed`
    返回 `()`，而 `"pdb.set_trace" in diff` 是 False）。

    危害面比第七个洞更大：`.git/info/` 在**公共** git dir 里（和 hooks 一样），
    父仓库和所有并行 worktree 共用一份。实测从一个 worktree 写进去，主仓库的
    `git diff HEAD` 也跟着瞎 —— 一个 worker 能同时瞎掉所有并行任务的监工。

    取内容 sha256 而不是「文件存不存在」：外部仓库可以合法带一份
    `.git/info/attributes`（本机不该提交的属性配置就该放这儿）。要判的还是
    「这一轮变了」，不是「有没有」。
    """
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0 or not (base := common.stdout.strip()):
        return ""
    f = Path(base) / "info" / "attributes"
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest()
    except OSError:
        # 不存在是绝大多数仓库的常态，和「读不了」一样当空 —— 两边都当空的话
        # 「从没有到有」照样是差异，拦得住；反过来把读不了当成一个固定字符串
        # 会让权限异常伪装成「没变」。
        return ""


_DIFF_ATTR_OK = "unspecified"


def diff_suppressed(root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    """这些路径里，diff 正文被 `.gitattributes` 压掉的那些。

    洞的形状：`capture_diff` 用 `git diff HEAD`，而 `.gitattributes` 里
    一行 `*.py -diff` 就让正文退化成 `Binary files a/x.py and b/x.py differ`。
    实测：

      changed_paths : ('.gitattributes', 'a.py')   ← 照常，两个都在
      diff 正文     : a.py 只有一行 "Binary files ... differ"
      "pdb.set_trace" in diff : False              ← 监工看不到

    受害的是**审 diff 正文**的两个模型监工（规格、架构）—— 它们拿到的
    是一句「二进制变了」。范围监工和 runbook 靠 `--name-only` 和真实
    文件树，所以照常工作，这也是这个洞不容易发现的原因：三道闸门里
    两道正常。

    用 `git check-attr` 而不是解析 `.gitattributes`：属性可以来自任意层级
    的 `.gitattributes`、`.git/info/attributes`、`core.attributesFile`，
    自己解析那套优先级规则等于重写 git。check-attr 还顺手区分了
    `unset`（`-diff`）和自定义 driver（`diff=nodiff`），两者危害不同但
    都要拦。
    """
    # 空 paths 就别 fork 两个 git 了。**不是**为了防挂住 —— 实测
    # `git check-attr diff --` 不读 stdin，立刻以 129 退出，下面
    # `returncode != 0` 那条路本来就兜得住。纯粹是省两次进程。
    if not paths:
        return ()
    now = _diff_attrs(root, paths)
    if not now:
        return ()
    # 基线：`--source=HEAD` 让 git 用 **HEAD 里那份** .gitattributes 判属性。
    # 没有它这道检测会误拒合法用途 —— 一个 `*.lock -diff` 的仓库里，改
    # uv.lock 的任务每次都判红（实测本仓库就有 uv.lock）。要判的是
    # 「这个路径的 diff 是**这一轮**才被关掉的」，不是「有没有被关」。
    base = _diff_attrs(root, paths, source="HEAD")
    return tuple(
        p for p, v in now.items()
        if v != _DIFF_ATTR_OK and base.get(p, _DIFF_ATTR_OK) == _DIFF_ATTR_OK
    )


def _diff_attrs(
    root: Path, paths: tuple[str, ...], *, source: str | None = None
) -> dict[str, str]:
    """`git check-attr diff` 的结果，路径 → 属性值。"""
    args = ["check-attr"]
    if source:
        args.append(f"--source={source}")
    proc = _git(root, *args, "diff", "--", *paths)
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        # 格式是 `<path>: diff: <value>`，而 path 里可能带 `: `，所以从右边切。
        if ": diff: " not in line:
            continue
        path, _, value = line.rpartition(": diff: ")
        out[path] = value.strip()
    return out
