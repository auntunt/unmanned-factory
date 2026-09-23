"""组织树、项目归属与负责人的只读管理范围。

核心约束：管理查看 ≠ 执行权；跨部门数据（详情、聚合数、审计）不可见；
授权与撤销对下一次请求立即生效；旧 admin/member 与 team_projects 不受影响。
"""
from tests.test_admin_config_conversation import env, _login  # noqa: F401

PW = 'member-long-password'


def _user(client, name):
    return client.app.state.auth.create_user(name, PW, role='member')['id']


def _project(store, name):
    return store.add_project({'name': name, 'repository': f'local/{name}', 'workspace': f'/tmp/{name}',
                              'checks': []})['id']


def _run(store, pid, actor, actor_id, status, title):
    run, _ = store.create_run(pid, title, source={'type': 'web', 'actor': actor, 'actor_id': actor_id})
    return store.update(run['id'], {'status': status, 'plan': {'title': title}})


def _unit(client, admin, name, kind, parent=None):
    r = client.post('/api/v5/org/units', headers=admin, json={'name': name, 'kind': kind, 'parent_id': parent})
    assert r.status_code == 201, r.text
    return r.json()['id']


def _world(client, store):
    """公司 → 研发部(→前端组) / 销售部；各一个项目；一个未归属旧项目。"""
    admin = _login(client)
    leader_id, member_id = _user(client, 'leader-a'), _user(client, 'member-b')
    company = _unit(client, admin, '示例公司', 'company')
    rnd = _unit(client, admin, '研发部', 'department', company)
    fe = _unit(client, admin, '前端组', 'group', rnd)
    sales = _unit(client, admin, '销售部', 'department', company)
    p_rnd, p_fe, p_sales, p_legacy = (_project(store, n) for n in ('rnd-api', 'fe-web', 'sales-crm', 'legacy'))
    for pid, uid in ((p_rnd, rnd), (p_fe, fe), (p_sales, sales)):
        assert client.put(f'/api/v5/org/projects/{pid}', headers=admin, json={'unit_id': uid}).status_code == 200
    # 成员 member-b 仍按旧 team_projects 被分配到销售项目
    assert client.put(f'/api/v3/team/members/{member_id}/projects', headers=admin,
                      json={'project_ids': [p_sales]}).status_code == 200
    runs = {
        'fe_pending': _run(store, p_fe, 'member-b', member_id, 'awaiting_approval', '前端待批准'),
        'rnd_ready': _run(store, p_rnd, 'admin1', 1, 'ready_for_review', '研发成果'),
        'sales_secret': _run(store, p_sales, 'member-b', member_id, 'running', '销售机密任务'),
        'legacy': _run(store, p_legacy, 'admin1', 1, 'published', '旧项目发布'),
    }
    return dict(admin=admin, leader_id=leader_id, member_id=member_id, company=company, rnd=rnd, fe=fe,
                sales=sales, p_rnd=p_rnd, p_fe=p_fe, p_sales=p_sales, p_legacy=p_legacy, runs=runs)


def _grant(client, w, unit, admin=None):
    r = client.post('/api/v5/org/scopes', headers=admin or w['admin'], json={'user_id': w['leader_id'], 'unit_id': unit})
    assert r.status_code == 201, r.text


