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
