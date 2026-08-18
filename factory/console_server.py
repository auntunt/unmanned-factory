"""对话式控制台的 HTTP 路由层。

挂在 /console/ 下，和 dashboard 的 / 共存。serve() 把两个 handler
都注册进同一个 HTTPServer，不用起两个端口。

路由：
  GET  /console/        主页面（完整 HTML）
  GET  /console/stream  SSE，每 5 秒推一次最新 threads 片段
  POST /console/prd     提需求（返回 JSON）

为什么不用 Flask / Starlette：dashboard 已经是一个裸 http.server，
引入新框架会让 CLI 的 --demo / --db / --queue 参数绕过去，而这三个参数
控制着「看哪个库、看哪个队列」—— 直接加路由，参数天然共享。

SSE 用 threading.Event 推送，不用 asyncio：http.server 是多线程的
（每请求一个线程），asyncio 混进去反而要锁。SSE 连接每 5 秒发一次，
连接断了线程自然退出，不需要 cleanup hook。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from factory.console_read import read_threads
from factory.console_render import page, threads_fragment


class ConsoleHandler:
    """dashboard 的 BaseHTTPRequestHandler 把 /console/* 代理到这里。

    为什么是「代理」而不是继承：dashboard.py 已经有一个 Handler 类，
    继承关系太深会让两边的错误处理纠缠在一起。这里做一个合作对象：
    Handler.do_GET 先判路径，/console/* 就 delegate 过来。
    """

    def __init__(self, *, db: str, queue: str | None, binary: str,
                 intake_model: str):
        self._db = db
        self._queue = queue
        self._binary = binary
        self._intake_model = intake_model
        # SSE 通知：POST 提需求后通知所有 stream 连接立即刷新，不用等 5 秒
        self._push = threading.Event()

    # ── 公开方法供 Handler 调用 ────────────────────────────────

    def handle_get(self, path: str, handler) -> bool:
        """处理 GET。返回 True 表示已处理，False 表示不归这里管。"""
        if path == "/console" or path == "/console/":
            self._serve_page(handler)
            return True
        if path == "/console/stream":
            self._serve_sse(handler)
            return True
        return False

    def handle_post(self, path: str, body: bytes, handler) -> bool:
        """处理 POST。"""
        if path == "/console/prd":
            self._serve_prd(body, handler)
            return True
        return False

    # ── 内部实现 ──────────────────────────────────────────────

    def _read(self):
        return read_threads(self._queue)

    def _serve_page(self, h) -> None:
        threads, error = self._read()
        body = page(threads, error=error).encode()
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _serve_sse(self, h) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Accel-Buffering", "no")   # 关 nginx/Caddy 缓冲
        h.end_headers()

        def _push(payload: str) -> bool:
            """推一条 SSE。写失败（连接断）就返回 False。"""
            try:
                line = f"data: {payload}\n\n"
                h.wfile.write(line.encode())
                h.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        # 先推一条当前状态
        threads, error = self._read()
        frag = threads_fragment(threads)
        if not _push(json.dumps({"html": frag, "error": error},
                                ensure_ascii=False)):
            return

        while True:
            # 每 5 秒 push 一次；POST 提需求后 Event 触发，立即推
            triggered = self._push.wait(timeout=5.0)
            if triggered:
                self._push.clear()
            threads, error = self._read()
            frag = threads_fragment(threads)
            if not _push(json.dumps({"html": frag, "error": error},
                                    ensure_ascii=False)):
                return

    def _serve_prd(self, body: bytes, h) -> None:
        """收到「提需求」POST。

        不在这里同步跑 `factory prd` —— 那可能要几十秒（调用模型、等网络），
        HTTP 请求等这么久会触发浏览器超时。
        异步 subprocess，立刻返回「已提交」，SSE 通知前端刷新。
        """
        fields = parse_qs(body.decode(errors="replace"))
        text = (fields.get("text") or [""])[0].strip()
        if not text:
            self._json(h, 400, {"ok": False, "error": "需求不能为空"})
            return

        if not self._queue:
            self._json(h, 503, {"ok": False,
                                "error": "服务器没有配置队列目录，无法提交需求。"
                                         "重启时加 --queue 参数。"})
            return

        # 后台跑 `factory prd --text "..." --queue ...`
        # 不等它结束：直接返回 202，SSE 会在任务进队后推刷新。
        cmd = [
            sys.executable, "-m", "factory.cli", "prd",
            "--text", text,
            "--queue", self._queue,
            "--binary", self._binary,
            "--intake-model", self._intake_model,
        ]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # 用 start_new_session 脱离父进程的信号组，
                # 父进程重启时 prd 还能跑完
                start_new_session=True,
            )
        except (OSError, FileNotFoundError) as exc:
            self._json(h, 500, {"ok": False,
                                "error": f"无法启动 worker：{exc}。"
                                         f"检查 --binary 参数。"})
            return

        self._push.set()   # 通知所有 SSE 连接立即刷新
        self._json(h, 202, {
            "ok": True,
            "msg": "已收到，正在解析需求，稍等十几秒后在下方看进度…",
        })

    @staticmethod
    def _json(h, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
