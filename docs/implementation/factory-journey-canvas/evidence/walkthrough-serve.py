"""演练服务：真实 create_app + uvicorn + 前端构建；执行器换成测试替身 FakeSDK（不调模型）。所有数据为合成演练数据。"""
import os, sys, pathlib
sys.path.insert(0, '/Users/auntlee/workspace/.factory-worktrees/enterprise-governance-v1')
from factory.control.app import create_app
from factory.control.service import Service
from factory.control.store import Store
from tests.test_control_app import FakeSDK
import uvicorn
S = pathlib.Path(os.environ['S'])
data = S / 'data'
store = Store(data / 'control.db')
profiles = {k: {'provider': 'codex', 'model': 'fake-sdk'} for k in ('planner', 'cheap', 'standard', 'strong')}
svc = Service(store, runner=FakeSDK(), profiles=profiles)
app = create_app(data_dir=data, workspace_root=S / 'ws', public_origin='http://127.0.0.1:8812', service=svc, webhook_secret='x')
for name, role in (('demo-admin', 'admin'), ('demo-member', 'member')):
    try: app.state.auth.create_user(name, 'demo-only-password-2026', role=role)
    except Exception: pass
uvicorn.run(app, host='127.0.0.1', port=8812, log_level='warning')
