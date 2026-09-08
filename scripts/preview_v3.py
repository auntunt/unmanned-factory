"""Loopback-only UI rehearsal with real git/checks and a labelled scripted provider.

Run: .venv/bin/python scripts/preview_v3.py
Login: preview / factory-preview-only (public rehearsal credentials).
No model, cloud service, Feishu message, or GitHub publication is invoked.
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
from factory.control.autonomy import DEFAULT_POLICY, PolicyStore
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Store


class RehearsalRunner:
    def available(self):
        return [{"id": "codex", "installed": False, "detail": "本地演练使用脚本，未调用模型"}]

    def run(self, request, emit, cancel=None):
        if cancel and cancel.is_set():
            raise RuntimeError("演练已取消")
        if request.read_only:
            text = request.prompt.split("Request: ", 1)[-1].split("Prior planning history:", 1)[0]
            needs_question = "模糊需求" in text and "Prior planning history:\n(none)" in request.prompt
            plan = {
                "title": "完善工单服务的欢迎提示", "summary": "在独立工作区调整欢迎提示，并通过项目配置的内容检查。",
                "questions": ["这个提示面向管理员还是报修用户？建议使用面向报修用户的简短引导。"] if needs_question else [],
                "tasks": [] if needs_question else [{
                    "id": "welcome", "title": "更新欢迎提示并验证", "prompt": "将 welcome.txt 更新为：工单服务已就绪。",
                    "acceptance": ["welcome.txt 的内容为工单服务已就绪", "项目内容检查通过"],
                    "paths": ["welcome.txt"], "checks": ["welcome"], "depends_on": [],
                    "complexity": "small", "risk": "high" if "风险演练" in text else "low",
                }],
            }
            emit("assistant.message", {"text": "演练规划脚本已生成结构化需求与验收条件。"})
            return ProviderResult(json.dumps(plan, ensure_ascii=False), cost_usd=0.0, tokens_in=0, tokens_out=0)
        # Deliberately fail the economic role's first check to exercise the real
        # repair/escalation path. These names are explicitly preview profiles.
        content = "演练：等待修复" if request.model.endswith("cheap") else "工单服务已就绪"
        Path(request.workspace, "welcome.txt").write_text(content, encoding="utf-8")
        emit("tool.completed", {"tool": "rehearsal.write", "path": "welcome.txt",
                                "message": "演练脚本修改了独立工作区中的文件"})
        return ProviderResult("演练脚本执行完毕；最终结果由项目检查确认。", cost_usd=0.0, tokens_in=0, tokens_out=0)


def rehearsal_app(port=8790):
    data = ROOT / ".factory-preview"
    repo = data / "workspaces" / "ticket-service"
    if not repo.exists():
        repo.mkdir(parents=True)
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.name", "Factory rehearsal"],
                          ["config", "user.email", "preview@localhost.invalid"]):
            subprocess.run(["git", *arguments], cwd=repo, check=True)
        (repo / "welcome.txt").write_text("欢迎使用", encoding="utf-8")
        (repo / "README.md").write_text("# 工单服务 · 本地演练\n\n这是用于工厂界面验收的隔离仓库。\n", encoding="utf-8")
        subprocess.run(["git", "add", "welcome.txt", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "Initialize isolated rehearsal"], cwd=repo, check=True)
    store = Store(data / "control.db")
    profiles = {role: {"provider": "codex", "model": f"preview-{role}"}
                for role in ("planner", "cheap", "standard", "strong")}
    service = Service(store, runner=RehearsalRunner(), profiles=profiles, timeout_s=60)
    service.preview_mode = True
    app = create_app(data_dir=data, workspace_root=repo.parent,
                     public_origin=f"http://127.0.0.1:{port}", service=service)
    try:
        app.state.auth.create_user("preview", "factory-preview-only")
    except AuthError as exc:
        if exc.status != 409:
            raise
    if not store.projects():
        project = store.add_project({"name": "工单服务 · 本地演练", "repository": "preview/ticket-service",
            "workspace": str(repo), "base_branch": "main", "auto_issues": False,
            "auto_publish": False, "budget_usd": 2.0,
            "checks": {"welcome": [sys.executable, "-c",
                "from pathlib import Path; assert Path('welcome.txt').read_text() == '工单服务已就绪', '欢迎提示未达到验收条件'"]}})
        PolicyStore(store).update(project['id'], {**DEFAULT_POLICY, 'mode': 'autonomous'}, 0, 'rehearsal')
        # The normal startup recovery discovers and durably dispatches these.
        store.create_run(project['id'], "请完善工单服务的欢迎提示，并验证交付结果。", source={'type': 'web', 'actor': 'rehearsal'})
        store.create_run(project['id'], "模糊需求：希望工单欢迎提示更合适。", source={'type': 'web', 'actor': 'rehearsal'})
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8790)
    args = parser.parse_args()
    import uvicorn
    print(f"本地演练：http://127.0.0.1:{args.port} · 登录 preview / factory-preview-only", flush=True)
    uvicorn.run(rehearsal_app(args.port), host='127.0.0.1', port=args.port, proxy_headers=False)
