"""合成演练数据：全部经由真实 HTTP 接口写入隔离库，仓库均声明 synthetic。"""
import json, os, sys, time, pathlib
import httpx
S = pathlib.Path(os.environ['S']); B = 'http://127.0.0.1:8812'; O = {'Origin': B}
c = httpx.Client(base_url=B, timeout=30)
def login(u):
    r = c.post('/api/auth/login', json={'username': u, 'password': 'demo-only-password-2026'}, headers=O); r.raise_for_status()
    return {**O, 'X-CSRF-Token': r.json()['csrf_token']}
h = login('demo-admin')
def reg(source, name):
    r = c.post('/api/v2/maintenance/repos', json={'source': source, 'name': name, 'synthetic': True}, headers=h)
    assert r.status_code == 201, r.text; return r.json()['project_id']
orders = reg(str(S / 'ws/orders-api'), 'orders-api（演练）')
report = reg(str(S / 'ws/report-web'), 'report-web（演练）')
broken = reg('https://127.0.0.1:9/demo/broken-repo.git', 'broken-repo（演练）')
p = c.get('/api/v2/projects').json()['projects']; proj = next(x for x in p if x['id'] == orders)
body = {k: proj.get(k) for k in ('name', 'base_branch', 'auto_issues', 'auto_publish')}
body.update(revision=proj['revision'], checks={'greeting': [sys.executable, '-c', "from pathlib import Path; assert Path('greeting.txt').read_text().strip() == 'hello world'"]}, budget_source='inherit')
r = c.put(f'/api/v2/projects/{orders}', json=body, headers=h); assert r.status_code == 200, r.text
c.post(f'/api/v2/maintenance/repos/{orders}/probe', headers=h)
def submit(text, key):
    r = c.post('/api/v2/maintenance/requirements', json={'project_id': orders, 'content': text, 'idempotency_key': key}, headers=h)
    assert r.status_code == 201, r.text; return r.json()
def wait(rid, states):
    for _ in range(300):
        s = c.get(f'/api/v2/runs/{rid}').json()['status']
        if s in states: return s
        time.sleep(0.1)
    raise SystemExit(f'{rid} stuck in {s}')
a = submit('问候语改成 hello world（演练需求 A）', 'demo-req-a-0001'); wait(a['execution_id'], {'awaiting_approval'})
r = c.post(f"/api/v2/maintenance/tasks/{a['task_id']}/approve", headers=h); assert r.status_code == 200, r.text
wait(a['execution_id'], {'ready_for_review'})
b = submit('订单导出增加时区说明（演练需求 B）', 'demo-req-b-0001'); wait(b['execution_id'], {'awaiting_approval'})
member = [u for u in c.get('/api/v3/team').json()['members'] if u['username'] == 'demo-member'][0]
r = c.put(f"/api/v3/team/members/{member['id']}/projects", json={'project_ids': [report]}, headers=h); assert r.status_code == 200
for _ in range(100):
    st = {x['project_id']: x['state'] for x in c.get('/api/v2/maintenance/repos').json()['repos']}
    if st.get(broken) != 'analyzing': break
    time.sleep(0.1)
out = {'orders': orders, 'report': report, 'broken': broken, 'task_delivered': a['task_id'], 'task_pending': b['task_id'], 'repo_states': st}
(S / 'seed.json').write_text(json.dumps(out, ensure_ascii=False, indent=1)); print(json.dumps(out, ensure_ascii=False))
