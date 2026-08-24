"""四级配置优先序：命令行 > 环境变量 > 项目配置 > 内置默认。

## 为什么需要这一层

在这之前所有默认值都写死在 argparse 里（`--db audit.db`、`--binary claude`、
`--timeout 900` 等四十多处）。后果是跑一次真实的 loop 要在命令行上背一长串
参数：

    factory loop --workspace ~/proj --db ~/data/audit.db --queue ~/data/queue \
      --binary /home/ubuntu/.npm-global/bin/claude --judge-binary ... \
      --sandbox --global-runbook --runbook ~/rules.yaml --budget-usd 20 ...

这串东西**每次都要一模一样**，因为它描述的是这台机器的装配（claude 装在哪、
队列放哪、预算多少），不是这一次运行的意图。人肉重复输入的直接代价是漂移：
systemd 单元里写了 `--sandbox`，手动排查时忘了写，于是「手动能复现、服务里
不能」——而这个差异在任何日志里都看不出来。

## 优先序为什么是这个顺序

命令行最高：它是当下这一次的显式意图，任何配置都不该盖过一次显式指定。
环境变量次之：systemd/CI 注入的是这套部署的事实（凭据、路径），比仓库里
签入的配置更贴近运行现场，而且不该要求为了改一个路径去改文件。
项目配置再次：签入仓库、多人共享的约定（这个项目用哪个 harness、要不要开
全局 runbook）。放在环境变量之下，是因为它签入了版本库 —— 一份签入的配置
不该悄悄改变某台机器上的实际行为。
内置默认最低，且必须**永远能单机跑起来**：没有配置文件、没有环境变量、
只给必填参数时，`factory run` 要能工作。

## 刻意不做的事

不支持配置文件里再套 include/继承。四级已经足够解释「这个值从哪来」，
第五级会让 `factory config --explain` 的输出没人看得懂 —— 而排查配置问题时
唯一真正有用的东西就是「这个值从哪来」。

不做全局 `~/.factory.toml`。多个项目共用一份全局配置时，`--workspace` 指向
哪个项目决定了哪些值该生效，这个关系没法在文件里表达清楚，最后一定变成
「我在 A 项目改了配置，B 项目行为变了」。要复用就用环境变量。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 配置文件名。放在 workspace 之外由调用方决定路径 —— 和 runbook 同一个理由：
# worker 能写 workspace，从那儿读配置等于让它自己决定沙箱开不开。
CONFIG_NAME = "factory.toml"

# 环境变量前缀。FACTORY_DB、FACTORY_BINARY、FACTORY_BUDGET_USD……
ENV_PREFIX = "FACTORY_"

# 哪些键允许从配置来。白名单而不是「argparse 有什么就认什么」：
# --workspace 这类「这一次要做什么」的参数不该被配置文件预设，否则一个
# 忘了清理的配置能让 factory 去动一个你没打算动的仓库。
#
# 值是类型转换函数，用来把字符串（环境变量只有字符串）变成目标类型。
_SCHEMA: dict[str, type] = {
    "db": str,
    "queue": str,
    "binary": str,
    "judge_binary": str,
    "judge_model": str,
    "harness": str,
    "timeout": int,
    "max_turns": int,
    "worktree_root": str,
    "runbook": str,
    "global_runbook": bool,
    "sandbox": bool,
    "spec_review": bool,
    "architecture_review": bool,
    "budget_usd": float,
    "max_tasks": int,
    "max_runtime": float,
    "poll": float,
    "port": int,
}


class ConfigError(Exception):
    """配置本身有问题（语法坏了、键不认识、值转不过去）。

    单独一个异常类型，因为配置错误和运行错误的处理方式不同：配置错必须
    在动任何仓库之前就退出，而且报错要指名文件和键。
    """


def _coerce(key: str, raw: Any, want: type) -> Any:
    """把配置里的原始值转成 schema 要求的类型。

    环境变量只能是字符串，TOML 则已经有类型了 —— 两条来源都走这里，
    免得「TOML 里写 true 能用、环境变量里写 true 不能用」这种不一致。
    """
    if want is bool:
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ConfigError(
            f"{key} 要的是真假值，给的是 {raw!r}。"
            f"可写 true/false（也认 1/0、yes/no、on/off）"
        )
    try:
        return want(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{key} 要的是 {want.__name__}，给的是 {raw!r}（{exc}）"
        ) from None


def from_env(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """收环境变量里的 FACTORY_* 配置。

    不认识的 FACTORY_* 一律报错，不静默忽略：拼错的环境变量
    （FACTORY_SANBOX）静默忽略的结果是沙箱没开而你以为开了，
    而这类错误在正常输出里一个字都看不见。
    """
    env = os.environ if environ is None else environ
    out: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        key = name[len(ENV_PREFIX):].lower()
        if key not in _SCHEMA:
            raise ConfigError(
                f"不认识的环境变量 {name}。"
                f"可用的有：{', '.join(ENV_PREFIX + k.upper() for k in sorted(_SCHEMA))}"
            )
        out[key] = _coerce(name, raw, _SCHEMA[key])
    return out


def from_file(path: str | Path) -> dict[str, Any]:
    """读 factory.toml。文件不存在返回空字典（配置是可选的）。

    只认顶层的 [factory] 表。不铺平整个文件，因为 TOML 里放别的表是常见的
    （项目可能想把 factory 配置塞进已有的 pyproject.toml 之外的文件里），
    而铺平会让无关的键变成「不认识的配置」报错。
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open("rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} 不是合法的 TOML：{exc}") from None
    section = doc.get("factory", {})
    if not isinstance(section, dict):
        raise ConfigError(f"{p} 里的 [factory] 必须是一张表，当前是 {type(section).__name__}")
    out: dict[str, Any] = {}
    for key, raw in section.items():
        k = key.replace("-", "_")  # 允许 TOML 里写 judge-binary
        if k not in _SCHEMA:
            raise ConfigError(
                f"{p} 里有不认识的配置 {key}。"
                f"可用的有：{', '.join(sorted(_SCHEMA))}"
            )
        out[k] = _coerce(f"{p}:{key}", raw, _SCHEMA[k])
    return out


