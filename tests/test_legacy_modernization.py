"""信创化改造 (legacy-modernization): target memory, dependency disposal, and one
real migration slice delivered end to end.

Everything here is a synthetic example and every receipt says so. Three layers:

* Unit level -- ``normalize``/``register_target``/``confirm_dimension``/
  ``dimensions_view``/``disposal_plan`` against a plain ``Store``, no executor.
* Vertical -- one synthetic slice through the real ``execute_plan`` executor
  (the same one production uses, with an explicit fake model standing in for
  the coding provider, exactly like ``test_issue_maintenance_vertical.py``),
  proving a real failing baseline becomes a real patch, a real receipt, and an
  honestly-labelled unverified dimension because there is no real 达梦 instance
  in this sandbox. A follow-up revision then proves the confirmed constraint
  survives and is read back.
* HTTP -- the plugin gate really blocks new-slice creation over
  ``/api/v2/modernization`` while the plugin is disabled (its real default,
  since ``plugins.py`` still declares it non-executable and this task does not
  flip that), while read-only endpoints stay reachable; and, with an injected
  fake gate/dispatch (test-only, never production's default), one slice really
  goes end to end through the mounted router.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import factory.control.legacy_modernization as lm
from factory.control import scenario_memory
from factory.control.app import create_app
from factory.control.execution import execute_plan
from factory.control.issue_maintenance_webuddy import WebuddyIdentity, WebuddyRepository
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Conflict, Store

ACTOR = {'id': 7, 'username': 'modernizer'}


# ---------------------------------------------------------------------------
# unit level: target memory, dimensions, disposal plan
# ---------------------------------------------------------------------------

@pytest.fixture
def store_project(tmp_path):
    store = Store(tmp_path / 'control.db')
    project = store.add_project({
        'name': '合成信创项目', 'repository': 'synthetic/xinchuang',
        'workspace': str(tmp_path / 'ws'), 'base_branch': 'main',
        'budget_usd': 5.0})
    return store, project


def test_register_target_defaults_to_candidate_not_active(store_project):
    store, project = store_project
    entries = lm.register_target(
        store, project['id'],
        {'database': '达梦 DM8', 'jdk_framework': 'Java17/SpringBoot3'},
        actor='evaluator', source='_MIGRATION_ASSESSMENT.md')
    assert len(entries) == 2
    assert all(e['status'] == scenario_memory.UNCONFIRMED_STATUS for e in entries)
    assert all(e['kind'] == 'hypothesis' for e in entries)

    dims = lm.dimensions_view(store, project['id'])
    by_dim = {d['dimension']: d for d in dims}
    assert by_dim['database']['status'] == 'candidate'
    assert by_dim['database']['confirmed'] is False
    assert '达梦' in by_dim['database']['content']
    # Untouched dimensions are reported as missing, not fabricated.
    assert by_dim['browser']['status'] == 'missing'
    assert by_dim['browser']['content'] is None

    # A report's suggestion never travels into an executor prompt as a
    # requirement -- it is quoted only as a title under "不作为依据".
    block = scenario_memory.constraints_block(
        scenario_memory.recall(store, project['id']))
    assert '不作为依据' in block
    assert '达梦' not in block.split('不作为依据')[0]


def test_confirm_dimension_promotes_without_losing_the_others(store_project):
    store, project = store_project
    lm.register_target(store, project['id'],
                       {'database': '达梦 DM8', 'jdk_framework': 'Java17/SpringBoot3'},
                       actor='evaluator', source='report')
    confirmed = lm.confirm_dimension(store, project['id'], 'database', actor='customer_pm')
    assert confirmed['status'] == scenario_memory.CONFIRMED_STATUS
    assert confirmed['kind'] == 'decision'

    dims = {d['dimension']: d for d in lm.dimensions_view(store, project['id'])}
    assert dims['database']['confirmed'] is True
    # The other dimension is untouched: confirming one does not silently
    # promote a sibling.
    assert dims['jdk_framework']['status'] == 'candidate'

    block = scenario_memory.constraints_block(
        scenario_memory.recall(store, project['id']))
    # Heading text moved when the renderer learned to tell a confirmed
    # requirement from a confirmed fact; the property is unchanged --
    # 达梦 is binding, the unconfirmed JDK suggestion is not.
    assert '本次必须遵守' in block
    assert '达梦' in block.split('不作为依据')[0]
    assert 'Java17/SpringBoot3' not in block.split('不作为依据')[0]


def test_confirm_dimension_without_a_prior_target_is_refused(store_project):
    store, project = store_project
    with pytest.raises(Conflict):
        lm.confirm_dimension(store, project['id'], 'browser', actor='customer_pm')


def test_confirm_dimension_rejects_unknown_dimension(store_project):
    store, project = store_project
    with pytest.raises(ValueError):
        lm.confirm_dimension(store, project['id'], 'quantum_computer', actor='x')


def test_register_target_rejects_unknown_dimension(store_project):
    store, project = store_project
    with pytest.raises(ValueError):
        lm.register_target(store, project['id'], {'nope': 'x'}, actor='x')


def test_disposal_plan_reports_missing_index_honestly(store_project):
    store, project = store_project
    plan = lm.disposal_plan(store, project['id'])
    assert len(plan) == len(lm.DIMENSIONS)
    for dim in plan:
        assert dim['blocked'] is True  # nothing registered yet
        for loc in dim['locations']:
            assert loc['source'] == 'none'
            assert '未建立代码索引' in loc['note']


def test_disposal_plan_unblocks_only_the_confirmed_dimension(store_project):
    store, project = store_project
    lm.register_target(store, project['id'], {'database': '达梦 DM8'},
                       actor='evaluator', source='report')
    lm.confirm_dimension(store, project['id'], 'database', actor='pm')
    plan = {d['dimension']: d for d in lm.disposal_plan(store, project['id'])}
    assert plan['database']['blocked'] is False
    assert plan['browser']['blocked'] is True


# ---------------------------------------------------------------------------
# normalize() validation
# ---------------------------------------------------------------------------

def _valid_request(base, **over):
    return {
        'project_id': 'p1', 'repository': 'synthetic/xinchuang', 'base_sha': base,
        'dimension': 'database', 'scope_paths': ['db_url.py'],
        'expected_behaviour': '旧的 mysql 方言行为不变，新增 dm 方言',
        'delivery_goal': '导出补丁与回执',
        'agreement': {'revision': 'v1'}, 'idempotency_key': 'slice-key-00000001',
        **over,
    }


def test_normalize_requires_a_concrete_non_empty_scope():
    base = '0' * 40
    with pytest.raises(ValueError, match='范围'):
        lm.normalize(_valid_request(base, scope_paths=[]))


def test_normalize_rejects_unknown_dimension():
    base = '0' * 40
    with pytest.raises(ValueError, match='维度'):
        lm.normalize(_valid_request(base, dimension='quantum'))


def test_normalize_requires_full_sha():
    with pytest.raises(ValueError, match='SHA'):
        lm.normalize(_valid_request('main'))


def test_content_fingerprint_changes_with_scope():
    base = '0' * 40
    a = lm.normalize(_valid_request(base))
    b = lm.normalize(_valid_request(base, scope_paths=['other.py']))
    assert lm.content_fingerprint(a) != lm.content_fingerprint(b)


# ---------------------------------------------------------------------------
# vertical: one synthetic slice through the real executor
# ---------------------------------------------------------------------------

BROKEN = '''def build_url(dialect, host, db):
    """Return the JDBC-style connection URL for the given dialect."""
    if dialect == 'mysql':
        return f'jdbc:mysql://{host}/{db}?useSSL=false'
    raise ValueError(f'unsupported dialect: {dialect}')
'''

FIXED = '''def build_url(dialect, host, db):
    """Return the JDBC-style connection URL for the given dialect."""
    if dialect == 'mysql':
        return f'jdbc:mysql://{host}/{db}?useSSL=false'
    if dialect == 'dm':
        return f'jdbc:dm://{host}/{db}'
    raise ValueError(f'unsupported dialect: {dialect}')
'''

TEST = '''import sys
sys.path.insert(0, '.')
from db_url import build_url


def test_mysql_dialect_is_unchanged():
    assert build_url('mysql', 'h', 'd') == 'jdbc:mysql://h/d?useSSL=false'


def test_dm_dialect_is_now_supported():
    assert build_url('dm', 'h', 'd') == 'jdbc:dm://h/d'
'''

# Named ``regression`` on purpose: it does not contain any of
# ``DIMENSION_HINTS['database']`` ('jdbc'/'datasource'/'database'/'dm'/'sql'),
# so the receipt's automatic dimension-coverage check has nothing to match --
# passing this check proves the URL builder is correct, not that a real 达梦
# instance was ever reached, and the receipt must say so.
CHECK = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:randomly', 'test_db_url.py']


def _git(root, *args, **kw):
    return subprocess.run(['git', *args], cwd=root, check=True,
                          capture_output=True, text=True, **kw)


def _legacy_repo(tmp_path):
    root = tmp_path / 'legacy'
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'f@l')
    _git(root, 'config', 'user.name', 'F')
    (root / 'db_url.py').write_text(BROKEN)
    (root / 'test_db_url.py').write_text(TEST)
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'legacy baseline')
    base = _git(root, 'rev-parse', 'HEAD').stdout.strip()
    return root, base


def _check_exit(root):
    return subprocess.run(CHECK, cwd=root, capture_output=True, text=True).returncode


class _FakeModel:
    def __init__(self):
        self.requests, self.heads = [], []

    def run(self, request, emit, cancel=None):
        self.requests.append(request)
        self.heads.append(_git(request.workspace, 'rev-parse', 'HEAD').stdout.strip())
        (Path(request.workspace) / 'db_url.py').write_text(FIXED)
        return ProviderResult('新增达梦方言支持，mysql 方言保持不变', cost_usd=0.0)


def _service(store, project, model, *, check_name='regression'):
    class _Svc:
        def __init__(self):
            self.store = store
            self.governance = None
            self.dispatched = []

        def start_plan(self, rid):
            self.dispatched.append(rid)
            run = store.get(rid)
            artifacts = execute_plan(
                run_id=rid,
                plan={'tasks': [{'id': 'dm-dialect', 'title': '支持达梦方言',
                                 'prompt': run['request'],
                                 'paths': ('db_url.py',), 'checks': [check_name]}]},
                project=project,
                profiles={role: {'provider': 'fake', 'model': 'coder'}
                          for role in ('planner', 'cheap', 'standard', 'strong')},
                runner=model, emit=lambda *a, **k: None,
                cancel=threading.Event(), timeout_s=300)
            store.update(rid, {'status': 'ready_for_review', 'artifacts': artifacts},
                         expected=('received',), event=('run.verified', {}))

    return _Svc()


def _plans(store, project, model, *, check_name='regression'):
    svc = _service(store, project, model, check_name=check_name)
    execution = lm.ModernizationExecution(store, dispatch=svc.start_plan, cost=lambda eid: 0.0)
    port = lm.ModernizationPlans(store, execution=execution,
                                 repository=WebuddyRepository(store),
                                 identity=WebuddyIdentity())
    port.svc = svc
    return port


def _slice_request(base, **over):
    return {'project_id': 'p-xinchuang', 'repository': 'synthetic/xinchuang',
            'base_sha': base, 'base_branch_label': 'main',
            'dimension': 'database', 'scope_paths': ['db_url.py'],
            'expected_behaviour': 'mysql 方言输出完全不变，新增 dm 方言输出',
            'delivery_goal': '导出可下载补丁并给出回执',
            'agreement': {'revision': 'v1', 'skill_version': 'legacy-modernization@1'},
            'idempotency_key': 'synthetic-dm-1', 'synthetic': True,
            'known_gaps': ['没有达梦（DM8）真实实例，未执行连接可用性验证，只验证了 URL 构造'],
            'code_locations': [{'path': 'db_url.py', 'line': 1, 'source': 'manual'}],
            **over}


@pytest.fixture
def slice_(tmp_path):
    root, base = _legacy_repo(tmp_path)
    store = Store(tmp_path / 'control.db')
    project = store.add_project({
        'name': '合成信创仓库', 'repository': 'synthetic/xinchuang',
        'workspace': str(root), 'base_branch': 'main', 'budget_usd': 5.0,
        'checks': {'regression': CHECK}, 'max_tasks': 1})
    model = _FakeModel()
    port = _plans(store, {**project, 'expected_base_sha': base}, model)
    port.model, port.repo_root, port.base, port.project = model, root, base, project
    return port


def test_the_baseline_really_fails_before_anything_is_dispatched(slice_):
    assert _check_exit(slice_.repo_root) != 0, '合成仓库在基线上必须真的失败（缺 dm 方言支持）'


def test_a_synthetic_slice_becomes_a_verified_patch_and_an_honest_receipt(slice_):
    assert _check_exit(slice_.repo_root) != 0

    view = slice_.create_slice(
        _slice_request(slice_.base, project_id=slice_.project['id']), actor=ACTOR)
    assert view['status'] == 'delivered', view.get('blocking_reason')
    assert view['dimension'] == 'database'
    assert view['code_locations'] == [{'path': 'db_url.py', 'line': 1, 'source': 'manual'}]

    request = slice_.model.requests[0]
    assert slice_.model.heads == [slice_.base], \
        f'执行工作副本的 HEAD 应等于登记的基线，实际 {slice_.model.heads}'
    assert Path(request.workspace).resolve() != slice_.repo_root.resolve()
    assert 'synthetic/xinchuang' in request.prompt and slice_.base in request.prompt
    assert '数据库/版本' in request.prompt
    assert '没有达梦（DM8）真实实例' in request.prompt, '已知缺口必须原样送到执行器提示词'

    exported = slice_.export(view['slice_id'], actor=ACTOR)
    receipt, patch = exported['receipt'], exported['artifacts'][0]['bytes']

    assert receipt['checks'] == [{'name': 'regression', 'passed': True, 'exit_code': 0,
                                  'reused': False, 'identity_fingerprint': None}]
    # The check that ran is a real pass, but it is named "regression" -- it
    # does not, by name, plausibly cover 数据库 in the sense DIMENSION_HINTS
    # means, so the automatic coverage check must add its own unverified line
    # in addition to the explicitly declared known gap.
    assert any('未在真实目标环境验证' in item for item in receipt['unverified']), receipt['unverified']
    assert any('达梦' in item for item in receipt['unverified']), receipt['unverified']
    assert not any(item == '' for item in receipt['unverified'])
    assert receipt['synthetic'] is True
    assert '不代表任何客户成功案例' in exported['text']
    assert '未在真实目标环境验证' in exported['text']
    assert receipt['baseline']['base_sha'] == slice_.base
    assert receipt['delivery']['diff_hash'] == hashlib.sha256(patch).hexdigest()

    fresh = slice_.repo_root.parent / 'fresh'
    _git(slice_.repo_root.parent, 'clone', '-q', str(slice_.repo_root), str(fresh))
    _git(fresh, 'checkout', '-q', slice_.base)
    assert _check_exit(fresh) != 0
    (fresh / 'delivered.patch').write_bytes(patch)
    _git(fresh, 'config', 'user.email', 'f@l')
    _git(fresh, 'config', 'user.name', 'F')
    _git(fresh, 'am', 'delivered.patch')
    assert _check_exit(fresh) == 0, '交付的补丁必须真的让检查转绿（含新旧方言）'


def test_a_dimension_with_a_matching_check_name_is_not_flagged_unverified(tmp_path):
    """The automatic coverage note only fires when nothing plausibly matches.

    Same synthetic repository and fix as the main vertical test, but the
    project's only check is named ``database_regression`` instead of
    ``regression``. That name contains one of ``DIMENSION_HINTS['database']``
    ('database'), so the receipt's automatic coverage check must *not* add its
    own line -- proving the earlier test's line came from the name match
    genuinely being absent, not from the code always appending it regardless
    of what ran. The explicitly declared ``known_gaps`` entry still travels
    through either way, because a real check's name is not proof a real 达梦
    instance was reached.
    """
    root, base = _legacy_repo(tmp_path)
    store = Store(tmp_path / 'control.db')
    project = store.add_project({
        'name': '合成信创仓库2', 'repository': 'synthetic/xinchuang2',
        'workspace': str(root), 'base_branch': 'main', 'budget_usd': 5.0,
        'checks': {'database_regression': CHECK}, 'max_tasks': 1})
    model = _FakeModel()
    port = _plans(store, {**project, 'expected_base_sha': base}, model,
                 check_name='database_regression')

    view = port.create_slice(_slice_request(
        base, project_id=project['id'], repository=project['repository'],
        idempotency_key='synthetic-dm-named'),
        actor=ACTOR)
    assert view['status'] == 'delivered', view.get('blocking_reason')

    exported = port.export(view['slice_id'], actor=ACTOR)
    unverified = exported['receipt']['unverified']
    assert not any('未在真实目标环境验证' in item for item in unverified), unverified
    assert any('达梦' in item for item in unverified), \
        '显式声明的已知缺口必须仍然出现，即便自动检测没有再追加一行'


def test_revise_slice_keeps_the_predecessor_and_reads_current_memory(slice_):
    lm.register_target(slice_.svc.store, slice_.project['id'], {'database': '达梦 DM8'},
                       actor='evaluator', source='report')
    lm.confirm_dimension(slice_.svc.store, slice_.project['id'], 'database', actor='pm')

    first = slice_.create_slice(
        _slice_request(slice_.base, project_id=slice_.project['id']), actor=ACTOR)
    assert first['status'] == 'delivered'
    slice_.export(first['slice_id'], actor=ACTOR)

    # A follow-up requirement on the same slice is a revision, not a fresh
    # unrelated record, and it must be able to read the constraint confirmed
    # after the first slice was already delivered.
    second = slice_.revise_slice(
        first['slice_id'],
        _slice_request(slice_.base, project_id=slice_.project['id'],
                       idempotency_key='synthetic-dm-1-revised',
                       expected_behaviour='mysql 方言不变，dm 方言支持连接池参数'),
        actor=ACTOR)
    assert second['predecessor_id'] == first['slice_id']
    assert second['status'] == 'delivered'

    prompt = slice_.model.requests[-1].prompt
    assert '达梦 DM8' in prompt, '已确认的项目记忆必须出现在后续修订的执行提示词里'

    # The predecessor's own receipt is untouched.
    predecessor_receipts = slice_.records.receipts(first['slice_id'])
    assert len(predecessor_receipts) == 1


def test_revise_with_identical_content_is_refused(slice_):
    first = slice_.create_slice(
        _slice_request(slice_.base, project_id=slice_.project['id']), actor=ACTOR)
    with pytest.raises(Conflict):
        slice_.revise_slice(first['slice_id'],
                            _slice_request(slice_.base, project_id=slice_.project['id']),
                            actor=ACTOR)


# ---------------------------------------------------------------------------
# HTTP: plugin gate wiring and one full round trip through the mounted router
# ---------------------------------------------------------------------------

class _FakeAvailability:
    """A minimal stand-in for ``PluginAvailability``, fixed to one state.

    Used only to inject a deterministic gate into a test-mounted router --
    production wiring always uses the real, durable ``PluginAvailability``.
    """

    def __init__(self, state='enabled'):
        self._state = state

    def state(self, plugin_id):
        return {'plugin_id': plugin_id, 'state': self._state, 'revision': 1,
                'updated_at': None, 'actor': 'test'}

    def view(self, plugin_id):
        return self.state(plugin_id)


class FakeSDK:
    """Never called in these tests -- ``app_env`` needs *some* runner to boot."""

    def available(self):
        return []

    def run(self, request, emit, cancel=None):  # pragma: no cover
        raise AssertionError('the fake SDK should not be reached by these tests')


@pytest.fixture
def http_env(tmp_path):
    repo = tmp_path / 'repos' / 'xinchuang'
    repo.mkdir(parents=True)
    for args in (['init', '-q', '-b', 'main'], ['config', 'user.name', 'T'],
                 ['config', 'user.email', 't@example.com']):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    (repo / 'db_url.py').write_text(BROKEN)
    (repo / 'test_db_url.py').write_text(TEST)
    subprocess.run(['git', 'add', '-A'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True, capture_output=True)
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()

    data = tmp_path / 'data'
    store = Store(data / 'control.db')
    profiles = {k: {'provider': 'codex', 'model': 'test'} for k in
                ('planner', 'cheap', 'standard', 'strong')}
    svc = Service(store, runner=FakeSDK(), profiles=profiles)
    app = create_app(data_dir=data, workspace_root=tmp_path / 'repos',
                     public_origin='http://testserver', service=svc,
                     webhook_secret='test-webhook-secret')
    app.state.auth.create_user('owner', 'a-long-test-password')
    with TestClient(app) as client:
        yield client, store, svc, repo, base


def _replace_mount(client, router_obj):
    """Swap production's ``/api/v2/modernization`` routes for this router's.

    ``create_app`` now registers ``modernization_router(store, svc)`` for real,
    so merely *inserting* another copy would change nothing: the app's own
    routes come first and win, and a test that thought it had injected a fake
    dispatcher would silently be driving the production one. The existing
    routes are therefore dropped before the replacements go in, at the same
    position, so ordering against the catch-all is unchanged.

    A test that wants production's wiring simply does not call this.
    """
    routes = client.app.router.routes
    prefix = '/api/v2/modernization'
    keep = [r for r in routes if not str(getattr(r, 'path', '')).startswith(prefix)]
    assert len(keep) < len(routes), 'app.py 应当已经接线了信创路由，这里却一条都没找到'
    index = next(i for i, r in enumerate(keep)
                 if getattr(r, 'path', None) == '/api/{path:path}')
    keep[index:index] = router_obj.routes
    routes[:] = keep


def _login(client):
    response = client.post('/api/auth/login',
                           json={'username': 'owner', 'password': 'a-long-test-password'},
                           headers={'Origin': 'http://testserver'})
    assert response.status_code == 200, response.text
    return {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}


def _project(client, repo, headers):
    response = client.post('/api/v2/projects', json={
        'name': 'Xinchuang', 'repository': 'owner/xinchuang', 'budget_usd': 10,
        'workspace': str(repo), 'checks': {'regression': CHECK}}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_disabled_plugin_blocks_new_slices_but_not_reads_over_http(http_env):
    client, store, svc, repo, base = http_env
    # No mounting: these are the routes ``create_app`` registered. The plugin is
    # declared executable but seeds ``disabled`` (``default_enabled=False``), so
    # this is production's actual state on a fresh install, not a fixture choice.
    headers = _login(client)
    p = _project(client, repo, headers)

    body = {'project_id': p['id'], 'repository': p['repository'], 'base_sha': base,
            'dimension': 'database', 'scope_paths': ['db_url.py'],
            'expected_behaviour': 'x', 'delivery_goal': 'y',
            'agreement': {'revision': 'v1'}, 'idempotency_key': 'blocked-key-1'}
    res = client.post('/api/v2/modernization/slices', json=body, headers=headers)
    assert res.status_code == 409, res.text
    assert '已停用' in res.text

    res = client.post('/api/v2/modernization/targets',
                      json={'project_id': p['id'], 'target': {'database': '达梦 DM8'}},
                      headers=headers)
    assert res.status_code == 409, res.text

    # Reads stay reachable while disabled -- "隐藏创建入口不足以停用" cuts both
    # ways: a stop must not make history or the current picture unreachable.
    res = client.get(f"/api/v2/modernization/targets?project_id={p['id']}", headers=headers)
    assert res.status_code == 200
    assert len(res.json()['dimensions']) == len(lm.DIMENSIONS)

    res = client.get(f"/api/v2/modernization/disposal-plan?project_id={p['id']}", headers=headers)
    assert res.status_code == 200

    res = client.get(f"/api/v2/modernization/locate?project_id={p['id']}&q=jdbc", headers=headers)
    assert res.status_code == 200
    assert res.json()['source'] == 'none'


def test_approval_is_gated_even_though_it_bypasses_the_port(http_env):
    """``approve`` hands its decision to the run lifecycle, not to the port.

    Approving a plan is what makes the executor start changing files, so a
    stopped plugin that still accepted approvals would be stopped in name only.
    The port's own methods cannot demonstrate this: ``approve`` never touches
    them, which is exactly why the route has to ask the gate by hand and why
    this needs its own test rather than riding on the create-path refusal.

    A fixed ``disabled`` gate stands in for the state, because no reachable
    transition produces "disabled while a slice awaits approval" -- draining
    refuses to stop while that slice counts as live -- leaving only the window
    between counting and writing the state row.
    """
    client, store, svc, repo, base = http_env
    from factory.control.modernization_routes import router as modernization_router
    headers = _login(client)
    p = _project(client, repo, headers)

    _replace_mount(client, modernization_router(
        store, svc, availability=_FakeAvailability('enabled'),
        dispatch=lambda rid: None))
    body = {'project_id': p['id'], 'repository': p['repository'], 'base_sha': base,
            'dimension': 'database', 'scope_paths': ['db_url.py'],
            'expected_behaviour': 'x', 'delivery_goal': 'y',
            'agreement': {'revision': 'v1'}, 'idempotency_key': 'approve-gate-1'}
    created = client.post('/api/v2/modernization/slices', json=body, headers=headers)
    assert created.status_code == 201, created.text
    slice_id = created.json()['slice_id']

    _replace_mount(client, modernization_router(
        store, svc, availability=_FakeAvailability('disabled')))
    refused = client.post(f'/api/v2/modernization/slices/{slice_id}/approve',
                          headers=headers)
    assert refused.status_code == 409, refused.text
    assert '停用' in refused.text
    # The refusal happens before the plan is handed on: the run is untouched.
    execution_id = created.json()['execution_id']
    assert store.get(execution_id)['status'] == 'received'


def test_one_slice_goes_end_to_end_through_the_mounted_router(http_env):
    """With an injected fake gate and a fake dispatch that calls the real
    executor directly (the same substitution ``test_issue_maintenance_vertical``
    makes for the port-level test), the HTTP surface itself really produces a
    patch and a receipt -- proving the route wiring, not just the port.
    """
    client, store, svc, repo, base = http_env
    from factory.control.modernization_routes import router as modernization_router

    headers = _login(client)
    p = _project(client, repo, headers)
    model = _FakeModel()

    def fake_dispatch(rid):
        run = store.get(rid)
        project = {**store.project(p['id']), 'expected_base_sha': base}
        artifacts = execute_plan(
            run_id=rid,
            plan={'tasks': [{'id': 'dm-dialect', 'title': '支持达梦方言',
                             'prompt': run['request'], 'paths': ('db_url.py',),
                             'checks': ['regression']}]},
            project=project,
            profiles={role: {'provider': 'fake', 'model': 'coder'}
                      for role in ('planner', 'cheap', 'standard', 'strong')},
            runner=model, emit=lambda *a, **k: None,
            cancel=threading.Event(), timeout_s=300)
        store.update(rid, {'status': 'ready_for_review', 'artifacts': artifacts},
                     expected=('received',), event=('run.verified', {}))

    _replace_mount(client, modernization_router(
        store, svc, availability=_FakeAvailability('enabled'), dispatch=fake_dispatch))

    res = client.post('/api/v2/modernization/targets',
                      json={'project_id': p['id'], 'target': {'database': '达梦 DM8'},
                            'source': '_MIGRATION_ASSESSMENT.md'},
                      headers=headers)
    assert res.status_code == 201, res.text
    assert res.json()['entries'][0]['status'] == 'candidate'

    body = {'project_id': p['id'], 'repository': p['repository'], 'base_sha': base,
            'dimension': 'database', 'scope_paths': ['db_url.py'],
            'expected_behaviour': 'mysql 方言不变，新增 dm 方言',
            'delivery_goal': '导出补丁与回执',
            'agreement': {'revision': 'v1'}, 'idempotency_key': 'http-slice-1',
            'known_gaps': ['没有达梦真实实例']}
    res = client.post('/api/v2/modernization/slices', json=body, headers=headers)
    assert res.status_code == 201, res.text
    view = res.json()
    assert view['status'] == 'delivered', view.get('blocking_reason')

    res = client.get(f"/api/v2/modernization/slices/{view['slice_id']}", headers=headers)
    assert res.status_code == 200 and res.json()['status'] == 'delivered'

    res = client.get(f"/api/v2/modernization/slices/{view['slice_id']}/export", headers=headers)
    assert res.status_code == 200, res.text
    receipt = res.json()['receipt']
    assert receipt['delivery']['commit']
    assert any('达梦' in item for item in receipt['unverified'])

    name = receipt['delivery']['artifacts'][0]
    res = client.get(f"/api/v2/modernization/slices/{view['slice_id']}/artifacts/{name}",
                     headers=headers)
    assert res.status_code == 200
    assert res.content.startswith(b'From ')  # a real git format-patch payload
