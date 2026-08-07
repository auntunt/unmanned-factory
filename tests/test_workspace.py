import subprocess

import pytest

from factory.harness.workspace import capture_diff, diff_hash, has_baseline


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "kept.txt").write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def test_has_baseline(tmp_path, repo):
    assert has_baseline(repo) is True
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    assert has_baseline(empty) is False


def test_empty_diff(repo):
    diff, paths = capture_diff(repo)
    assert diff == ""
    assert paths == ()
    assert diff_hash(diff) is None


def test_captures_modified_added_and_nested(repo):
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
    (repo / "added.txt").write_text("new\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")

    diff, paths = capture_diff(repo)
    assert set(paths) == {"kept.txt", "added.txt", "sub/deep.txt"}
    assert "changed" in diff and "deep" in diff


def test_captures_deletion(repo):
    (repo / "kept.txt").unlink()
    diff, paths = capture_diff(repo)
    assert paths == ("kept.txt",)
    assert "deleted file" in diff or "-original" in diff


def test_intent_to_add_does_not_stage_content(repo):
    (repo / "added.txt").write_text("new\n", encoding="utf-8")
    capture_diff(repo)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--stat"], cwd=repo,
        capture_output=True, text=True,
    ).stdout
    # -N 只登记 intent-to-add，内容没有被 stage
    assert "new" not in staged


def test_diff_hash_is_stable_and_content_sensitive(repo):
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
    d1, _ = capture_diff(repo)
    assert diff_hash(d1) == diff_hash(d1)
    (repo / "kept.txt").write_text("changed twice\n", encoding="utf-8")
    d2, _ = capture_diff(repo)
    assert diff_hash(d1) != diff_hash(d2)


def test_capture_diff_without_baseline_raises(tmp_path):
    _git(tmp_path, "init", "-q")
    with pytest.raises(RuntimeError, match="commit"):
        capture_diff(tmp_path)


# ---------- 架构监工的周边代码 ----------

def test_neighbour_context_gives_sibling_code_but_not_the_changed_file(tmp_path):
    """改动本身在 diff 里已经给过。重复给会让「哪些是新写的」变模糊，
    而判重复实现恰恰要分清这个。"""
    from factory.harness.workspace import neighbour_context

    pkg = tmp_path / "factory"
    pkg.mkdir()
    (pkg / "text.py").write_text("def slugify(s): return s.lower()\n")
    (pkg / "new.py").write_text("def slugify(s): return s.lower()\n")

    ctx = neighbour_context(tmp_path, ("factory/new.py",))
    assert "factory/text.py" in ctx
    assert "factory/new.py" not in ctx


def test_neighbour_context_skips_noise_and_non_code(tmp_path):
    from factory.harness.workspace import neighbour_context

    pkg = tmp_path / "src"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "notes.md").write_text("# 不是代码\n")
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "a.pyc").write_text("junk")

    ctx = neighbour_context(tmp_path, ("src/b.py",))
    assert "src/a.py" in ctx
    assert "notes.md" not in ctx
    assert "pycache" not in ctx


def test_neighbour_context_is_empty_for_a_missing_directory(tmp_path):
    from factory.harness.workspace import neighbour_context

    assert neighbour_context(tmp_path, ("nope/x.py",)) == ""


def test_neighbour_context_respects_the_byte_budget(tmp_path):
    """给监工的上下文必须有上限，否则一个大目录能把 prompt 撑爆。"""
    from factory.harness.workspace import neighbour_context

    pkg = tmp_path / "big"
    pkg.mkdir()
    for i in range(5):
        (pkg / f"f{i}.py").write_text("# pad\n" * 500)

    ctx = neighbour_context(tmp_path, ("big/new.py",), max_bytes=1000)
    assert 0 < len(ctx) < 4000
