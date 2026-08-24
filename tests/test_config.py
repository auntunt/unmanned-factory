"""四级配置优先序：命令行 > 环境变量 > 项目配置 > 内置默认。

这个模块存在的全部理由是让「跑一次真实 loop」不必在命令行上背二十个参数。
所以测试的重点不是「能读 TOML」，而是**优先序在任何组合下都不翻转** ——
翻转一次的代价是沙箱以为开了其实没开，而这件事没有任何日志会告诉你。
"""

from __future__ import annotations

import pytest

from factory.config import (
    ConfigError,
    explain,
    explicit_keys,
    from_env,
    from_file,
    resolve,
)


def write_cfg(tmp_path, body: str):
    p = tmp_path / "factory.toml"
    p.write_text(body, encoding="utf-8")
    return p


# ---------- 单层来源 ----------

def test_missing_file_is_not_an_error(tmp_path):
    """配置是可选的：没有 factory.toml 时一切照跑。

    内置默认必须永远能单机跑起来，否则「装好」和「能跑」之间又有断层了。
    """
    assert from_file(tmp_path / "nope.toml") == {}


def test_only_the_factory_table_is_read(tmp_path):
    """只认 [factory]，别的表无视。

    项目会想把 factory 配置和别的东西放同一个文件里。铺平整个文档会让
    无关的键变成「不认识的配置」而报错退出 —— 那是把可选功能变成了地雷。
    """
    p = write_cfg(tmp_path, '[other]\nwhatever = 1\n\n[factory]\ndb = "x.db"\n')
    assert from_file(p) == {"db": "x.db"}


def test_hyphens_in_toml_keys_are_accepted(tmp_path):
    """TOML 惯例是连字符，Python 惯例是下划线。两个都收。

    不收的话，写 judge-binary 的人得到的是「不认识的配置」，而那个名字
    正是 --judge-binary 长得样子。
    """
    p = write_cfg(tmp_path, '[factory]\njudge-binary = "j"\nglobal-runbook = true\n')
    assert from_file(p) == {"judge_binary": "j", "global_runbook": True}


def test_env_vars_are_typed_like_file_values():
    """环境变量只有字符串，但转换后必须和 TOML 里写同样的值等价。

    不统一的后果是「TOML 里 timeout=900 能用、FACTORY_TIMEOUT=900 变成
    字符串 '900'」，然后在某个做算术的地方炸掉，堆栈离配置十万八千里。
    """
    got = from_env({"FACTORY_TIMEOUT": "900", "FACTORY_BUDGET_USD": "20",
                    "FACTORY_SANDBOX": "true", "FACTORY_DB": "a.db"})
    assert got == {"timeout": 900, "budget_usd": 20.0,
                   "sandbox": True, "db": "a.db"}


@pytest.mark.parametrize("raw,want", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
])
def test_bool_spellings(raw, want):
    """真假值的写法不该考记忆。1/true/yes/on 都收，大小写不敏感。"""
    assert from_env({"FACTORY_SANDBOX": raw}) == {"sandbox": want}


def test_a_bad_bool_says_what_it_wanted():
    """报错要说「要真假值、给的是啥、可以怎么写」，三样齐全。"""
    with pytest.raises(ConfigError) as e:
        from_env({"FACTORY_SANDBOX": "maybe"})
    msg = str(e.value)
    assert "真假值" in msg and "maybe" in msg and "true/false" in msg


def test_unknown_env_var_is_rejected_not_ignored():
    """拼错的 FACTORY_* 必须报错。

    静默忽略 FACTORY_SANBOX 的结果是沙箱没开而你以为开了 —— 这类错误在
    正常输出里一个字都看不见，直到某个 worker 写了 $HOME。
    """
    with pytest.raises(ConfigError) as e:
        from_env({"FACTORY_SANBOX": "true"})
    assert "FACTORY_SANBOX" in str(e.value)
    assert "FACTORY_SANDBOX" in str(e.value)  # 列出可用键，方便看出拼错在哪


def test_unknown_file_key_is_rejected(tmp_path):
    """同理，配置文件里拼错也要报错并列出可用键。"""
    p = write_cfg(tmp_path, "[factory]\nsanbox = true\n")
    with pytest.raises(ConfigError) as e:
        from_file(p)
    assert "sanbox" in str(e.value) and "sandbox" in str(e.value)


def test_broken_toml_names_the_file(tmp_path):
    """语法错要指名文件 —— 一台机器上可能有好几个候选路径。"""
    p = write_cfg(tmp_path, "[factory]\nport = = 1\n")
    with pytest.raises(ConfigError) as e:
        from_file(p)
    assert str(p) in str(e.value)


def test_workspace_cannot_come_from_config(tmp_path):
    """--workspace 不在白名单里：配置不该预设「要动哪个仓库」。

    一个忘了清理的配置文件能让 factory 去动一个你没打算动的仓库，
    而它做的第一件事是让 worker 往里写代码。
    """
    p = write_cfg(tmp_path, '[factory]\nworkspace = "/somewhere/else"\n')
    with pytest.raises(ConfigError) as e:
        from_file(p)
    assert "workspace" in str(e.value)


