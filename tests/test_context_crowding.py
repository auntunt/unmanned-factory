"""一个巨大邻居文件能把架构监工判重复实现的唯一依据挤出视野。

洞的形状：`neighbour_context` 原来是**先到先得**吃预算，而顺序是
`sorted(directory.iterdir())`。所以一个名叫 `aaa_big.py` 的 12000 行文件
（上一轮正常提交进版本控制的）能把 `zz_util.py` 里的已有实现完全挤掉。

架构监工的活是「重复实现 —— 周边代码里已经有等价的函数或模块」，而它判这件
事的唯一依据就是这段 context。挤掉之后它会如实判 pass：推理没错，输入不完整。

而且这一轮**没有任何改动** —— 前面四道闸门全瞎：不在 changed_paths 里、不是
新增文件、不被 .gitignore 挡住、diff 属性正常。

两条修法，都必要：
  1. 每文件配额，不是先到先得 —— 让被挤的文件重回视野。
  2. **说出来** —— 原来静默截断，「邻居只有这些」和「邻居被挤掉了」在监工眼里
     长得一模一样，而静默降级那一侧永远更好看（这里是「看起来没有重复实现」）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.workspace import capture_diff, neighbour_context

MARKER = "上下文不完整"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path, *, big_lines: int = 12_000, peers: int = 0) -> Path:
    """`zz_util.py` 里有已有实现，`aaa_big.py` 是排在它前面的巨大文件。"""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "zz_util.py").write_text(
        'def slugify(s):\n    return s.lower().replace(" ", "-")\n', encoding="utf-8"
    )
    if big_lines:
        (root / "src" / "aaa_big.py").write_text("# pad\n" * big_lines, encoding="utf-8")
    for i in range(peers):
        (root / "src" / f"aaa_{i:02d}.py").write_text("# small\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    # worker 这一轮重复实现了 slugify，只改了 main.py
    (root / "src" / "main.py").write_text(
        'def slugify(s):\n    return s.lower().replace(" ", "-")\n', encoding="utf-8"
    )
    return root


def _ctx(root: Path) -> str:
    _, paths = capture_diff(root)
    assert paths == ("src/main.py",), paths
    return neighbour_context(root, paths)


# --- 一、先证明洞是真的（用旧算法重放） -----------------------------------


def _old_algorithm(root: Path, changed: tuple[str, ...], max_bytes: int = 40_000) -> str:
    """改之前那版：先到先得吃预算，不说截断。"""
    import factory.harness.workspace as ws

    picked = []
    for p in changed:
        d = (root / p).parent
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.suffix not in ws._CODE_SUFFIXES:
                continue
            rel = str(f.relative_to(root))
            if rel not in changed and rel not in picked:
                picked.append(rel)
    chunks, budget = [], max_bytes
    for rel in picked[:12]:
        body = (root / rel).read_text(encoding="utf-8", errors="replace")
        if budget <= 0:
            break
        chunks.append(f"--- {rel} ---\n{body[:budget]}")
        budget -= len(body)
    return "\n\n".join(chunks)


def test_the_hole_a_big_neighbour_crowds_out_the_existing_implementation(tmp_path) -> None:
    root = _repo(tmp_path)
    old = _old_algorithm(root, ("src/main.py",))

    assert "aaa_big.py" in old
    assert "slugify" not in old, "已有实现还在？那这个洞不存在"
    assert MARKER not in old, "旧版本会说自己不完整？"


def test_the_hole_needs_no_change_this_round(tmp_path) -> None:
    """挤人的那个文件是上一轮正常提交的，四道闸门全看不见它。"""
    from factory.harness.workspace import diff_suppressed, runner_hooks, shadow_code

    root = _repo(tmp_path)
    _, paths = capture_diff(root)

    assert paths == ("src/main.py",)
    assert "src/aaa_big.py" not in paths
    assert shadow_code(root) == ()
    assert runner_hooks(root) == ()
    assert diff_suppressed(root, paths) == ()


# --- 二、修法一：每文件配额，被挤的文件回到视野 ---------------------------


def test_the_existing_implementation_survives_a_big_neighbour(tmp_path) -> None:
    root = _repo(tmp_path)
    ctx = _ctx(root)
    assert "slugify" in ctx, "架构监工判重复实现的唯一依据还是看不见"
    assert "zz_util.py" in ctx


def test_the_big_neighbour_is_still_partly_visible(tmp_path) -> None:
    """不是把大文件踢掉 —— 它也可能有约定信息，只是不许它吃光预算。"""
    root = _repo(tmp_path)
    ctx = _ctx(root)
    assert "aaa_big.py" in ctx


def test_the_byte_budget_still_holds(tmp_path) -> None:
    """配额不能变成「预算失效」，prompt 上限是这个函数存在的理由。"""
    root = _repo(tmp_path)
    ctx = neighbour_context(root, ("src/main.py",), max_bytes=10_000)
    assert len(ctx) < 14_000, len(ctx)


def test_many_small_peers_do_not_crowd_it_out_silently(tmp_path) -> None:
    """另一条挤出路径：20 个小文件顶满 max_files=12，靠 sorted 排在前面。

    这条挤不回来（超出 max_files 的确实没放），但**必须说出来**。
    """
    root = _repo(tmp_path, big_lines=0, peers=20)
    ctx = _ctx(root)
    body, _, note = ctx.partition(MARKER)
    # 正文里确实没有它（max_files 生效），但名字必须出现在告知里
    assert "def slugify" not in body, "max_files 失效了？"
    assert "zz_util.py" not in body
    assert "zz_util.py" in note, "没放进来的文件名得列出来"


# --- 三、修法二：少给东西必须说出来 ---------------------------------------


def test_truncation_is_announced(tmp_path) -> None:
    root = _repo(tmp_path)
    ctx = _ctx(root)
    assert MARKER in ctx
    assert "aaa_big.py" in ctx.split(MARKER)[1], "被截断的文件名得列出来"


def test_the_note_tells_the_judge_what_to_do_with_it(tmp_path) -> None:
    """光说「不完整」不够 —— 得说清这对「有没有重复实现」意味着什么。"""
    root = _repo(tmp_path)
    ctx = _ctx(root)
    assert "没看到不等于不存在" in ctx


def test_a_complete_context_says_nothing(tmp_path) -> None:
    """装得下就别加噪声。一个每次都说「我不完整」的标记等于没有标记。"""
    root = _repo(tmp_path, big_lines=0)
    ctx = _ctx(root)
    assert "slugify" in ctx
    assert MARKER not in ctx, ctx[-200:]


def test_no_neighbours_says_nothing(tmp_path) -> None:
    root = tmp_path / "solo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "only.py").write_text("x = 1\n", encoding="utf-8")
    assert neighbour_context(root, ("src/only.py",)) == ""


def test_this_repo_announces_its_truncation() -> None:
    """本仓库 tests/ 有 44 个邻居，必然截断 —— 那就必须带标记。"""
    here = Path(__file__).resolve().parent.parent
    ctx = neighbour_context(here, ("tests/test_workspace.py",))
    assert MARKER in ctx


def test_a_small_budget_gives_each_file_a_readable_slice(tmp_path) -> None:
    """预算调小时，均分会把每个文件切到只剩几行 import，读不出约定。

    `max_bytes=10_000` 配 12 个邻居 = 均分 833 字节。低于下限的话与其给一份
    读不出东西的开头，不如把这个文件归到「没放进来」里说清楚 —— 前者会让
    监工以为自己看过了。默认参数下均分是 3333，所以这条只有调小预算才触发。
    """
    root = tmp_path / "many"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    for i in range(12):
        (root / "src" / f"n{i:02d}.py").write_text("# pad\n" * 400, encoding="utf-8")

    ctx = neighbour_context(root, ("src/main.py",), max_bytes=10_000)

    bodies = [c for c in ctx.split("--- ") if c.startswith("src/")]
    assert bodies, ctx[:200]
    for b in bodies:
        assert len(b) >= 1_500, f"切得太碎读不出东西：{len(b)}"
    assert MARKER in ctx, "切碎了却不说"
