"""One synthetic contract, one real HTTP round trip against a local mock third
party, end to end.

Everything here is a synthetic example and the receipt says so. There is no
customer contract, no customer credential, and no claim about a real GP Server
integration -- ``docs/.../03-INTEGRATION.md`` and the installed pack both say
the real vendor spec has not been obtained yet.

What makes the vertical-slice test more than a formatting exercise:

* The local mock third party is a real ``http.server.HTTPServer`` bound to
  ``127.0.0.1:0`` (an OS-assigned ephemeral port), started *inside the check
  subprocess* that ``execute_plan`` runs -- not mocked at the Python-import
  level and not stubbed inside this module. It is a real socket accepting a
  real HTTP request. It is explicitly tagged ``mock: True`` in every result it
  produces, which is how "this was a local test double, not the real vendor"
  survives into the receipt.
* The baseline adapter genuinely fails the round trip (no Authorization header
  -> the mock server's real 401), measured directly, before any fix is
  dispatched.
* The fix is made through the real ``execute_plan`` -- the same executor
  production uses -- with an explicit fake model standing in for the coding
  provider, exactly as ``tests/test_issue_maintenance_vertical.py`` does for
  the maintenance module. No paid call happens, no second executor exists.
* The three business-outcome fields (``technical_success``, ``business_accepted``,
  ``business_completed``) are read back out of the check's own captured
  stdout -- printed by code that actually made the call -- not invented by
  this test or by the module from an HTTP status code.
"""
import hashlib
import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from factory.control import scenario_memory
from factory.control.api_adaptation import (
    SOURCE_TYPE, AdaptationExecution, AdaptationIdentity, AdaptationRepository,
    AdaptationTasks, PLUGIN_ID, affected_mappings, content_fingerprint,
    contract_diff, normalize, tasks_for,
)
from factory.control.execution import execute_plan
from factory.control.providers import ProviderResult
from factory.control.store import Conflict, Store

ACTOR = {'id': 7, 'username': 'integrator'}


# ---------------------------------------------------------------------------
# The synthetic adapter under test: a real (broken, then fixed) HTTP client,
# and a real local mock third-party endpoint that the check script starts.
# ---------------------------------------------------------------------------

BROKEN_ADAPTER = '''import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta


def _to_shanghai(iso_utc):
    dt = datetime.strptime(iso_utc, '%Y-%m-%dT%H:%M:%SZ') + timedelta(hours=8)
    return dt.strftime('%Y-%m-%dT%H:%M:%S+08:00')


def send_item(url, source_item, auth_token):
    payload = {
        'item_id': source_item['id'],
        'total_amount': round(float(source_item['amount']), 4),
        'last_modified': _to_shanghai(source_item['updated_at']),
        'is_active': source_item['status'] == 'active',
    }
    data = json.dumps(payload).encode()
    # BUG: no Authorization header -- the mock (and the real vendor) rejects
    # this with a real 401 before it ever looks at the body.
    req = urllib.request.Request(url, data=data, method='POST',
                                  headers={'Content-Type': 'application/json'})
    correlation_id = str(uuid.uuid4())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            body = json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = {}
    technical_success = 200 <= status < 300
    business_accepted = technical_success and body.get('code') == 'ACCEPTED'
    return {
        'correlation_id': correlation_id,
        'technical_success': technical_success,
        'business_accepted': business_accepted,
        # One-way report: this scope has no channel that confirms final
        # business completion, so it is never guessed as True.
        'business_completed': False,
        'mock': True,
        'endpoint': 'POST /v2/projects/{project_id}/items',
        'sanitized_response': {'code': body.get('code'), 'request_id': body.get('request_id')},
    }
'''

FIXED_ADAPTER = BROKEN_ADAPTER.replace(
    "headers={'Content-Type': 'application/json'})",
    "headers={'Content-Type': 'application/json',\n"
    "                                           'Authorization': f'Bearer {auth_token}'})")

