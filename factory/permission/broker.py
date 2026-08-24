"""把 PermissionGate 架在一个 Unix socket 上，供沙箱里的 hook 客户端查询。

为什么要隔一层进程通信，而不是让 hook 直接调 PermissionGate：

  1. 沙箱把工厂代码 tmpfs 掉了（sandbox_linux 第 2 段），hook 里 import
     factory.* 必然失败，而 hook 失败 = 静默放行。
  2. GateStats 要在**一次 attempt** 的粒度上累计，好让 dispatcher 收尾时
     一次性落库。每个 hook 进程各自持有一个 gate 的话，统计全丢。
  3. 模型审批者（第 2 层）要在沙箱**外**跑：它自己也是一次 claude 调用，
     塞进沙箱里就得把 binary 和凭据一起放进去，等于给 worker 开后门。

socket 而非 HTTP：不占端口、随目录权限走（0600 + 私有父目录），
不用担心并行派发撞端口。

并发：worker 的工具调用本身是串行的，但 CLI 会并发发起多个（比如同时读几个
文件），所以每个连接一个线程。gate 的记账加锁——GateStats.record 会改四个
计数器，没锁的话并发下会丢事件，而丢的恰好是审计要的那一侧。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import stat
import tempfile
import threading
from pathlib import Path

from factory.permission.gate import PermissionGate
from factory.permission.rules import Decision

#: hook 脚本在沙箱里的落脚名。见 install_hook。
_HOOK_NAME = "factory_gate_hook.py"

#: accept 的轮询间隔。要够小让 stop() 及时返回，够大不空转烧 CPU。
_ACCEPT_TIMEOUT_S = 0.25


class GateBroker:
    """gate 的 socket 服务端。用作 context manager。

        with GateBroker(gate) as broker:
            settings = broker.settings_json()      # 喂给 --settings
            env = {broker.socket_env: broker.socket_path}
            ...  # 跑 worker
        # 退出时 socket 关掉、临时目录清掉，gate.stats 留着给 dispatcher 落库
    """

    socket_env = "FACTORY_GATE_SOCKET"

    def __init__(self, gate: PermissionGate, *, python: str | None = None) -> None:
        self._gate = gate
        self._lock = threading.Lock()
        # 用哪个 python 跑 hook。默认 sys.executable：venv 里的解释器保证存在。
        # 沙箱模式下调用方要换成沙箱内可见的路径（见 claude_code.py）。
        self._python = python or _default_python()
        self._dir: Path | None = None
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._hook_path: Path | None = None

    # ---- 生命周期 ----

    def __enter__(self) -> GateBroker:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def start(self) -> None:
        # 0700 的私有父目录：socket 自己的 mode 在部分平台不被 connect 检查，
        # 真正管住访问的是父目录权限。
        d = Path(tempfile.mkdtemp(prefix="factory-gate-"))
        os.chmod(d, stat.S_IRWXU)
        self._dir = d

        sock_path = d / "gate.sock"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        os.chmod(sock_path, stat.S_IRUSR | stat.S_IWUSR)
        s.listen(16)
        s.settimeout(_ACCEPT_TIMEOUT_S)
        self._sock = s

        self._hook_path = _install_hook(d)

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    # ---- 给调用方的接线信息 ----

    @property
    def socket_path(self) -> str:
        if self._dir is None:
            raise RuntimeError("broker 未启动")
        return str(self._dir / "gate.sock")

    @property
    def mount_dir(self) -> Path:
        """要 bind 进沙箱的目录（hook 脚本 + socket 都在里面）。"""
        if self._dir is None:
            raise RuntimeError("broker 未启动")
        return self._dir

    def settings_json(self) -> str:
        """`--settings` 的值。传 JSON 字符串而不是文件路径。

        CLI 两种都接受，但字符串少一个「文件在沙箱里可不可见」的问题。
        matcher 用 `*` 匹配所有工具：过滤放在 gate 里（_WRITE_TOOLS /
        _COMMAND_TOOLS），不在这里写第二份工具名单——两份名单必然漂移，
        而漂移的方向是放行。
        """
        if self._hook_path is None:
            raise RuntimeError("broker 未启动")
        return json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{self._python} {self._hook_path}",
                                }
                            ],
                        }
                    ]
                }
            }
        )

    # ---- 服务端 ----

    def _serve(self) -> None:
        while not self._stop.is_set():
            if self._sock is None:
                return
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(30.0)
                buf = b""
                while not buf.endswith(b"\n") and len(buf) < 1 << 20:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode("utf-8").strip() or "{}")
                tool = str(req.get("tool", ""))
                args = req.get("args")
                ruling = self._check(tool, args if isinstance(args, dict) else {})
                reply = json.dumps(
                    {
                        "decision": ruling.decision.value,
                        "rule": ruling.rule,
                        "reason": ruling.reason,
                    }
                )
                conn.sendall(reply.encode("utf-8") + b"\n")
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                # 不让一条坏请求带走整个 broker：后面的调用还要审。
                return

    def _check(self, tool: str, args: dict):
        # 串行化：GateStats.record 改四个计数器 + append 事件列表，
        # 并发下不加锁会丢记录，而丢的正是审计要看的非 ALLOW 事件。
        with self._lock:
            return self._gate.check_tool_call(tool, args)

    # ---- 落库 ----

    def drain_events(self) -> tuple[dict, ...]:
        """取出累计的非 ALLOW 事件，转成 store.record_permission_event 的 kwargs。

        取完就清空：dispatcher 每轮 attempt 收尾时调一次，下一轮重新累计。
        不清的话第 2 轮会把第 1 轮的事件重复落库，审计里同一个拦截出现多次。
        """
        with self._lock:
            events = list(self._gate.stats.events)
            self._gate.stats.events.clear()
        return tuple(
            {
                "tool": e.tool,
                "target": e.target,
                "decision": e.decision.value,
                "rule": e.rule,
                "reason": e.reason,
                "tokens": e.tokens,
                "cost_usd": e.cost_usd,
            }
            for e in events
        )

    @property
    def denied(self) -> int:
        return self._gate.stats.denied

    @property
    def escalated(self) -> int:
        return self._gate.stats.escalated

    @property
    def gate_cost_usd(self) -> float:
        return self._gate.stats.cost_usd


#: 沙箱里必然存在的系统解释器。**不能用 sys.executable**：那指向
#: `<factory_root>/.venv/bin/python`，而沙箱第 2 段 `--tmpfs <factory_root>`
#: 把整个仓库连 .venv 一起盖掉了，于是 hook 启动失败 → 静默放行。
#: 失效表现极其隐蔽：settings 里有 hook、socket 也绑进沙箱了，看起来门装好了，
#: 实际一个动作都拦不住。这就是 hook.py 必须纯标准库的原因 —— 换了解释器
#: 也不需要任何第三方包。
_SYSTEM_PYTHONS = ("/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3")


def _default_python() -> str:
    for cand in _SYSTEM_PYTHONS:
        if os.path.exists(cand):
            return cand
    # 兜底：没有系统解释器时退回当前解释器。非沙箱模式下能用，
    # 沙箱模式下会失败 —— 但那种环境本来也跑不了 bwrap。
    return sys.executable or "python3"


def _install_hook(dest_dir: Path) -> Path:
    """把 hook.py 复制进 dest_dir。

    复制而不是直接指向仓库里的 factory/permission/hook.py：沙箱把工厂代码
    整个 tmpfs 掉了，指过去的话 worker 那边看到的是空目录，hook 启动失败
    → 静默放行。复制到 bind 进沙箱的目录里，两种模式（沙箱/非沙箱）走同一条码路。
    """
    src = Path(__file__).with_name("hook.py")
    dst = dest_dir / _HOOK_NAME
    shutil.copyfile(src, dst)
    os.chmod(dst, stat.S_IRUSR | stat.S_IXUSR)
    return dst


__all__ = ["GateBroker", "Decision"]
