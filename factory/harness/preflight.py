"""开跑前确认 worker 可执行体真的能找到。

存在的理由很具体：`claude` 装在 nvm 管的目录下
（`~/.nvm/versions/node/v24.16.0/bin/claude`），而 launchd 给的默认 PATH 是
`/usr/bin:/bin:/usr/sbin:/sbin`。所以一个「照着 README 写的」定时任务会在
**每一次派发**时找不到 binary，把整条队列刷成 error —— 而 `version()` 吞掉
OSError 只回 "unknown"（那是对的，探针失败不该杀掉一次运行），于是这个故障
在日志里没有任何早期信号。

`build_adapter` 只校验 harness 的**名字**，不校验 binary 能不能跑。这个模块
补的就是这一格，并且刻意只在 `loop` 的入口调用一次：`run` 是人敲的，敲错了
当场就看见；`loop` 一旦跑起来就没人看着了。
"""

from __future__ import annotations

import shutil
from pathlib import Path


class PreflightError(RuntimeError):
    """binary 找不到。开跑之前抛，不是跑到一半才抛。"""


def resolve_binary(name: str) -> Path:
    """把 argv[0] 解析成一个真实存在且可执行的路径。

    含斜杠时当路径处理，不查 PATH —— 这跟 execvp 的语义一致，
    否则 `--binary ./wrapper.sh` 会被拿去 PATH 里搜一遍然后报一个
    误导人的「不在 PATH 里」。
    """
    if "/" in name:
        p = Path(name).expanduser()
        if not p.exists():
            raise PreflightError(f"worker 可执行体不存在：{p}")
        if not p.is_file():
            raise PreflightError(f"worker 可执行体不是文件：{p}")
        if not _executable(p):
            raise PreflightError(f"worker 可执行体没有执行位：{p}")
        return p.resolve()

    found = shutil.which(name)
    if found is None:
        raise PreflightError(
            f"PATH 里找不到 worker 可执行体 `{name}`。\n"
            f"  当前 PATH: {_path_hint()}\n"
            "  定时任务（launchd/cron）拿到的 PATH 比登录 shell 短得多，"
            "nvm/asdf 装的 binary 通常不在里面。\n"
            "  两个改法：给 --binary 传绝对路径，或在 plist 里显式设 PATH。"
        )
    return Path(found).resolve()


def _executable(p: Path) -> bool:
    import os

    return os.access(p, os.X_OK)


def _path_hint() -> str:
    import os

    raw = os.environ.get("PATH", "")
    return raw if raw else "(空)"