CHECK_SCRIPT = '''import http.server
import json
import sys
import threading

sys.path.insert(0, '.')
import gp_adapter

EXPECTED_TOKEN = 'sandbox-secret-token'


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        if self.headers.get('Authorization') != f'Bearer {EXPECTED_TOKEN}':
            self.send_response(401)
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'unauthorized'}).encode())
            return
        required = ('item_id', 'total_amount', 'last_modified', 'is_active')
        if not all(k in body for k in required):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'code': 'BIZ_INVALID', 'message': 'missing field'}).encode())
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'code': 'ACCEPTED', 'request_id': 'req-8f21'}).encode())

    def log_message(self, *a):
        pass


def main():
    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source_item = {'id': 'itm-01', 'amount': 12.3,
                        'updated_at': '2026-09-20T08:00:00Z', 'status': 'active'}
        result = gp_adapter.send_item(
            f'http://127.0.0.1:{port}/v2/projects/p1/items', source_item,
            auth_token=EXPECTED_TOKEN)
        print('ADAPTATION_RESULT: ' + json.dumps(result))
        ok = bool(result.get('technical_success')) and bool(result.get('business_accepted'))
        sys.exit(0 if ok else 1)
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
'''

CHECK = [sys.executable, 'check_adapt.py']


def _git(root, *args, **kw):
    return subprocess.run(['git', *args], cwd=root, check=True,
                          capture_output=True, text=True, **kw)


def _adapter_repo(tmp_path, adapter_source=BROKEN_ADAPTER):
    root = tmp_path / 'adapter-repo'
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'a@d')
    _git(root, 'config', 'user.name', 'A')
    (root / 'gp_adapter.py').write_text(adapter_source)
    (root / 'check_adapt.py').write_text(CHECK_SCRIPT)
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'baseline adapter (broken auth)')
    base = _git(root, 'rev-parse', 'HEAD').stdout.strip()
    return root, base


def _run_check_exit(root):
    return subprocess.run(CHECK, cwd=root, capture_output=True, text=True).returncode


CONTRACT_FIELDS_V1 = {
    'id': {'type': 'string'},
    'amount': {'type': 'decimal', 'precision': 2},
    'updated_at': {'type': 'datetime', 'timezone': 'UTC'},
    'status': {'type': 'enum', 'enum': ['active', 'archived']},
    'metadata': {'type': 'object'},
}


def _contract(*, version='v2.1', fields=None):
    return {
        'source_api': {'name': 'Sample External API', 'version': version,
                       'base_url': 'https://api.example.invalid/v2',
                       'auth': 'Bearer token (OAuth2 client_credentials)',
                       'environment': 'mock'},
        'target_adapter': {'name': 'GP Adapter', 'entry': 'gp_adapter.py'},
        'endpoints': [
            {'method': 'GET', 'path': '/projects/{project_id}/items'},
            {'method': 'POST', 'path': '/projects/{project_id}/items'},
        ],
        'fields': dict(fields or CONTRACT_FIELDS_V1),
    }


def _mapping_matrix(*, unmapped=True):
    matrix = [
        {'source_field': 'id', 'mapped': True, 'target_field': 'item_id',
         'type': 'string', 'transform': '直接映射'},
        {'source_field': 'amount', 'mapped': True, 'target_field': 'total_amount',
         'type': 'decimal', 'precision': '源 2 位小数，目标 4 位小数，需补零',
         'transform': '精度对齐'},
        {'source_field': 'updated_at', 'mapped': True, 'target_field': 'last_modified',
         'type': 'datetime', 'timezone': 'UTC -> Asia/Shanghai',
         'transform': '时区转换'},
        {'source_field': 'status', 'mapped': True, 'target_field': 'is_active',
         'type': 'bool', 'enum': ['active', 'archived'],
         'transform': 'active->true, archived->false'},
    ]
    if unmapped:
        matrix.append({'source_field': 'metadata', 'mapped': False,
                       'impact': '目标 Adapter 无对应字段，metadata 中的自定义标签会丢失，需确认是否扩展目标接口'})
    return matrix


