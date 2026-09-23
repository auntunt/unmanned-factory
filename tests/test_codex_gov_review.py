from tests.test_org_governance import env, _world, _grant, _login, PW

def test_moved_project_audit_does_not_expose_other_department(env):
    client, store, _ = env
    w = _world(client, store)
    _grant(client, w, w['rnd'])
    assert client.put(f"/api/v5/org/projects/{w['p_sales']}", headers=w['admin'], json={'unit_id': w['rnd']}).status_code == 200
    _login(client, 'leader-a', PW)
    audit = client.get('/api/v5/management/overview').json()['audit']
    # Project itself is now authorized. Historical outside unit identifiers and paths are not.
    leaked = [r for r in audit if w['sales'] in str(r) or '销售部' in str(r)]
    assert not leaked, leaked