@dataclass(frozen=True)
class Resolved:
    """合并结果 + 每个值的来源。

    来源必须一起返回，不能只给值：配置出问题时人问的永远是「为什么沙箱没开」，
    答案是「被 factory.toml 里的 sandbox=false 盖掉了」。只给值的话这个问题
    要靠人逐层翻文件回答，而四层里任何一层都可能是元凶。
    """

    values: dict[str, Any]
    origins: dict[str, str]

    def origin_of(self, key: str) -> str:
        return self.origins.get(key, "内置默认")


def resolve(
    cli: dict[str, Any],
    *,
    cli_explicit: set[str],
    project_file: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> Resolved:
    """按四级优先序合并：命令行 > 环境变量 > 项目配置 > 内置默认。

    `cli_explicit` 是「用户真的在命令行上写了的键」。这个参数不能省 ——
    argparse 的 Namespace 分不清「用户显式给了 --db audit.db」和「用户没给，
    argparse 填了默认值 audit.db」，两者在 ns.db 上长得一模一样。分不清的
    后果是 argparse 默认值会盖掉配置文件，那配置文件就永远不生效了，
    而且是静默不生效 —— 这是这一层最容易写错的地方。
    """
    values: dict[str, Any] = {}
    origins: dict[str, str] = {}

    # 从低到高铺，后写的盖前面的
    if project_file is not None:
        for k, v in from_file(project_file).items():
            values[k], origins[k] = v, f"项目配置 {project_file}"

    for k, v in from_env(environ).items():
        values[k], origins[k] = v, f"环境变量 {ENV_PREFIX}{k.upper()}"

    for k in cli_explicit:
        if k in cli:
            values[k], origins[k] = cli[k], "命令行"

    # 兜底：schema 里的键如果四层都没给，用 argparse 的默认值填上，
    # 让调用方拿到的是一份完整的配置，不用自己再判断 key 在不在。
    for k, v in cli.items():
        if k in _SCHEMA and k not in values:
            values[k], origins[k] = v, "内置默认"

    return Resolved(values=values, origins=origins)


def explain(r: Resolved) -> str:
    """人能读的配置来源表，给 `factory config` 用。

    等宽对齐、一屏看完，最长的那列（来源）放最后 —— 排查时眼睛是顺着
    「哪个键」找到「从哪来」，反过来排列要横跳。
    """
    if not r.values:
        return "（没有任何配置，全部走内置默认）"
    # 列宽按实际内容算，不用固定值：写死 18 时一个长路径就把 ORIGIN 列顶歪，
    # 而这张表的全部价值在于竖着扫得动。
    kw = max(len(k) for k in r.values)
    kw = max(kw, len("KEY"))
    vw = max(len(str(v)) for v in r.values.values())
    vw = max(vw, len("VALUE"))
    lines = [f"{'KEY'.ljust(kw)}  {'VALUE'.ljust(vw)}  ORIGIN"]
    for k in sorted(r.values):
        val = str(r.values[k])
        lines.append(f"{k.ljust(kw)}  {val.ljust(vw)}  {r.origin_of(k)}")
    return "\n".join(lines)


def explicit_keys(argv: list[str], dest_of: dict[str, str]) -> set[str]:
    """从原始 argv 里找出用户真的写了哪些选项。

    为什么扫 argv 而不是比对「Namespace 的值 != default」：值恰好等于默认值
    时那种比法会把显式指定误判成没指定。用户显式写 `--sandbox` 而配置文件里
    是 false 时，误判会让配置文件赢 —— 显式的命令行输不给任何东西，这是
    优先序里最不能破的一条。

    `dest_of` 把选项字符串映射到 argparse 的 dest（`--judge-binary` →
    `judge_binary`），由调用方从 parser 里取，这样新增选项不用改这里。
    """
    seen: set[str] = set()
    for tok in argv:
        if not tok.startswith("-"):
            continue
        opt = tok.split("=", 1)[0]
        if dest := dest_of.get(opt):
            seen.add(dest)
    return seen