def _message(**over):
    return {
        'name': '上报条目', 'method': 'POST', 'path': '/v2/projects/{project_id}/items',
        'idempotent': False,
        'retry_policy': '非幂等创建；依赖宿主 delivery_id/semantic_id 去重，适配层不做超时重试。',
        'sample_payload': {'id': 'itm-01', 'amount': 12.3,
                           'updated_at': '2026-09-20T08:00:00Z', 'status': 'active'},
        **over,
    }


def _request(base, project_id, *, contract=None, mapping_matrix=None, message=None, **over):
    return {
        'project_id': project_id, 'repository': 'synthetic/gp-adapter',
        'base_sha': base, 'base_branch_label': 'main',
        'contract': contract or _contract(),
        'mapping_matrix': mapping_matrix if mapping_matrix is not None else _mapping_matrix(),
        'message': message or _message(),
        'expected_behaviour': '一条条目上报能被目标端正确鉴权、映射并受理',
        'delivery_goal': '导出可下载补丁并给出真实联调回执',
        'agreement': {'revision': 'v1', 'skill_version': 'api-adaptation@1'},
        'idempotency_key': over.pop('idempotency_key', f'synthetic-{uuid.uuid4().hex[:8]}'),
        'synthetic': True,
        **over,
    }


# ---------------------------------------------------------------------------
# normalize() / diff helpers -- unit level, no execution involved.
# ---------------------------------------------------------------------------

def test_normalize_rejects_a_field_marked_unmapped_with_no_stated_impact():
    bad = _mapping_matrix(unmapped=False)
    bad.append({'source_field': 'metadata', 'mapped': False})
    with pytest.raises(ValueError, match='impact'):
        normalize(_request('0' * 40, 'p1', mapping_matrix=bad))


def test_normalize_rejects_a_mapped_field_with_no_target_field():
    bad = _mapping_matrix(unmapped=False)
    bad.append({'source_field': 'metadata', 'mapped': True})
    with pytest.raises(ValueError, match='target_field'):
        normalize(_request('0' * 40, 'p1', mapping_matrix=bad))


def test_normalize_requires_a_retry_policy_for_a_non_idempotent_message():
    with pytest.raises(ValueError, match='重试'):
        normalize(_request('0' * 40, 'p1',
                           message=_message(idempotent=False, retry_policy='')))


def test_normalize_keeps_http_operations_business_messages_and_fields_distinct():
    normalized = normalize(_request('0' * 40, 'p1'))
    unmapped = [m for m in normalized['mapping_matrix'] if not m['mapped']]
    assert len(normalized['contract']['endpoints']) == 2
    assert len(normalized['mapping_matrix']) == 5
    assert [m['source_field'] for m in unmapped] == ['metadata']


def test_content_fingerprint_changes_when_mapping_changes():
    a = normalize(_request('0' * 40, 'p1'))
    changed = _mapping_matrix()
    changed[0] = {**changed[0], 'target_field': 'different_id'}
    b = normalize(_request('0' * 40, 'p1', mapping_matrix=changed))
    assert content_fingerprint(a) != content_fingerprint(b)


def test_contract_diff_and_affected_mappings_locate_the_changed_field():
    old = _contract(version='v2.1')
    new = _contract(version='v2.2', fields={
        **CONTRACT_FIELDS_V1, 'amount': {'type': 'decimal', 'precision': 4}})
    diff = contract_diff(old, new)
    assert diff['fields_changed'] == ['amount']
    assert diff['fields_added'] == [] and diff['fields_removed'] == []
    affected = affected_mappings(diff, _mapping_matrix())
    assert [a['source_field'] for a in affected] == ['amount']


def test_contract_diff_reports_a_removed_field():
    old = _contract(version='v1')
    new_fields = dict(CONTRACT_FIELDS_V1)
    del new_fields['metadata']
    new = _contract(version='v2', fields=new_fields)
    diff = contract_diff(old, new)
    assert diff['fields_removed'] == ['metadata']