def test_leader_sees_only_own_subtree_and_nothing_across(env):
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    leader = _login(client, 'leader-a', PW)

    me = client.get('/api/v5/me/workspaces').json()
    assert me['management'] is True and [s['unit_id'] for s in me['scopes']] == [w['rnd']]

    view = client.get('/api/v5/management/overview').json()
    assert {p['id'] for p in view['projects']} == {w['p_rnd'], w['p_fe']}
    assert view['counts'] == {'in_progress': 0, 'pending': 1, 'ready': 1, 'published': 0, 'failed': 0}
    assert 'unassigned_projects' not in view
    text = str(view)
    assert '销售' not in text and w['p_sales'] not in text and w['p_legacy'] not in text

    # 手改组织 ID / 项目 ID：与不存在的 ID 同样回答，不泄露存在性
    for path in (f"/api/v5/management/overview?unit_id={w['sales']}",
                 f"/api/v5/management/overview?unit_id={w['company']}",
                 f"/api/v5/management/projects/{w['p_sales']}",
                 f"/api/v5/management/projects/{w['p_legacy']}",
                 '/api/v5/management/overview?unit_id=' + '0' * 32):
        r = client.get(path)
        assert r.status_code == 404, (path, r.text)
        assert r.json()['detail'] == '范围不存在或你无权查看'

    sub = client.get(f"/api/v5/management/overview?unit_id={w['fe']}").json()
    assert [p['id'] for p in sub['projects']] == [w['p_fe']]
    detail = client.get(f"/api/v5/management/projects/{w['p_fe']}").json()
    assert [r['title'] for r in detail['runs']] == ['前端待批准']
    assert 'request' not in detail['runs'][0] and 'conversation' not in str(detail).lower()

    # 管理员组织接口与全部写接口对负责人关闭（不能自授、不能改树）
    assert client.get('/api/v5/org').status_code == 403
    for method, path, body in (
            ('post', '/api/v5/org/scopes', {'user_id': w['leader_id'], 'unit_id': w['company']}),
            ('post', '/api/v5/org/units', {'name': 'x', 'kind': 'group', 'parent_id': w['rnd']}),
            ('put', f"/api/v5/org/projects/{w['p_sales']}", {'unit_id': w['rnd']}),
            ('delete', f"/api/v5/org/scopes/{w['leader_id']}/{w['rnd']}", None)):
        r = client.request(method.upper(), path, headers=leader, json=body)
        assert r.status_code == 403, (path, r.text)
    assert client.put(f"/api/v3/team/members/{w['leader_id']}", headers=leader,
                      json={'role': 'admin', 'active': True}).status_code == 403


def test_management_view_is_not_an_execution_grant(env):
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    leader = _login(client, 'leader-a', PW)
    pending = client.get('/api/v5/management/overview').json()['pending']
    assert len(pending) == 1 and pending[0]['can_act'] is False
    assert '管理查看不包含执行或批准权限' in pending[0]['action_hint']
    rid = w['runs']['fe_pending']['id']
    r = client.post(f'/api/v2/runs/{rid}/approve', headers=leader, json={'revision': 0})
    assert r.status_code == 403
    r = client.post('/api/v2/runs', headers=leader, json={'project_id': w['p_fe'], 'request': 'x'})
    assert r.status_code == 403
    assert store.get(rid)['status'] == 'awaiting_approval'


def test_revoke_and_move_take_effect_on_next_request(env):
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    _login(client, 'leader-a', PW)
    assert client.get(f"/api/v5/management/projects/{w['p_fe']}").status_code == 200

    admin = _login(client)
    # 把前端组挪到销售部下：研发负责人立即看不到它
    assert client.patch(f"/api/v5/org/units/{w['fe']}", headers=admin, json={'parent_id': w['sales']}).status_code == 200
    _login(client, 'leader-a', PW)
    assert client.get(f"/api/v5/management/projects/{w['p_fe']}").status_code == 404
    admin = _login(client)
    assert client.delete(f"/api/v5/org/scopes/{w['leader_id']}/{w['rnd']}", headers=admin).status_code == 200
    _login(client, 'leader-a', PW)
    assert client.get('/api/v5/management/overview').status_code == 403
    assert client.get(f"/api/v5/management/projects/{w['p_rnd']}").status_code == 403
    assert client.get('/api/v5/me/workspaces').json()['management'] is False


def test_org_rejects_cycles_bad_parents_and_unknown_targets(env):
    client, store, _ = env
    w = _world(client, store)
    admin = w['admin']
    assert client.patch(f"/api/v5/org/units/{w['rnd']}", headers=admin, json={'parent_id': w['fe']}).status_code == 422
    assert client.patch(f"/api/v5/org/units/{w['rnd']}", headers=admin, json={'parent_id': w['rnd']}).status_code == 422
    assert client.patch(f"/api/v5/org/units/{w['company']}", headers=admin, json={'parent_id': w['rnd']}).status_code == 422
    assert client.post('/api/v5/org/units', headers=admin, json={'name': '第二个公司', 'kind': 'company'}).status_code == 409
    assert client.post('/api/v5/org/units', headers=admin,
                       json={'name': 'x', 'kind': 'group', 'parent_id': '0' * 32}).status_code == 404
    assert client.put('/api/v5/org/projects/nope', headers=admin, json={'unit_id': w['rnd']}).status_code == 404
    assert client.post('/api/v5/org/scopes', headers=admin, json={'user_id': 999, 'unit_id': w['rnd']}).status_code == 404
    assert client.post('/api/v5/org/scopes', headers=admin, json={'user_id': 1, 'unit_id': w['rnd']}).status_code == 422
    assert client.delete(f"/api/v5/org/units/{w['rnd']}", headers=admin).status_code == 409


