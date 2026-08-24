"""副作用隔离：把 worker 的 argv 包一层沙箱。

**平台分发层。** 本模块根据 OS 导入对应后端：
  - macOS: sandbox_macos（原 sandbox.py 主体，Seatbelt / sandbox-exec）
  - Linux: sandbox_linux（bubblewrap / bwrap）
  - 其他: 不支持，available() 返回 False

两个后端接口形状一致：
  - available() -> bool
  - prepare(argv, workspace, stack) -> (wrapped_argv, env_overrides)
  - tag_version(raw, enabled) -> str
  - SANDBOX_BINARY: str
  - SandboxUnavailable

调用方（shell.py / claude_code.py / cli.py）只 import sandbox，不关心平台。

**这一层不是 D 类硬闸门的替代，也不能成为它的理由。**
硬闸门在派发**之前**判，压根不启动 worker；沙箱在 worker **运行时**兜底。
不允许出现「有沙箱了所以 D 类可以放宽」的推理：沙箱挡不住已经拿到
生产凭据的进程去调用远端 API，它只挡本机文件。两层各挡各的。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 类型检查时固定看 Linux 后端：三个后端接口形状一致，随便挑一个当契约。
    # 不这么写的话，else 分支里无条件 raise 的 prepare 会被推断成 NoReturn，
    # 跟真实后端的返回类型冲突，Pyright 报 reportAssignmentType。
    from factory.harness.sandbox_linux import (
        SANDBOX_BINARY as SANDBOX_BINARY,
        SandboxUnavailable as SandboxUnavailable,
        available as available,
        prepare as prepare,
        tag_version as tag_version,
    )
elif sys.platform == "darwin":
    from factory.harness.sandbox_macos import (
        SANDBOX_BINARY,
        SandboxUnavailable,
        available,
        prepare,
        tag_version,
    )
elif sys.platform.startswith("linux"):
    from factory.harness.sandbox_linux import (
        SANDBOX_BINARY,
        SandboxUnavailable,
        available,
        prepare,
        tag_version,
    )
else:
    SANDBOX_BINARY = ""

    class SandboxUnavailable(RuntimeError):
        """沙箱不可用（非 macOS/Linux）。"""

    def available() -> bool:
        """本平台不支持沙箱。"""
        return False

    def prepare(argv, workspace, stack):
        """fallback：抛异常，不许静默降级。"""
        raise SandboxUnavailable(f"沙箱在 {sys.platform} 上不可用。")

    def tag_version(raw: str, enabled: bool) -> str:
        """fallback：不加后缀。"""
        return raw[:64]


__all__ = ["SANDBOX_BINARY", "SandboxUnavailable", "available", "prepare", "tag_version"]
