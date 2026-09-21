"""Loopback rehearsal for the 信创化改造 and 接口适配 pages.

Run: python3 scripts/preview_scenarios.py [--port 8791]
Login: preview / factory-preview-only

What is real here: the FastAPI service, the HTTP contract the pages call, the
control database, the plugin availability gate, the project's own checks, the
git worktrees and the exported patches.

What is **not** real: the coding model. No provider SDK is installed on this
machine (``claude_agent_sdk`` / ``openai_codex`` / ``deepseek_harness`` are all
absent, and the SDK worker deliberately strips the host agent's credentials), so
the executor here is a labelled script. It edits real files in the real
workspace, and the project's real checks decide pass or fail -- but nothing in
this rehearsal is evidence that a model can do the work.

Deliberately shaped to fail once and then repair, so the failing path is
exercised rather than assumed: the first execution writes content the project's
own check rejects, and only the repair round writes content it accepts.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factory.control.app import create_app
from factory.control.auth import AuthError
from factory.control.plugins import PluginAvailability
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Store

# --- 信创 fixture: a quote module pinned to one SQL dialect ------------------
LEGACY_DB = '''DIALECTS = {"mysql": "jdbc:mysql://{host}:{port}/{db}"}


def build_url(dialect, host, port, db):
    template = DIALECTS[dialect]
    return template.format(host=host, port=port, db=db)
'''
LEGACY_TEST = '''from db_url import build_url


def test_mysql_unchanged():
    assert build_url("mysql", "h", 3306, "d") == "jdbc:mysql://h:3306/d"


def test_dm_supported():
    assert build_url("dm", "h", 5236, "d") == "jdbc:dm://h:5236/d"
'''
LEGACY_BROKEN_FIX = '''DIALECTS = {
    "mysql": "jdbc:mysql://{host}:{port}/{db}",
    "dm": "jdbc:dm//{host}:{port}/{db}",
}


def build_url(dialect, host, port, db):
    template = DIALECTS[dialect]
    return template.format(host=host, port=port, db=db)
'''
LEGACY_GOOD_FIX = '''DIALECTS = {
    "mysql": "jdbc:mysql://{host}:{port}/{db}",
    "dm": "jdbc:dm://{host}:{port}/{db}",
}


def build_url(dialect, host, port, db):
    template = DIALECTS[dialect]
    return template.format(host=host, port=port, db=db)
'''

# --- 适配 fixture: an adapter that forgets the auth header -------------------
ADAPTER = '''import json
import urllib.request

TOKEN = "sandbox-secret-token"


def build_request(url, payload, headers=None):
    body = json.dumps(payload).encode()
    return urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json",
                                           **(headers or {})})
'''
ADAPTER_FIXED = '''import json
import urllib.request

TOKEN = "sandbox-secret-token"


def build_request(url, payload, headers=None):
    body = json.dumps(payload).encode()
    return urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json",
                                           "Authorization": "Bearer " + TOKEN,
                                           **(headers or {})})
'''
ADAPTER_CHECK = '''"""Start a local stand-in for the third party and send one business message.