def test_member_and_admin_keep_existing_behaviour(env):
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    # 普通成员：无管理范围，旧项目分配仍然有效
    _login(client, 'member-b', PW)
    assert client.get('/api/v5/me/workspaces').json()['management'] is False
    assert client.get('/api/v5/management/overview').status_code == 403
    team = client.get('/api/v3/team').json()
    assert [m['project_ids'] for m in team['members']] == [[w['p_sales']]]
    # 负责人不因查看范围获得 team_projects 执行分配
    _login(client, 'leader-a', PW)
    assert [m['project_ids'] for m in client.get('/api/v3/team').json()['members']] == [[]]
    # 管理员：看见全部与未归属旧项目
    _login(client)
    view = client.get('/api/v5/management/overview').json()
    assert {p['id'] for p in view['unassigned_projects']} == {w['p_legacy']}
    assert view['counts']['published'] == 1 and view['counts']['in_progress'] == 1


def test_audit_is_recorded_scoped_and_redacted(env):
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    admin = _login(client)
    assert client.delete(f"/api/v5/org/scopes/{w['leader_id']}/{w['rnd']}", headers=admin).status_code == 200
    _grant(client, w, w['rnd'], admin)
    full = client.get('/api/v5/management/overview').json()['audit']
    actions = [a['action'] for a in full]
    assert actions[:3] == ['org.scope.granted', 'org.scope.revoked', 'org.scope.granted']
    revoked = full[1]
    assert revoked['actor'] == 'admin1' and revoked['data']['target_username'] == 'leader-a'
    assert revoked['data']['unit_path'] == '示例公司 / 研发部' and revoked['data']['result'] == 'ok' and revoked['at']
    with client.app.state.auth._connection() as db:
        try:
            db.execute('DELETE FROM team_audit')
            raise AssertionError('audit must be append-only')
        except Exception as exc:
            assert 'append-only' in str(exc)
    _login(client, 'leader-a', PW)
    leader_audit = client.get('/api/v5/management/overview').json()['audit']
    assert leader_audit and all('销售' not in str(a) for a in leader_audit)
    assert all(a['data'].get('unit_id') in (w['rnd'], w['fe']) or a['data'].get('project_id') in (w['p_rnd'], w['p_fe'])
               for a in leader_audit)


def test_usage_unknown_is_not_zero(env):
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    _login(client, 'leader-a', PW)
    usage = client.get('/api/v5/management/overview').json()['usage']
    assert usage['recorded'] is False and usage['known_cost_usd'] is None
    rid = w['runs']['rnd_ready']['id']
    store.append(rid, 'usage.recorded', {'call_id': 'c1', 'cost_usd': 0.5})
    store.append(rid, 'usage.recorded', {'call_id': 'c2', 'cost_usd': None})
    store.append(w['runs']['sales_secret']['id'], 'usage.recorded', {'call_id': 'c3', 'cost_usd': 99})
    usage = client.get('/api/v5/management/overview').json()['usage']
    assert usage['known_cost_usd'] == 0.5 and usage['unknown_cost_calls'] == 1 and usage['calls'] == 2


def test_old_endpoints_do_not_leak_other_departments(env):
    """负责人 A 通过旧 /v2 /v3 /v4 入口读 B 部门：列表过滤、直接 ID 拒绝；已分配成员照常。"""
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    sales_run = w['runs']['sales_secret']['id']
    rnd_run = w['runs']['rnd_ready']['id']
    _login(client, 'leader-a', PW)
    direct = [f"/api/v2/projects/{w['p_sales']}/readiness", f"/api/v2/projects/{w['p_sales']}/agent",
              f"/api/v3/projects/{w['p_sales']}/policy", f"/api/v4/projects/{w['p_sales']}/assistant",
              f'/api/v2/runs/{sales_run}', f'/api/v2/runs/{sales_run}/events',
              f'/api/v2/runs/{sales_run}/conversation', f'/api/v2/runs/{sales_run}/export',
              f'/api/v3/runs/{sales_run}/plans', f'/api/v3/runs/{sales_run}/deliverables/download',
              f"/api/v2/runs?project_id={w['p_sales']}", f"/api/v3/overview?project_id={w['p_sales']}",
              # 管理范围内的项目：管理摘要可见，但原始运行/对话仍不因此可读
              f'/api/v2/runs/{rnd_run}/conversation', f'/api/v2/runs/{rnd_run}/events']
    for path in direct:
        r = client.get(path)
        assert r.status_code == 403, (path, r.status_code, r.text[:200])
        assert '机密' not in r.text
    for path in ('/api/v2/projects', '/api/v2/runs', '/api/v3/overview', '/api/v3/team'):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:200])
        assert '销售' not in r.text and w['p_sales'] not in r.text and sales_run not in r.text, path

    _login(client, 'member-b', PW)
    assert [p['id'] for p in client.get('/api/v2/projects').json()['projects']] == [w['p_sales']]
    # 自己发起的运行（前端组那条）按既有「发起人」规则仍可读
    assert {r['id'] for r in client.get('/api/v2/runs').json()['runs']} == {sales_run, w['runs']['fe_pending']['id']}
    for path in (f'/api/v2/runs/{sales_run}', f'/api/v2/runs/{sales_run}/events',
                 f'/api/v2/runs/{sales_run}/conversation', f"/api/v3/overview?project_id={w['p_sales']}"):
        assert client.get(path).status_code == 200, path
    assert client.get(f'/api/v2/runs/{rnd_run}').status_code == 403