# ---------------------------------------------------------------------------
# The real vertical slice: broken auth -> real 401 -> real fix -> real 200 +
# business acceptance, read back from the check's own stdout.
# ---------------------------------------------------------------------------

class _FakeModel:
    """The explicit stand-in for the coding provider. No network, no money.

    Writes the fix into the worktree the executor handed it -- production's
    own path downstream (checks, commit, patch) is exercised for real.
    """

    def __init__(self, fixed_source=FIXED_ADAPTER):
        self.fixed_source = fixed_source
        self.requests = []

    def run(self, request, emit, cancel=None):
        self.requests.append(request)
        (Path(request.workspace) / 'gp_adapter.py').write_text(self.fixed_source)
        return ProviderResult('已补上鉴权头', cost_usd=0.0)


def _service(store, project, model):
    class _Svc:
        def __init__(self):
            self.store = store
            self.governance = None

        def start_plan(self, rid):
            run = store.get(rid)
            artifacts = execute_plan(
                run_id=rid,
                plan={'tasks': [{'id': 'fix', 'title': '补上鉴权头',
                                 'prompt': run['request'],
                                 'paths': ('gp_adapter.py',), 'checks': ['adapt']}]},
                project=project,
                profiles={role: {'provider': 'fake', 'model': 'coder'}
                          for role in ('planner', 'cheap', 'standard', 'strong')},
                runner=model, emit=lambda *a, **k: None,
                cancel=threading.Event(), timeout_s=300)
            store.update(rid, {'status': 'ready_for_review', 'artifacts': artifacts},
                         expected=('received',), event=('run.verified', {}))

    return _Svc()


def _tasks(store, project, model):
    svc = _service(store, project, model)
    execution = AdaptationExecution(store, dispatch=svc.start_plan, cost=lambda eid: 0.0)
    return AdaptationTasks(store, execution=execution,
                           repository=AdaptationRepository(store),
                           identity=AdaptationIdentity())


@pytest.fixture
def slice_(tmp_path):
    root, base = _adapter_repo(tmp_path)
    store = Store(tmp_path / 'control.db')
    project = store.add_project({
        'name': '合成三方适配仓库', 'repository': 'synthetic/gp-adapter',
        'workspace': str(root), 'base_branch': 'main', 'budget_usd': 5.0,
        'checks': {'adapt': CHECK}, 'max_tasks': 1})
    model = _FakeModel()
    port = _tasks(store, {**project, 'expected_base_sha': base}, model)
    port.model, port.repo_root, port.base, port.project = model, root, base, project
    return port


def test_the_baseline_really_fails_the_real_round_trip(slice_):
    """The premise of the whole slice, measured rather than asserted in prose."""
    assert _run_check_exit(slice_.repo_root) != 0, '合成仓库在基线上必须真的因缺鉴权头而失败'


