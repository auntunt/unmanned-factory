"""binary 预检。真实故障：launchd 的 PATH 里没有 nvm 装的 claude。"""
import os
import stat

import pytest

from factory.harness.preflight import PreflightError, resolve_binary


def _exe(p):
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def test_a_binary_on_path_resolves_to_an_absolute_path(tmp_path, monkeypatch):
    _exe(tmp_path / "faux")
    monkeypatch.setenv("PATH", str(tmp_path))
    got = resolve_binary("faux")
    assert got.is_absolute() and got.name == "faux"


def test_a_binary_missing_from_path_names_the_path_it_searched(
        tmp_path, monkeypatch):
    # 报错必须带上当时的 PATH。这个故障的全部难点就是「PATH 和我以为的不一样」,
    # 只说「找不到 claude」会让人去查安装，而装是装好的。
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(PreflightError) as ei:
        resolve_binary("claude")
    msg = str(ei.value)
    assert str(tmp_path) in msg
    assert "launchd" in msg, "得点出定时任务的 PATH 更短，否则人查不到方向"


def test_a_path_with_a_slash_is_not_searched_on_path(tmp_path, monkeypatch):
    """含斜杠时按路径处理，和 execvp 语义一致。

    否则 `--binary ./wrapper.sh` 会被拿去 PATH 里搜，然后报一个误导人的
    「不在 PATH 里」—— 而问题其实是相对路径相对错了目录。
    """
    _exe(tmp_path / "wrapper.sh")
    monkeypatch.setenv("PATH", str(tmp_path))  # 就算在 PATH 上也不该走 which
    with pytest.raises(PreflightError, match="不存在"):
        resolve_binary("./wrapper.sh")


def test_an_absolute_path_that_exists_resolves(tmp_path):
    p = _exe(tmp_path / "faux")
    assert resolve_binary(str(p)) == p.resolve()


def test_a_file_without_the_execute_bit_is_rejected(tmp_path):
    p = tmp_path / "faux"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o644)
    with pytest.raises(PreflightError, match="执行位"):
        resolve_binary(str(p))


def test_a_directory_is_not_a_binary(tmp_path):
    # `--binary ~/.nvm/versions/node/v24.16.0/bin` 这类少打一段的写法。
    # 不查这个的话报错会是 exec 时的 Permission denied，指向完全错误的方向。
    with pytest.raises(PreflightError, match="不是文件"):
        resolve_binary(str(tmp_path))


def test_the_real_launchd_path_does_not_find_claude(monkeypatch):
    """钉住这个功能存在的理由本身。

    launchd 给的默认 PATH 就是这四个目录。claude 装在 nvm 下，所以这里必须
    抛 —— 哪天它真在 /usr/bin 里了，这条会挂，那时该重读这个模块的必要性。
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    with pytest.raises(PreflightError):
        resolve_binary("claude")


def test_an_empty_path_is_reported_as_empty_not_as_blank(monkeypatch):
    # PATH 被清空时 which 也返回 None。报错里如果是空白，人会以为是排版问题。
    monkeypatch.setenv("PATH", "")
    with pytest.raises(PreflightError, match=r"\(空\)"):
        resolve_binary("claude")


def test_path_is_read_at_call_time_not_import_time(tmp_path, monkeypatch):
    # 模块级缓存 PATH 会让 loop 在 launchd 下的表现和测试里不一样。
    _exe(tmp_path / "faux")
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    with pytest.raises(PreflightError):
        resolve_binary("faux")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_binary("faux").name == "faux"


def test_tilde_is_expanded(tmp_path, monkeypatch):
    # plist 里写 ~/.nvm/... 很自然，但没有 shell 帮忙展开。
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "bin").mkdir()
    _exe(tmp_path / "bin" / "faux")
    assert resolve_binary("~/bin/faux").name == "faux"
    assert os.path.isabs(resolve_binary("~/bin/faux"))
