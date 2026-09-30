"""One fixed synthetic model drives actual modern APIs and independent review."""
import json
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient

from factory.control.app import create_app
from factory.control.auth import AuthError
from factory.control.providers import ProviderError, ProviderRequest
from factory.control.requirement_analysis import IDENTITY, validate
from factory.control.service import Service
from factory.control.store import Store
from scripts import preview_enterprise as fixture


def signed_workspace(tmp_path, *, goal=None, active=True):
    dest = tmp_path / '.spec/requirements/fixed/spec.md'
    dest.parent.mkdir(parents=True)
    payload = {'original_request': goal or fixture.FIXED_GOAL,
               'spec_draft': fixture.SPEC, 'fidelity_target': None}
    dest.write_text('---\nstatus: ' + ('active' if active else 'draft')
                    + '\n---\n## raw source\n\n' + json.dumps(payload, ensure_ascii=False)
                    + '\n\n## expanded spec\n', encoding='utf-8')
    return dest


def request(root, **kwargs):
    return ProviderRequest(provider='codex', model='synthetic', workspace=str(root),
                           prompt=kwargs.pop('prompt', ''), **kwargs)


def verification_prompt(extra=None):
    criteria = [{'id': 'test:' + str(i), 'text': text}
                for i, text in enumerate(sorted(fixture.SUPPORTED_CRITERIA))]
    if extra:
        criteria.append({'id': 'unknown', 'text': extra})
    return 'CRITERIA JSON:\n' + json.dumps(criteria, ensure_ascii=False) + '\nEND CRITERIA'


def test_analysis_uses_real_schema_and_rejects_unknown_goals(tmp_path):
    runner = fixture.EnterpriseRehearsalRunner()
    prompt = IDENTITY + '\nUSER REQUEST (data):\n' + json.dumps(fixture.FIXED_GOAL, ensure_ascii=False)
    result = runner.run(request(tmp_path, prompt=prompt, read_only=True), lambda *_: None)
    assert validate(json.loads(result.text), [])['spec_draft'] == fixture.SPEC
    with pytest.raises(ProviderError, match='仅支持'):
        runner.run(request(tmp_path, prompt=prompt.replace(fixture.FIXED_GOAL, '建立企业支付平台'), read_only=True), lambda *_: None)


@pytest.mark.parametrize('active,goal', [(False, None), (True, '不同的需求')])
def test_changed_or_unsigned_spec_cannot_be_coded(tmp_path, active, goal):
    signed_workspace(tmp_path, active=active, goal=goal)
    with pytest.raises(ProviderError, match='未签署或已变化'):
        fixture.EnterpriseRehearsalRunner().run(request(tmp_path), lambda *_: None)
    assert not (tmp_path / 'hello.py').exists()


def test_unknown_criterion_cannot_gain_a_pass(tmp_path):
    signed_workspace(tmp_path)
    (tmp_path / 'hello.py').write_text(fixture.SOURCE)
    with pytest.raises(ProviderError, match='不支持该验收条件'):
        fixture.EnterpriseRehearsalRunner().run(request(tmp_path,
            prompt=verification_prompt('还要部署到生产'), read_only=True, verification=True), lambda *_: None)


def test_tampered_source_is_not_executed_or_accepted(tmp_path):
    signed_workspace(tmp_path)
    (tmp_path / 'hello.py').write_text("from pathlib import Path; Path('should-not-execute').touch()")
    result = fixture.EnterpriseRehearsalRunner().run(request(tmp_path,
        prompt=verification_prompt(), read_only=True, verification=True), lambda *_: None)
    assert json.loads(result.text)['verdict'] == 'fail'
    assert not (tmp_path / 'should-not-execute').exists()


def test_independent_verifier_runs_real_check_and_rejects_output_mismatch(tmp_path, monkeypatch):
    signed_workspace(tmp_path)
    (tmp_path / 'hello.py').write_text(fixture.SOURCE)
    events = []
    runner = fixture.EnterpriseRehearsalRunner()
    req = request(tmp_path, prompt=verification_prompt(), read_only=True, verification=True)
    result = runner.run(req, lambda *event: events.append(event), threading.Event())
    assert json.loads(result.text)['verdict'] == 'pass'
    observed = next(payload for kind, payload in events if kind == 'check.result')
    assert observed['exit'] == 0 and observed['stdout'] == '工单服务已就绪\n'
    assert not any(kind == 'command.completed' for kind, _ in events)
    monkeypatch.setattr(fixture, 'EXPECTED_STDOUT', 'different expected output\n')
    failed = runner.run(req, lambda *_: None, threading.Event())
    assert json.loads(failed.text)['verdict'] == 'fail'