def test_a_contract_import_becomes_a_verified_patch_and_a_real_business_call(slice_):
    view = slice_.create(_request(slice_.base, slice_.project['id']), actor=ACTOR)
    assert view['status'] == 'delivered', view

    # HTTP operations, business messages and fields counted separately.
    assert view['counts'] == {'http_operations': 2, 'business_messages': 1, 'fields': 5}
    assert [m['source_field'] for m in view['unmapped_fields']] == ['metadata']

    call = view['business_call']
    assert call is not None, '必须能从检查的真实输出里读到业务调用结果'
    assert call['technical_success'] is True
    assert call['business_accepted'] is True
    # One-way reporting has no completion channel in this scope: never guessed True.
    assert call['business_completed'] is False
    assert call['mock'] is True, '本地模拟第三方端点必须被标为 mock'
    assert call['correlation_id']
    # The sanitized response never carries the bearer token or the raw payload.
    assert 'sandbox-secret-token' not in json.dumps(call['sanitized_response'])
    assert set(call['sanitized_response']) <= {'code', 'request_id'}

    exported = slice_.export(view['task_id'], actor=ACTOR)
    receipt, patch = exported['receipt'], exported['artifacts'][0]['bytes']
    assert receipt['business_call']['technical_success'] is True
    assert receipt['business_call']['business_accepted'] is True
    assert receipt['business_call']['business_completed'] is False
    assert receipt['mapping_summary'] == {'fields': 5, 'mapped': 4, 'unmapped': 1}
    assert receipt['unmapped_fields'] == [
        {'source_field': 'metadata',
         'impact': '目标 Adapter 无对应字段，metadata 中的自定义标签会丢失，需确认是否扩展目标接口'}]
    assert receipt['synthetic'] is True
    assert '不代表任何客户验收案例' in exported['text']
    assert '业务最终完成：False' in exported['text']
    assert '这是对本地模拟第三方端点的联调' in exported['text']

    # The patch really fixes a fresh checkout of the pinned baseline.
    assert receipt['delivery']['diff_hash'] == hashlib.sha256(patch).hexdigest()
    fresh = slice_.repo_root.parent / 'fresh'
    _git(slice_.repo_root.parent, 'clone', '-q', str(slice_.repo_root), str(fresh))
    _git(fresh, 'checkout', '-q', slice_.base)
    assert _run_check_exit(fresh) != 0
    (fresh / 'delivered.patch').write_bytes(patch)
    _git(fresh, 'config', 'user.email', 'a@d')
    _git(fresh, 'config', 'user.name', 'A')
    _git(fresh, 'am', 'delivered.patch')
    assert _run_check_exit(fresh) == 0, '交付的补丁必须真的让业务报文被受理'


def test_unmapped_field_is_recorded_as_pending_not_silently_dropped(slice_):
    view = slice_.create(_request(slice_.base, slice_.project['id']), actor=ACTOR)
    entries = scenario_memory.recall(slice_.execution.store, view['project_id'],
                                     plugin_id=PLUGIN_ID)
    pending = [e for e in entries if '待确认字段' in e['title']]
    assert pending, '待确认字段必须写入项目记忆，而不是只存在于任务视图里'
    assert pending[0]['status'] == 'candidate', '未经确认的观察必须是 candidate，不能被当作已批准'
    assert 'metadata' in pending[0]['content']


def test_contract_memory_entry_is_scoped_and_unconfirmed(slice_):
    view = slice_.create(_request(slice_.base, slice_.project['id']), actor=ACTOR)
    entries = scenario_memory.recall(slice_.execution.store, view['project_id'],
                                     plugin_id=PLUGIN_ID)
    assert entries, '契约信息必须落地到项目记忆'
    assert all(scenario_memory.belongs_to(e, PLUGIN_ID) for e in entries)
    assert all(e['status'] == 'candidate' for e in entries)


# ---------------------------------------------------------------------------
# revise(): import a next contract version, diff it, keep old evidence.
# ---------------------------------------------------------------------------

def test_revise_diffs_the_contract_and_preserves_the_old_revisions_receipt(slice_):
    first = slice_.create(_request(slice_.base, slice_.project['id'],
                                   idempotency_key='synthetic-v1'), actor=ACTOR)
    exported_first = slice_.export(first['task_id'], actor=ACTOR)

    new_fields = {**CONTRACT_FIELDS_V1, 'amount': {'type': 'decimal', 'precision': 4}}
    second_request = _request(
        slice_.base, slice_.project['id'], contract=_contract(version='v2.2', fields=new_fields),
        idempotency_key='synthetic-v2')
    second = slice_.revise(first['task_id'], second_request, actor=ACTOR)

    assert second['task_id'] != first['task_id']
    assert second['revision'] == 2
    assert second['predecessor_id'] == first['task_id']
    assert second['contract_diff']['fields_changed'] == ['amount']
    assert [a['source_field'] for a in second['affected_mappings']] == ['amount']
    assert second['status'] == 'delivered'

    # The predecessor's own receipt is untouched by the successor's work.
    # ``receipts()`` adds its own ``at`` timestamp column on top of the stored
    # receipt body, which ``put_receipt``'s return value does not carry.
    preserved = slice_.records.receipts(first['task_id'])
    assert len(preserved) == 1
    assert {k: v for k, v in preserved[0].items() if k != 'at'} == exported_first['receipt']

    diff_entries = scenario_memory.recall(slice_.execution.store, first['project_id'],
                                          plugin_id=PLUGIN_ID)
    assert any('契约变更' in e['title'] for e in diff_entries)