The endpoint is a local process started by this check. Everything it reports is
therefore labelled mock: a 200 from it is not a third party accepting anything.
"""
import json
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from adapter import build_request


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        if self.headers.get("Authorization") != "Bearer sandbox-secret-token":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"code":"AUTH_REQUIRED"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"code": "ACCEPTED",
                                     "request_id": "mock-1"}).encode())

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/report" % server.server_address[1]
correlation = uuid.uuid4().hex
try:
    with urllib.request.urlopen(build_request(url, {"amount": "12.34"}), timeout=10) as reply:
        body = json.loads(reply.read().decode())
    accepted = body.get("code") == "ACCEPTED"
    print("ADAPTATION_RESULT: " + json.dumps({
        "correlation_id": correlation, "technical_success": True,
        "business_accepted": accepted,
        # One-way reporting: no channel confirms final completion, so it is
        # never claimed.
        "business_completed": False, "mock": True, "endpoint": "/report",
        "sanitized_response": {"code": body.get("code"),
                               "request_id": body.get("request_id")}}))
    raise SystemExit(0 if accepted else 1)
except urllib.error.HTTPError as exc:
    print("ADAPTATION_RESULT: " + json.dumps({
        "correlation_id": correlation, "technical_success": False,
        "business_accepted": False, "business_completed": False, "mock": True,
        "endpoint": "/report", "sanitized_response": {"code": "AUTH_REQUIRED"}}))
    raise SystemExit(1)
'''


class ScenarioRehearsalRunner:
    """A labelled script, not a model. It edits real files; checks judge them."""

    def available(self):
        return [{"id": "codex", "installed": False,
                 "detail": "本地演练使用脚本，没有调用任何模型"}]

    def run(self, request, emit, cancel=None):
        if cancel and cancel.is_set():
            raise RuntimeError("演练已取消")
        workspace = Path(request.workspace)
        kind = 'adaptation' if (workspace / 'adapter.py').exists() else 'modernization'
        if request.read_only:
            return self._plan(kind, emit)
        return self._execute(kind, workspace, emit)

    def _plan(self, kind, emit):
        if kind == 'modernization':
            plan = {
                "title": "数据库维度：新增达梦方言",
                "summary": "在不改变 mysql 现有输出的前提下，为 db_url 增加达梦方言。",
                "questions": [],
                "tasks": [{
                    "id": "dm", "title": "增加 dm 方言并跑回归",
                    "prompt": "为 build_url 增加 dm 方言，保持 mysql 行为不变。",
                    "acceptance": ["mysql 输出不变", "dm 方言可用", "项目回归检查通过"],
                    "paths": ["db_url.py"], "checks": ["regression"],
                    "depends_on": [], "complexity": "small", "risk": "low"}],
            }
        else:
            plan = {
                "title": "上报报文：补上鉴权头",
                "summary": "适配器缺少 Authorization 头，业务报文被对端拒绝。",
                "questions": [],
                "tasks": [{
                    "id": "auth", "title": "补鉴权头并联调一次",
                    "prompt": "在 build_request 里带上 Bearer 鉴权头。",
                    "acceptance": ["业务报文被受理", "凭据不硬编码在日志里"],
                    "paths": ["adapter.py"], "checks": ["contract"],
                    "depends_on": [], "complexity": "small", "risk": "low"}],
            }
        emit("assistant.message", {"text": "演练脚本生成了结构化计划（没有调用模型）。"})
        return ProviderResult(json.dumps(plan, ensure_ascii=False), cost_usd=0.0,
                              tokens_in=0, tokens_out=0)

    def _execute(self, kind, workspace, emit):
        # Judged on what is in the workspace right now, not on a round counter:
        # the first pass writes something the project's own check rejects, and
        # the repair pass fixes it. The failing path is exercised, not assumed.
        if kind == 'modernization':
            target = workspace / 'db_url.py'
            current = target.read_text(encoding='utf-8')
            repairing = 'jdbc:dm//' in current
            target.write_text(LEGACY_GOOD_FIX if repairing else LEGACY_BROKEN_FIX,
                              encoding='utf-8')
            note = '修好了少一个斜杠的 dm 连接串' if repairing else '先写入一个有缺陷的 dm 方言，交给项目回归检查判定'
        else:
            target = workspace / 'adapter.py'
            current = target.read_text(encoding='utf-8')
            repairing = 'Authorization' not in current
            target.write_text(ADAPTER_FIXED if repairing else ADAPTER,
                              encoding='utf-8')
            note = '补上了 Authorization 头' if repairing else '先保持缺鉴权头的现状，让契约检查真的失败一次'
        emit("tool.completed", {"tool": "rehearsal.write", "path": target.name,
                                "message": note})
        return ProviderResult(note, cost_usd=0.0, tokens_in=0, tokens_out=0)


def _repo(root: Path, files: dict[str, str]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / '.git').exists():
        for arguments in (["init", "-q", "-b", "main"],
                          ["config", "user.name", "Factory rehearsal"],
                          ["config", "user.email", "preview@localhost.invalid"]):
            subprocess.run(["git", *arguments], cwd=root, check=True,
                           capture_output=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding='utf-8')
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    if subprocess.run(["git", "status", "--porcelain"], cwd=root,
                      capture_output=True, text=True).stdout.strip():
        subprocess.run(["git", "commit", "-qm", "rehearsal baseline"], cwd=root,
                       check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def rehearsal_app(port=8791, data_dir=None):
    data = Path(data_dir) if data_dir else ROOT / '.factory-preview-scenarios'
    workspaces = data / 'workspaces'
    legacy_sha = _repo(workspaces / 'legacy-quote',
                       {'db_url.py': LEGACY_DB, 'test_db_url.py': LEGACY_TEST})
    adapter_sha = _repo(workspaces / 'gp-adapter',
                        {'adapter.py': ADAPTER, 'check_adapt.py': ADAPTER_CHECK})
    store = Store(data / 'control.db')
    profiles = {role: {"provider": "codex", "model": f"rehearsal-{role}"}
                for role in ("planner", "cheap", "standard", "strong")}
    service = Service(store, runner=ScenarioRehearsalRunner(), profiles=profiles,
                      timeout_s=180)
    service.preview_mode = True
    app = create_app(data_dir=data, workspace_root=workspaces,
                     public_origin=f"http://127.0.0.1:{port}", service=service)
    try:
        app.state.auth.create_user("preview", "factory-preview-only")
    except AuthError as exc:
        if exc.status != 409:
            raise

    existing = {p['repository'] for p in store.projects()}
    if 'preview/legacy-quote' not in existing:
        store.add_project({
            "name": "老报价系统 · 信创演练", "repository": "preview/legacy-quote",
            "workspace": str(workspaces / 'legacy-quote'), "base_branch": "main",
            "auto_issues": False, "auto_publish": False, "budget_usd": 2.0,
            "checks": {"regression": [sys.executable, "-m", "pytest", "-q",
                                      "-p", "no:randomly", "test_db_url.py"]}})
    if 'preview/gp-adapter' not in existing:
        store.add_project({
            "name": "三方上报适配 · 演练", "repository": "preview/gp-adapter",
            "workspace": str(workspaces / 'gp-adapter'), "base_branch": "main",
            "auto_issues": False, "auto_publish": False, "budget_usd": 2.0,
            "checks": {"contract": [sys.executable, "check_adapt.py"]}})

    # Both business plugins ship disabled; an administrator turns them on. The
    # rehearsal does it here so the page shows the enabled state, and the
    # enable/disable path itself is still driven over HTTP by the driver.
    availability = PluginAvailability(store)
    for plugin_id in ('legacy-modernization', 'api-adaptation'):
        if availability.view(plugin_id)['state'] != 'enabled':
            availability.set_state(plugin_id, 'enabled', actor='rehearsal')

    print(f"信创演练基线：preview/legacy-quote @ main = {legacy_sha}", flush=True)
    print(f"适配演练基线：preview/gp-adapter  @ main = {adapter_sha}", flush=True)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8791)
    parser.add_argument('--data-dir', default=None)
    args = parser.parse_args()
    import uvicorn
    print(f"场景演练：http://127.0.0.1:{args.port} · 登录 preview / factory-preview-only",
          flush=True)
    uvicorn.run(rehearsal_app(args.port, args.data_dir), host='127.0.0.1',
                port=args.port, proxy_headers=False)