def test_real_modern_entry_analyzes_confirms_codes_verifies_and_is_idempotent(tmp_path):
    data = tmp_path / 'data'
    root = tmp_path / 'workspaces'
    store = Store(data / 'control.db')
    profiles = {role: {'provider': 'codex', 'model': 'synthetic-' + role}
                for role in ('planner', 'cheap', 'standard', 'strong')}
    service = Service(store, runner=fixture.EnterpriseRehearsalRunner(), profiles=profiles, timeout_s=60)
    service.preview_mode = True
    app = create_app(data_dir=data, workspace_root=root,
                     public_origin='http://testserver', service=service)
    app.state.auth.create_user('preview', 'factory-preview-only')
    with TestClient(app) as client:
        logged = client.post('/api/auth/login', json={'username': 'preview', 'password': 'factory-preview-only'},
                             headers={'Origin': 'http://testserver'})
        assert logged.status_code == 200
        headers = {'Origin': 'http://testserver', 'X-CSRF-Token': logged.json()['csrf_token']}
        body = {'name': fixture.FIXED_GOAL[:60], 'idempotency_key': 'modern-fixture-same-operation'}
        created = client.post('/api/v2/projects/create-workspace', json=body, headers=headers)
        assert created.status_code == 201, created.text
        project = created.json()
        assert set(project['checks']) == {'workspace-integrity'}
        payload = {'project_id': project['id'], 'request': fixture.FIXED_GOAL,
                   'operation': 'general', 'interaction_mode': 'automatic',
                   'idempotency_key': body['idempotency_key']}
        first = client.post('/api/v2/runs', json=payload, headers=headers)
        assert first.status_code in (200, 201, 202), first.text
        rid = first.json()['id']
        second = client.post('/api/v2/runs', json=payload, headers=headers)
        assert second.status_code in (200, 201, 202), second.text
        assert second.json()['id'] == rid
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            finished = store.get(rid)
            if finished['status'] in ('ready_for_review', 'failed', 'needs_human', 'needs_clarification'):
                break
            time.sleep(.05)
        assert finished['status'] == 'ready_for_review', (finished['status'], finished.get('error'), finished.get('artifacts', {}).get('verification'))
        assert finished['spec_confirmation']['automatic'] is True
        assert finished['spec_confirmation']['policy'] == 'submission'
        assert finished['artifacts']['acceptance_ledger']['complete'] is True
        assert all(row['status'] == 'pass' for row in finished['artifacts']['acceptance_ledger']['items'])
        assert finished['artifacts']['checks'][0]['name'] == 'workspace-integrity'
        assert finished['artifacts']['checks'][0]['exit'] == 0
        events = store.events(rid)
        assert {'requirement_analysis.completed', 'spec.auto_confirmed', 'policy.authorized', 'scope_declaration', 'verification.completed'} <= {e['type'] for e in events}
        ordered = ['requirement_analysis.completed', 'spec.auto_confirmed', 'policy.authorized',
                   'run.started', 'scope_declaration', 'verification.completed']
        identifiers = [next(e['id'] for e in events if e['type'] == kind) for kind in ordered]
        assert identifiers == sorted(identifiers)
        policy = next(e['payload'] for e in events if e['type'] == 'policy.authorized')
        assert policy['actor'] == 'project-policy'
        assert not any(e['type'] == 'human.approved' for e in events)
        declaration = next(e['payload'] for e in events if e['type'] == 'scope_declaration')
        assert any(item['path'] == 'hello.py' and item['accepted'] and not item['late']
                   for item in declaration['files'])
        actual = [e['payload'] for e in events if e['type'] == 'check.result' and e['task_id'] == 'verification']
        assert actual and actual[-1]['stdout'] == '工单服务已就绪\n'
        assert len([r for r in store.all_runs() if r['project_id'] == project['id']]) == 1
        assert Path(finished['artifacts']['worktree'], 'hello.py').read_text() == fixture.SOURCE
        listing = client.get('/api/v3/runs/' + rid + '/deliverables')
        assert listing.status_code == 200 and listing.json()['saved'] is True
        item = next(row for row in listing.json()['items'] if row['name'] == 'hello.py')
        download = client.get('/api/v3/runs/' + rid + '/deliverables/files/' + str(item['id']))
        assert download.status_code == 200
        assert download.text == fixture.SOURCE



def test_modern_coding_request_without_signed_spec_fails_closed(tmp_path):
    req = request(tmp_path, prompt='You are the coding owner for this project. ' + fixture.FIXED_GOAL)
    with pytest.raises(ProviderError, match='唯一已确认'):
        fixture.EnterpriseRehearsalRunner().run(req, lambda *_: None)
    assert not (tmp_path / 'hello.py').exists()
    assert not (tmp_path / 'welcome.txt').exists()


def test_unsupported_untyped_request_does_not_fall_back_to_legacy_writer(tmp_path):
    with pytest.raises(ProviderError, match='不支持该请求'):
        fixture.EnterpriseRehearsalRunner().run(request(tmp_path, prompt='Do an unrelated task'), lambda *_: None)
    assert not (tmp_path / 'welcome.txt').exists()


def test_existing_seeded_legacy_contract_still_works(tmp_path):
    (tmp_path / 'README.md').write_text('这是用于工厂界面验收的隔离仓库。', encoding='utf-8')
    (tmp_path / 'welcome.txt').write_text('欢迎使用', encoding='utf-8')
    runner = fixture.EnterpriseRehearsalRunner()
    plan = runner.run(request(tmp_path, read_only=True,
        prompt='Request: 请完善工单服务的欢迎提示，并验证交付结果。\nPrior planning history:\n(none)'), lambda *_: None)
    task = json.loads(plan.text)['tasks'][0]
    runner.run(request(tmp_path, prompt=task['prompt']), lambda *_: None)
    runner.run(request(tmp_path, prompt=task['prompt']), lambda *_: None)
    assert (tmp_path / 'welcome.txt').read_text() == '工单服务已就绪'