def test_revise_refuses_byte_identical_content(slice_):
    first = slice_.create(_request(slice_.base, slice_.project['id'],
                                   idempotency_key='same-key-01'), actor=ACTOR)
    with pytest.raises(Conflict):
        slice_.revise(first['task_id'],
                      _request(slice_.base, slice_.project['id'], idempotency_key='same-key-02'),
                      actor=ACTOR)


# ---------------------------------------------------------------------------
# tasks_for(): the gated assembly. api-adaptation seeds ``disabled``
# in factory/control/plugins.py (integrator-owned), so its default
# availability is 'disabled' -- this confirms the gate actually reads that,
# rather than confirming a business outcome this plugin is not yet cleared for.
# ---------------------------------------------------------------------------

def test_tasks_for_is_gated_and_currently_disabled(tmp_path):
    store = Store(tmp_path / 'gated.db')
    project = store.add_project({'name': 'p', 'repository': 'r/r',
                                 'workspace': str(tmp_path), 'base_branch': 'main'})

    class _Svc:
        def __init__(self):
            self.store = store
            self.governance = None

        def start_plan(self, rid):
            raise AssertionError('disabled 状态下不应该派发任何执行')

    gated = tasks_for(_Svc())
    assert gated.gate.state() == 'disabled'
    with pytest.raises(Conflict):
        gated.create(_request('0' * 40, project['id']), actor=ACTOR)
    # Reading history is still allowed in every availability state.
    with pytest.raises(KeyError):
        gated.get('no-such-task', actor=ACTOR)


# ---------------------------------------------------------------------------
# HTTP surface wiring, standalone (app.py is integrator-owned and does not
# mount this router yet -- see OWNERSHIP.md). This exercises the real
# ``adaptation_routes.router`` against a minimal FastAPI app with the same
# Conflict/KeyError exception mapping app.py registers.
# ---------------------------------------------------------------------------

def test_http_router_wiring_and_gated_refusal(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from factory.control.adaptation_routes import router
    from factory.control.store import Conflict as _Conflict

    store = Store(tmp_path / 'http.db')
    project = store.add_project({'name': 'p', 'repository': 'r/r',
                                 'workspace': str(tmp_path), 'base_branch': 'main'})

    class _Svc:
        def __init__(self):
            self.store = store
            self.governance = None

        def start_plan(self, rid):
            raise AssertionError('未启用时不应该派发')

    app = FastAPI()

    @app.middleware('http')
    async def inject_user(request: Request, call_next):
        request.state.user = {'id': 1, 'username': 'tester'}
        return await call_next(request)

    @app.exception_handler(_Conflict)
    async def conflict(req, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(req, exc):
        return JSONResponse({'detail': '记录不存在'}, status_code=404)

    app.include_router(router(store, _Svc()))
    client = TestClient(app)

    availability = client.get('/api/v2/adaptation/availability')
    assert availability.status_code == 200
    assert availability.json()['state'] == 'disabled'
    # Declared executable (the handler below is real and tested), but a newly
    # wired scenario seeds off: an administrator turns it on per customer.
    assert availability.json()['executable'] is True

    created = client.post('/api/v2/adaptation/tasks',
                          json=_request('0' * 40, project['id']))
    assert created.status_code == 409, created.text
    assert '停用' in created.json()['detail'] or '未接入' in created.json()['detail']

    missing_task = client.get('/api/v2/adaptation/tasks/does-not-exist')
    assert missing_task.status_code == 404
