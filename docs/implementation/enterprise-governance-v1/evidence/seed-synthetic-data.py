# 合成验收数据：直接写隔离库（不经模型、不派发）。每条运行都标 source.synthetic=True。
import os, pathlib, json
from factory.control.store import Store
from factory.control.auth import AuthStore
d = pathlib.Path(os.environ['FACTORY_CONTROL_DATA']); ws = pathlib.Path(os.environ['FACTORY_WORKSPACE_ROOT'])
st = Store(d / 'control.db'); ids = {u['username']: u['id'] for u in AuthStore(d / 'users.db').users()}
P = {}
for name in ('rnd-api', 'fe-web', 'sales-crm', 'legacy-tool'):
    existing = [p for p in st.projects() if p['name'] == name]
    P[name] = existing[0]['id'] if existing else st.add_project({'name': name, 'repository': f'local/{name}', 'workspace': str(ws / name), 'checks': {}, 'actor': 'seed'})['id']
def run(pid, actor, status, title, cost=None, unknown=False):
    r, _ = st.create_run(pid, title, source={'type': 'web', 'actor': actor, 'actor_id': ids[actor], 'synthetic': True})
    st.update(r['id'], {'status': status, 'plan': {'title': title}})
    if cost is not None: st.append(r['id'], 'usage.recorded', {'call_id': r['id'] + '-1', 'cost_usd': cost})
    if unknown: st.append(r['id'], 'usage.recorded', {'call_id': r['id'] + '-2', 'cost_usd': None})
    return r['id']
R = {
 'rnd_ready': run(P['rnd-api'], 'gov-admin', 'ready_for_review', '订单接口增加分页（合成）', cost=1.25),
 'rnd_running': run(P['rnd-api'], 'gov-admin', 'running', '接口限流改造（合成）', unknown=True),
 'fe_pending': run(P['fe-web'], 'gov-admin', 'awaiting_approval', '登录页白屏修复方案待批准（合成）'),
 'sales_secret': run(P['sales-crm'], 'member-sales', 'needs_clarification', '销售部机密：大客户报价调整（合成）', cost=9.99),
 'legacy_pub': run(P['legacy-tool'], 'gov-admin', 'published', '旧工具发布（合成）'),
}
print(json.dumps({'projects': P, 'runs': R}, ensure_ascii=False))