# ---------- 优先序 ----------
#
# 下面每条都是一个「哪层赢」的断言。分开写而不是一条大用例：混在一起时
# 失败信息只告诉你「优先序坏了」，而四层之间有六种两两关系，逐一定位比
# 重新读一遍实现还慢。

def test_cli_beats_env(tmp_path):
    r = resolve({"port": 1}, cli_explicit={"port"},
                environ={"FACTORY_PORT": "2"})
    assert r.values["port"] == 1
    assert r.origin_of("port") == "命令行"


def test_env_beats_file(tmp_path):
    p = write_cfg(tmp_path, "[factory]\nport = 3\n")
    r = resolve({}, cli_explicit=set(), project_file=p,
                environ={"FACTORY_PORT": "2"})
    assert r.values["port"] == 2
    assert "FACTORY_PORT" in r.origin_of("port")


def test_file_beats_builtin_default(tmp_path):
    p = write_cfg(tmp_path, "[factory]\nport = 3\n")
    r = resolve({"port": 8788}, cli_explicit=set(), project_file=p)
    assert r.values["port"] == 3
    assert str(p) in r.origin_of("port")


def test_builtin_default_is_the_floor():
    """四层都没给时用 argparse 的默认值，并标明来源是内置默认。"""
    r = resolve({"port": 8788}, cli_explicit=set())
    assert r.values["port"] == 8788
    assert r.origin_of("port") == "内置默认"


def test_an_explicit_cli_value_equal_to_the_default_still_wins(tmp_path):
    """**最容易写错的一条**：显式给的值恰好等于 argparse 默认值。

    如果用「Namespace 的值 != default」来判断有没有显式指定，这里会误判成
    「用户没给」，于是配置文件的 9999 赢 —— 用户明明在命令行上写了 8788。
    显式的命令行不能输给任何东西，这是优先序里最不能破的一条。
    """
    p = write_cfg(tmp_path, "[factory]\nport = 9999\n")
    r = resolve({"port": 8788}, cli_explicit={"port"}, project_file=p)
    assert r.values["port"] == 8788, "显式命令行输给了配置文件"
    assert r.origin_of("port") == "命令行"


def test_layers_merge_per_key_not_wholesale(tmp_path):
    """合并是逐键的，不是「哪层有值就整层生效」。

    整层生效的话，配置文件里写了一个键就会把环境变量注入的凭据全抹掉。
    """
    p = write_cfg(tmp_path, '[factory]\ndb = "file.db"\nport = 3\ntimeout = 111\n')
    r = resolve({"port": 8788, "timeout": 900, "db": "d.db", "binary": "claude"},
                cli_explicit={"port"}, project_file=p,
                environ={"FACTORY_TIMEOUT": "222"})
    assert r.values["port"] == 8788        # 命令行
    assert r.values["timeout"] == 222      # 环境变量
    assert r.values["db"] == "file.db"     # 项目配置
    assert r.values["binary"] == "claude"  # 内置默认


def test_every_value_carries_an_origin(tmp_path):
    """每个值都要能回答「你从哪来」，一个不能少。

    排查配置问题时人只问这一个问题，缺一个来源就得回去翻四层文件。
    """
    p = write_cfg(tmp_path, '[factory]\ndb = "f.db"\n')
    r = resolve({"port": 8788, "db": "d.db", "timeout": 900},
                cli_explicit={"port"}, project_file=p,
                environ={"FACTORY_TIMEOUT": "222"})
    assert set(r.origins) == set(r.values)


# ---------- explicit_keys ----------

def test_explicit_keys_reads_argv_not_values():
    dest_of = {"--port": "port", "-p": "port", "--db": "db",
               "--judge-binary": "judge_binary"}
    assert explicit_keys(["api", "--port", "8788"], dest_of) == {"port"}
    assert explicit_keys(["api", "--port=8788"], dest_of) == {"port"}
    assert explicit_keys(["api", "-p", "1"], dest_of) == {"port"}
    assert explicit_keys(["api", "--judge-binary", "j"], dest_of) == {"judge_binary"}
    assert explicit_keys(["api"], dest_of) == set()


def test_explicit_keys_ignores_positionals_that_look_like_flags():
    """任务 ID 之类的位置参数里可能有横线，别把它当选项。"""
    dest_of = {"--port": "port"}
    assert explicit_keys(["run", "T-some-task", "--port", "1"], dest_of) == {"port"}


# ---------- explain ----------

def test_explain_lines_up_even_with_long_values(tmp_path):
    """ORIGIN 列必须对齐 —— 这张表的全部价值在于竖着扫得动。

    列宽写死的实现会被一个长路径顶歪，而长路径在真实配置里是常态
    （/home/ubuntu/.npm-global/bin/claude）。
    """
    r = resolve({"db": "/very/long/path/to/an/audit/database/audit.db",
                 "port": 8788}, cli_explicit=set())
    lines = explain(r).splitlines()
    col = lines[0].index("ORIGIN")
    for line in lines[1:]:
        assert line.index("内置默认") == col, f"ORIGIN 列没对齐：{line!r}"


def test_explain_says_so_when_there_is_nothing():
    """空配置要说「全部走内置默认」，不能给一张空表让人以为读失败了。"""
    assert "内置默认" in explain(resolve({}, cli_explicit=set()))
