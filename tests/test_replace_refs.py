"""`refs/replace/*` —— 让 git 在「那个对象是什么」这个问题上撒谎。

洞的形状比前面几个都靠下一层。前面的闸门盯的是 `git diff HEAD` 的**输入**
（hooks、属性、config、索引标记、验收标准）和它的**基线端**（HEAD 本身）。
这一条两样都不动：它改的是 git 回答对象内容时说的话，而所有闸门都是通过问
git 来看世界的。

两种形态：
  替换 blob   —— diff 退化成空字符串，changed_paths 为 ()
  替换 commit —— 伪造一个 tree 里带新文件的提交顶掉 HEAD，那个**全新文件**
                 在 diff 里不存在、也不在 changed_paths 里（实测：不替换时
                 paths 是 ('README.md', 'evil.py')，替换后只剩 ('README.md',)）

全新文件的**唯一**看见者是 `capture_diff` 里那句 `git add -A -N`。`shadow_code`
在这条路上帮不上忙，而且它不是被瞎掉的 —— 它只列 gitignore 挡住的文件，
`evil.py` 没被 ignore，替换前后它都返回 `()`（实测两种情况都是）。第一版这里
写的是「闸门 A 的判据落在闸门 B 的下游，第五次」，那是把「它没报」错当成
「它被瞎掉」。没报是因为这个文件从来不在它的管辖范围内。

危害是伪造那份绿，不是出货：`land` 只提交监工审过的那一组路径。和第十二个洞
（索引跳过标记）同性质。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.workspace import (
    capture_diff,
    changed_config,
    changed_hooks,
    diff_suppressed,
    git_config,
    head_position,
    hook_fingerprint,
    index_skipped,
    info_attributes,
    newly_skipped,
    replace_refs,
    runner_hooks,
    shadow_code,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "feat")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


BACKDOOR = "import pdb; pdb.set_trace()\n"


def _replace_blob(root: Path) -> None:
    """worker 干的事之一：把后门写进磁盘，再让 git 认为 HEAD 里本来就是这样。

    方向很容易搞反（第一版就反了）：ref 要以 **HEAD 里那个干净 blob** 命名，
    指向**磁盘上这个脏 blob**。git 读 HEAD:a.py 时被换成脏的，于是
    「HEAD 里的」和「工作区的」一模一样 → diff 为空。
    反过来（脏的 → 干净的）什么也挡不住：没人会去读那个脏 blob。
    """
    clean = _git(root, "rev-parse", "HEAD:a.py").strip()
    (root / "a.py").write_text("x = 1\n" + BACKDOOR, encoding="utf-8")
    dirty = _git(root, "hash-object", "-w", "a.py").strip()
    _git(root, "update-ref", f"refs/replace/{clean}", dirty)


def _replace_commit(root: Path) -> None:
    """更强的一支：伪造一个 tree 里已经带 evil.py 的提交顶掉 HEAD。

    于是那个全新文件在 git 眼里「本来就在」，diff 里不存在，shadow_code 也
    认它 —— 而磁盘上它是这一轮新写的，check 照样会执行它。
    """
    (root / "evil.py").write_text(BACKDOOR, encoding="utf-8")
    (root / "README.md").write_text("hi\nchanged\n", encoding="utf-8")
    head = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "add", "evil.py")
    tree = _git(root, "write-tree").strip()
    _git(root, "reset", "-q", "HEAD", "--", "evil.py")
    fake = subprocess.run(
        ("git", "commit-tree", tree, "-m", "fake"),
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(root, "update-ref", f"refs/replace/{head}", fake)


# --- 一、先证明洞是真的 ---------------------------------------------------


def test_the_hole_replacing_a_blob_empties_the_diff(tmp_path: Path) -> None:
    """替换 blob → diff 是空字符串，paths 是 ()。"""
    root = _repo(tmp_path)
    _replace_blob(root)

    diff, paths = capture_diff(root)

    assert "pdb.set_trace" not in diff, "洞不存在了？那这道闸门可以删"
    assert paths == (), f"paths 也该是空的: {paths}"
    # 危害在这里：git 嘴上说没变，磁盘上后门是真的，check 跑的是磁盘。
    assert BACKDOOR in (root / "a.py").read_text(encoding="utf-8")


def test_the_hole_a_partial_replace_keeps_paths_nonempty(tmp_path: Path) -> None:
    """只替换后门那个文件、另留一处干净改动不管 → 连「diff 是空的」都不成立。

    这一条是为了掐掉一个偷懒的修法：「diff 为空就判红」。worker 只要顺手留
    一处真改动，paths 就非空，那道假想的闸门永远不响。
    """
    root = _repo(tmp_path)
    _replace_blob(root)
    (root / "README.md").write_text("hi\nchanged\n", encoding="utf-8")

    diff, paths = capture_diff(root)

    assert paths == ("README.md",), f"只该看见那处诱饵: {paths}"
    assert "pdb.set_trace" not in diff
    assert diff.strip(), "diff 非空，所以「空 diff 判红」这个修法挡不住它"


def test_the_hole_replacing_a_commit_hides_a_brand_new_file(tmp_path: Path) -> None:
    """替换 commit → 全新的 evil.py 在 diff 里根本不存在。"""
    root = _repo(tmp_path)
    _replace_commit(root)

    diff, paths = capture_diff(root)

    assert "evil.py" not in diff, "洞不存在了？"
    assert "evil.py" not in paths, f"paths 里不该有它: {paths}"
    assert paths == ("README.md",), f"只剩那处诱饵: {paths}"
    assert (root / "evil.py").exists(), "磁盘上它是真的，check 会执行它"


def test_shadow_code_is_not_a_backstop_here(tmp_path: Path) -> None:
    """别把 shadow_code 当第二道防线 —— 它压根没管过这个文件。

    这条测试是一次自我纠错留下的。我先以为「闸门 A 的判据落在闸门 B 的下游」
    在这里第五次出现：全新的 .py 被替换的 tree 遮住，shadow_code 也瞎掉。
    量了才知道不是 —— 它在**替换与否两种情况下都返回 ()**，因为它问的是
    「被 gitignore 挡住的文件有哪些」（`ls-files --others --ignored`），
    而 evil.py 没被 ignore。它不是被瞎掉，是从来不在射程内。

    「它没报」和「它被瞎掉」是两件事，混起来会记下一条假的因果。真相反而更
    朴素也更该记：**全新非 ignore 文件的唯一视野来自 `capture_diff` 里那句
    `add -A -N`**，替换掉的正是它，此处没有第二道防线。
    """
    root = _repo(tmp_path)
    (root / "evil.py").write_text(BACKDOOR, encoding="utf-8")

    # 不替换：diff 看得见它，shadow_code 已经是 () —— 它本来就不管这类文件。
    diff, paths = capture_diff(root)
    assert "evil.py" in paths, f"add -A -N 让全新文件进 diff: {paths}"
    assert "pdb.set_trace" in diff
    assert shadow_code(root) == (), "没被 ignore，所以它不报 —— 与替换无关"

    # 它响的条件是「被 ignore」，不是「新」：
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "ignored").mkdir()
    (root / "ignored" / "h.py").write_text(BACKDOOR, encoding="utf-8")
    assert shadow_code(root) == ("ignored/h.py",), "它的射程是 ignore，不是 newness"


def test_the_hole_every_other_gate_stays_silent(tmp_path: Path) -> None:
    """七道已有闸门在这个攻击下全部静默 —— 所以它需要自己的一道。"""
    root = _repo(tmp_path)
    cfg_before = git_config(root)
    hooks_before = hook_fingerprint(root)
    skip_before = index_skipped(root)
    attrs_before = info_attributes(root)
    head_before = head_position(root)

    _replace_commit(root)
    _, paths = capture_diff(root)

    assert shadow_code(root) == ()
    assert runner_hooks(root) == ()
    assert diff_suppressed(root, paths) == ()
    assert newly_skipped(skip_before, index_skipped(root)) == ()
    assert changed_config(cfg_before, git_config(root)) == ()
    assert changed_hooks(hooks_before, hook_fingerprint(root)) == ()
    assert info_attributes(root) == attrs_before
    assert head_position(root) == head_before, "HEAD 没动，动的是 git 对它的回答"


# --- 二、函数本身：读法是量出来的 -----------------------------------------


def _blob(root: Path, text: str) -> str:
    (root / "scratch").write_text(text, encoding="utf-8")
    return _git(root, "hash-object", "-w", "scratch").strip()


def test_replace_refs_is_empty_on_a_clean_repo(tmp_path: Path) -> None:
    """误报面：干净仓库是空集。本仓库实测也是空集。"""
    assert replace_refs(_repo(tmp_path)) == frozenset()


def test_replace_refs_sees_both_forms(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _replace_blob(root)
    assert len(replace_refs(root)) == 1

    root2 = _repo(tmp_path, "repo2")
    _replace_commit(root2)
    assert len(replace_refs(root2)) == 1


def test_replace_refs_catches_repointing_where_replace_l_is_blind(
    tmp_path: Path,
) -> None:
    """把一条已有 ref 重新指向另一个替换物 —— `git replace -l` 一字不变。

    这是选 for-each-ref 的理由，不是风格偏好：`replace -l` 只印**被替换的**
    那一侧，攻击内容整个换掉它也看不出来。取 (ref 名, 目标对象) 才判得动。
    """
    root = _repo(tmp_path)
    clean = _git(root, "rev-parse", "HEAD:a.py").strip()
    _git(root, "update-ref", f"refs/replace/{clean}", _blob(root, "one\n"))

    listed_before = _git(root, "replace", "-l")
    seen_before = replace_refs(root)

    _git(root, "update-ref", f"refs/replace/{clean}", _blob(root, "two\n"))

    assert _git(root, "replace", "-l") == listed_before, "它瞎掉了，所以不能用它"
    assert replace_refs(root) != seen_before, "这道闸门必须看见重新指向"


def test_replace_refs_survives_pack_refs(tmp_path: Path) -> None:
    """`git pack-refs --all` 把 ref 挪进 packed-refs，松散目录空掉。

    这是不去读 `.git/refs/replace/` 目录的理由：那个读法在打包后返回空集，
    而攻击仍然生效（两个 git 读法都不受影响）。
    """
    root = _repo(tmp_path)
    _replace_blob(root)
    before = replace_refs(root)

    _git(root, "pack-refs", "--all")

    loose = root / ".git" / "refs" / "replace"
    assert not loose.exists() or not list(loose.iterdir()), "确认真被挪走了"
    assert replace_refs(root) == before, "打包不该改变判据"
    assert "pdb.set_trace" not in capture_diff(root)[0], "攻击仍然生效"


def test_replace_refs_reports_even_when_replacement_is_disabled(
    tmp_path: Path,
) -> None:
    """`core.useReplaceRefs=false` 关掉攻击效果，但 ref 还在 —— 照样报。

    判据是「这一轮多了/少了一条 replace ref」，不是「diff 现在是否被骗」。
    worker 可以先设 ref、再自己关掉开关，等它落地之后没人再去开 —— 判效果
    会漏，判 ref 在不在不会。而这个开关本身归 changed_config 管。
    """
    root = _repo(tmp_path)
    _replace_blob(root)
    _git(root, "config", "core.useReplaceRefs", "false")

    assert "pdb.set_trace" in capture_diff(root)[0], "开关确实关掉了效果"
    assert len(replace_refs(root)) == 1, "但 ref 还在，闸门照样得报"


def test_replace_refs_is_shared_across_worktrees(tmp_path: Path) -> None:
    """refs/replace 在 common dir 里：一个 worker 能瞎掉所有并行任务。"""
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    ws = tmp_path / "wt"

    _replace_blob(ws)

    assert replace_refs(root) == replace_refs(ws) != frozenset()


# --- 三、接线：真跑一轮 dispatcher ----------------------------------------


def _wt(tmp_path: Path) -> Path:
    """dispatcher 真跑的形状：worktree 里干活，顺手留一处干净改动。"""
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    ws = tmp_path / "wt"
    (ws / "README.md").write_text("hi\nchanged\n", encoding="utf-8")
    return ws


def _dispatch(tmp_path: Path, ws: Path, checks_ran: list, *, attack: str = ""):
    """跑一轮真 dispatcher。`attack` 模拟 worker 在派发**期间**设 replace ref。"""
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class Replacing(FakeAdapter):
        # 只动第一轮：闸门判红会打回，第二轮再设一次会撞上「ref 已存在」，
        # 那种失败长得像闸门坏了，其实是闸门生效了（第十六个洞踩过一次）。
        done = False

        def run(self, task, workspace, limits, *, model=None):
            root = Path(workspace)
            if not Replacing.done:
                Replacing.done = True
                if attack == "blob":
                    _replace_blob(root)
                elif attack == "commit":
                    _replace_commit(root)
            return super().run(task, workspace, limits, model=model)

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(
        task_id="T-replace",
        prompt="改点东西",
        checks=(CheckSpec(name="ok", command="true"),),
    )
    d = Dispatcher(
        adapter=Replacing([_result(paths=("README.md",))]),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Recording(),
    )
    return d.run(task, ws)


def test_replacing_a_blob_mid_round_blocks_the_merge(tmp_path: Path) -> None:
    rep = _dispatch(tmp_path, _wt(tmp_path), [], attack="blob")
    assert rep.outcome.value != "merged", f"设了 replace ref 还能合并：{rep}"


def test_replacing_a_commit_mid_round_blocks_the_merge(tmp_path: Path) -> None:
    rep = _dispatch(tmp_path, _wt(tmp_path), [], attack="commit")
    assert rep.outcome.value != "merged", f"替换了 HEAD 还能合并：{rep}"


def test_the_claim_names_the_gate(tmp_path: Path) -> None:
    rep = _dispatch(tmp_path, _wt(tmp_path), [], attack="commit")
    assert "replace-refs-changed" in rep.escalation_reason, rep.escalation_reason


def test_checks_do_not_run_when_a_replace_ref_appears(tmp_path: Path) -> None:
    """闸门在 check 之前 —— 那份绿压根不该被产出。"""
    ran: list = []
    _dispatch(tmp_path, _wt(tmp_path), ran, attack="commit")
    assert ran == [], f"check 跑了，说明闸门在它下游: {ran}"


def test_a_clean_round_still_merges(tmp_path: Path) -> None:
    """误拒面：什么都不动的一轮照常合并。"""
    rep = _dispatch(tmp_path, _wt(tmp_path), [], attack="")
    assert rep.outcome.value == "merged", f"干净的一轮被拦了: {rep.outcome}"


# --- 四、出不出货，以及基线站在哪 ------------------------------------------


def test_the_attack_forges_the_green_but_does_not_ship(tmp_path: Path) -> None:
    """危害定级：假的绿，不是出货。和第十二个洞同性质，比第十六个洞轻一档。

    `land` 只 `git add --` 监工审过的那一组路径，而 evil.py 恰恰不在那一组里
    （它被替换的 tree 藏起来了），于是它进不了历史。但这不等于没事：check
    跑在**真实文件树**上，那份绿是在一个含未审代码的树上得出的。
    """
    from factory.harness.landing import land

    ws = _wt(tmp_path)
    _replace_commit(ws)
    _, paths = capture_diff(ws)
    assert "evil.py" not in paths, "前提：监工没看见它"

    landed = land(ws, task_id="T-x", attempt_no=1, paths=paths)
    assert landed, f"落地本身该成功: {landed.reason}"

    # 读历史要绕开 replace，否则问的还是那个会撒谎的 git。
    tree = subprocess.run(
        ("git", "--no-replace-objects", "ls-tree", "-r", "--name-only", "HEAD"),
        cwd=ws, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "evil.py" not in tree, f"出货了，那这个洞比定级更重: {tree}"
    assert (ws / "evil.py").exists(), "但它在磁盘上，check 读得到 —— 绿是假的"


def test_a_preexisting_replace_ref_does_not_block(tmp_path: Path) -> None:
    """误拒面：外部仓库本来就带一条 replace ref 时，干净的一轮照常合并。

    判据是「这一轮新增/变动」，不是「存在」。git 的 replace 是个正当机制
    （改历史里的坏提交），拿存在当判据会让这道闸门在那类仓库上恒响。
    """
    ws = _wt(tmp_path)
    _replace_blob(ws)  # 派发**之前**就有
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")  # 复原，别留脏内容

    rep = _dispatch(tmp_path, ws, [], attack="")

    assert rep.outcome.value == "merged", f"本来就有的 ref 把它拦了: {rep}"


def test_the_baseline_is_taken_before_the_rounds(tmp_path: Path) -> None:
    """基线在轮次循环**外**取，否则这道闸门只拦第一轮。

    形状和第十五个洞（闸门的时刻）一样：如果每轮重取基线，第一轮判红打回后，
    第二轮的基线里那条 ref **已经在了**，差异为空，闸门当场哑掉 —— 而 ref
    还生效着，worker 只要熬过一轮就通关。单元测试对这个错法完全免疫，只有
    跑够两轮才看得见。
    """
    ran: list = []
    rep = _dispatch(tmp_path, _wt(tmp_path), ran, attack="commit")

    assert rep.rounds >= 2, f"没跑够两轮，这条测试证不了基线位置: {rep.rounds}"
    assert rep.outcome.value != "merged", "第二轮放它过去了 —— 基线被每轮重取了"
    assert "replace-refs-changed" in rep.escalation_reason
    assert ran == [], f"任何一轮都不该跑到 check: {ran}"


def test_repointing_an_existing_ref_mid_round_blocks_the_merge(
    tmp_path: Path,
) -> None:
    """条数不变的攻击：那条 ref 名上派发前就坐着一个无害替换物，worker 只把它
    **重新指向**后门 blob。

    这条测试是变异 M108 逼出来的。M108 把判据从 `!=` 换成 `len(now) >
    len(before)`（「多出来才算」），前面所有接线测试照旧全绿 —— 它们的基线都是
    空集，加一条必然让条数变多。重新指向和删除这两种形态它一概不管，而重新
    指向恰恰是威力最大的一支：`git replace -l` 的输出在它前后一字不变。

    函数级测试早就盖住了重新指向，但闸门是不是拿函数的**全部**信息在判，只有
    真跑一轮才看得见 —— 这就是「测接线，不只测行为」。
    """
    ws = _wt(tmp_path)
    clean = _git(ws, "rev-parse", "HEAD:a.py").strip()
    _git(ws, "update-ref", f"refs/replace/{clean}", clean)  # 诱饵：自替换，无害
    before = replace_refs(ws)
    assert len(before) == 1, "前提：派发前就有一条"

    rep = _dispatch(tmp_path, ws, [], attack="blob")

    after = replace_refs(ws)
    assert len(after) == len(before), f"前提：条数没变才能证明这件事: {after}"
    assert after != before, "前提：变的是替换物，不是条数"
    assert rep.outcome.value != "merged", "条数不变的攻击过了 —— 判据只看了条数"
    assert "replace-refs-changed" in rep.escalation_reason