def test_management_summary_never_echoes_request_text(env):
    client, store, _ = env
    w = _world(client, store)
    run, _ = store.create_run(w['p_rnd'], '请处理：客户身份证号 110101XXXX 与合同全文……',
                              source={'type': 'web', 'actor': 'admin1', 'actor_id': 1})
    store.update(run['id'], {'status': 'needs_clarification'})
    _login(client)
    body = client.get('/api/v5/management/overview').text + client.get(f"/api/v5/management/projects/{w['p_rnd']}").text
    assert '身份证' not in body and f"任务 {run['id'][:8]}（尚无方案标题）" in body


def _admin_audit(client):
    _login(client)
    return client.get('/api/v5/management/overview').json()['audit']


def test_project_moved_across_departments_hides_its_past_in_leader_audit(env):
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    assert client.put(f"/api/v5/org/projects/{w['p_sales']}", headers=w['admin'], json={'unit_id': w['rnd']}).status_code == 200
    _login(client, 'leader-a', PW)
    audit = client.get('/api/v5/management/overview').json()['audit']
    assert not [r for r in audit if w['sales'] in str(r) or '销售部' in str(r)]
    moved = [r for r in audit if r['action'] == 'org.project.bound' and r['data']['project_id'] == w['p_sales']]
    assert len(moved) == 1  # 只有移入研发部这一条；绑定到销售部的旧记录不出现
    assert moved[0]['data']['unit_path'] == '示例公司 / 研发部' and moved[0]['data']['previous_unit_path'] == '范围外组织'
    assert set(moved[0]['data']) <= {'result', 'unit_id', 'unit_path', 'project_id', 'project_name', 'previous_unit_path'}
    # 管理员的原始追加审计完整保留：两次绑定、旧组织 ID 与路径都在
    raw = [r for r in _admin_audit(client) if r['action'] == 'org.project.bound' and r['data']['project_id'] == w['p_sales']]
    assert [r['data']['unit_id'] for r in raw] == [w['rnd'], w['sales']]
    assert raw[0]['data']['previous_unit_id'] == w['sales'] and raw[0]['data']['previous_unit_path'] == '示例公司 / 销售部'


def test_unit_moved_across_departments_hides_its_past_in_leader_audit(env):
    client, store, _ = env
    w = _world(client, store)
    key = _unit(client, w['admin'], '大客户组', 'group', w['sales'])
    _grant(client, w, w['rnd'])
    assert client.patch(f'/api/v5/org/units/{key}', headers=w['admin'], json={'parent_id': w['rnd']}).status_code == 200
    _login(client, 'leader-a', PW)
    audit = client.get('/api/v5/management/overview').json()['audit']
    assert not [r for r in audit if w['sales'] in str(r) or '销售部' in str(r)]
    rows = {r['action']: r['data'] for r in audit if r['data'].get('unit_id') == key}
    # 节点现在在范围内：它的建立与移动可见，但路径按今天的树计算，来处不暴露
    assert rows['org.unit.created']['unit_path'] == '示例公司 / 研发部 / 大客户组'
    assert rows['org.unit.updated']['parent_path_before'] == '范围外组织'
    assert 'unit_path_before' not in rows['org.unit.updated'] and 'parent_id' not in rows['org.unit.created']
    raw = {r['action']: r['data'] for r in _admin_audit(client) if r['data'].get('unit_id') == key}
    assert raw['org.unit.created']['unit_path'] == '示例公司 / 销售部 / 大客户组'
    assert raw['org.unit.updated']['unit_path_before'] == '示例公司 / 销售部 / 大客户组'
    assert raw['org.unit.updated']['parent_id_before'] == w['sales']
