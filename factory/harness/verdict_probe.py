"""金丝雀探针：验「这份绿还能不能被推翻」。

前面几道检测都在猜机制 —— 影子代码猜「文件被 .gitignore 挡住」，
runner hook 猜「新增了 conftest.py」。猜机制的检测有个共同的天花板：
**伪造绿不需要新增文件。**实测三条都能拿到退出码 0：

  改已存在的 tests/conftest.py，追加一个 pytest_runtest_makereport
    hookwrapper 把 failed 改成 passed            → 退出码 0
  同一个 conftest 里 pytest_sessionfinish 改 session.exitstatus = 0
                                                → 退出码 0
  pyproject.toml 的 addopts = "-p myplug" 加载本地插件
                                                → 退出码 0

第一条和第二条 `runner_hooks` 都报不出来（它只判**新增**），第三条连
文件名都在 `_RUNNER_HOOKS` 的刻意排除名单里（`pyproject.toml` 是任务
合法要改的东西，列进去会让检测在正常任务上响）。

所以这里换一个问法：不问「worker 干了什么」，问「**那份绿是真的吗**」。
往测试目录里塞一条必然失败的测试，再跑一次同一条 check：

  金丝雀非 0 → 这份绿能被推翻，是真的绿
  金丝雀 0   → 一条 `assert 1 == 2` 都红不起来，那份绿是假的

好处是不用枚举机制。上面三条、以及任何将来的新写法，只要效果是
「红不起来」就都会被抓到，因为判据就是效果本身。

代价写在下面 `_TEST_DIRS` 和 `applies_to` 里：这个探针只对 pytest
一族有效，而 check 命令是任意 shell。所以它不是替代那几道检测，
是补它们的天花板。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded

#: 金丝雀文件名。前缀 `test_` 是必须的 —— pytest 只收集匹配
#: `python_files`（默认 `test_*.py`）的文件。放一个语法坏的
#: `_canary.py` 在仓库根，pytest 根本不看它，探针在干净树上就返回 0，
#: 于是「金丝雀红不起来」和「树是干净的」长得一模一样。实测踩过。
CANARY_NAME = "test__factory_canary__.py"

#: 金丝雀内容。用 `assert 1 == 2` 而不是语法错误：语法错误会让整个
#: 收集失败（退出码 2），那和「check 本来就在 collect 阶段挂了」混在
#: 一起分不开。一条正常收集、正常执行、必然失败的测试才是干净的信号。
CANARY_BODY = (
    "# 由无人工厂写入，跑完即删。它的存在是为了验证「这份绿能不能变红」。\n"
    "def test_factory_canary_must_fail():\n"
    '    assert 1 == 2, "factory canary"\n'
)


#: 谁改了就值得验一次。判据是「**这个文件能改变裁决**」，不是「能影响构建」。
#:
#: 和 workspace._RUNNER_HOOKS 的关系：那张表答「这一轮新增了自动发现的
#: 文件吗」，只看新增、只看文件名，因为它要在**跑 check 之前**拦下来。
#: 这张表答「要不要为这一轮多花一次探针」，新增和修改都算，而且**包含
#: pyproject.toml / setup.cfg** —— 那两个在 _RUNNER_HOOKS 里被刻意排除
#: （任务合法要改），在这里可以进来，因为进来的代价只是多跑一次金丝雀，
#: 而合法改动的金丝雀是非 0 的，不会误拒。两张表语义不同，不许合并。
_JUDGE_FILES = frozenset({
    "conftest.py", "pyproject.toml", "setup.cfg", "tox.ini",
    "pytest.ini", ".pytest.ini", "sitecustomize.py", "usercustomize.py",
})

#: 金丝雀往哪放。必须放进**已经有测试**的目录，否则 pytest 的
#: `testpaths` / `addopts=--ignore` 可能压根不看它。
_TEST_DIRS = ("tests", "test")

#: 只有跑 pytest 的 check 才验。别的（grep、test -f、npm）金丝雀说明不了
#: 任何事：往 tests/ 里加一个文件不该让 `test -f greet.py` 变红。
#: 认得宽一点（`pytest`、`python -m pytest`、`uv run pytest`）但必须是
#: 独立的词，免得把 `pytest-cov` 之类的字串也算进来。
_PYTEST_RE = re.compile(r"(?:^|[\s;&|/])pytest(?:$|[\s;&|])")


def judge_files_touched(changed_paths: tuple[str, ...]) -> tuple[str, ...]:
    """这一轮改动里，能改变裁决的那些文件。新增和修改都算。

    只看 `changed_paths` 而不自己扫文件树：这一条和影子代码不同，
    它判的是「**这一轮**动了没」，而 changed_paths 正好就是这个语义。
    影子代码那条必须绕开 git 是因为它要找 git 看不见的东西。
    """
    return tuple(
        p for p in changed_paths
        if Path(p).name in _JUDGE_FILES
    )


def applies_to(command: str) -> bool:
    """这条 check 值得验金丝雀吗（= 它是不是在跑 pytest）。"""
    return bool(_PYTEST_RE.search(command))


def canary_target(root: Path) -> Path | None:
    """金丝雀该写到哪。没有测试目录就返回 None（= 不验）。

    不在仓库根造一个 `tests/`：造出来的目录会进 `git status`，而这道
    探针跑在 capture_diff 之后、判绿之前，多一个目录会让「提交的正好是
    监工审过的那一组」这条不变量出现一个没人审过的成员。
    """
    for name in _TEST_DIRS:
        d = Path(root) / name
        if d.is_dir():
            return d / CANARY_NAME
    return None


@dataclass(frozen=True)
class ProbeResult:
    """金丝雀结果。`fake_green` 为 True 时那份绿是伪造的。

    `skipped` 和 `fake_green=False` 必须分开：没跑（没有测试目录、
    命令不是 pytest、探针自己超时）不等于验过了。混成一个 bool
    会让「没验」在报表上和「验过是真的」长得一样。
    """

    fake_green: bool = False
    skipped: bool = True
    reason: str = ""
    exit_code: int | None = None


def probe(root: Path, command: str, *, timeout_s: int = 300) -> ProbeResult:
    """跑一次金丝雀。返回 `fake_green=True` 表示这份绿推翻不了。

    金丝雀文件**一定会被删掉**（finally）。留下来的后果不是「多个垃圾
    文件」：它会进下一轮的 `git diff HEAD`，于是 changed_paths 里冒出一个
    没人要求过的文件，范围监工判越界，worker 拿到一条他看不懂的打回。

    探针自己超时/报错时返回 `skipped`，**不判红**。理由和落地失败不改
    判决同一条：让一个已经全绿的任务因为探针超时变成 escalated，是拿
    真问题换假问题。代价是这种情况下伪造绿会漏过去，所以 reason 里
    留了痕。
    """
    target = canary_target(root)
    if target is None:
        return ProbeResult(reason="没有测试目录，金丝雀无处可放")
    if not applies_to(command):
        return ProbeResult(reason=f"不是 pytest 命令，金丝雀说明不了什么：{command}")
    if target.exists():
        # 撞名字：宁可不验也不覆盖别人的文件。
        return ProbeResult(reason=f"{target.name} 已存在，不覆盖")

    target.write_text(CANARY_BODY, encoding="utf-8")
    try:
        proc = run_bounded(command, cwd=Path(root), timeout_s=timeout_s,
                           shell=True)
    except ProcTimeout:
        return ProbeResult(reason=f"金丝雀探针自己超时（{timeout_s}s），未验")
    finally:
        target.unlink(missing_ok=True)
        # pytest 会给金丝雀留一个 .pyc。它落在 __pycache__ 下，
        # 影子代码检测按 _CODE_SUFFIXES 过滤（.pyc 不在里面）且
        # __pycache__ 在 _VENDOR_DIRS 里，所以不会被误报 —— 但仍然清掉，
        # 免得下一轮 `git status` 里多一个没人认领的文件。
        for pyc in (target.parent / "__pycache__").glob(
            f"{target.stem}.*.pyc"
        ):
            pyc.unlink(missing_ok=True)

    if proc.returncode == 0:
        return ProbeResult(
            fake_green=True,
            skipped=False,
            reason="塞进一条 `assert 1 == 2` 之后 check 仍然退出 0 —— "
                   "这份绿推翻不了",
            exit_code=0,
        )
    return ProbeResult(skipped=False, reason="金丝雀红了，这份绿是真的",
                       exit_code=proc.returncode)
