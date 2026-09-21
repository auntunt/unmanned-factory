"""A manually imported issue is one task, with one baseline, told truthfully.

The failures these cover are all of the same shape: a second source of truth.
A repeat import that opens a second workspace, a receipt whose baseline comes
from the registry that wrote it, a status the maintenance surface invented
because the execution said something it did not recognise, a revision that
overwrites the evidence of the one before it.

The execution port here is a deliberate fake, but it is a *counting* fake: it
records every dispatch and every workspace it was asked to create, so "no repeat
payment, no second workspace" is a measured claim rather than a described one.
"""
import json
import subprocess

import pytest

from factory.control import issue_maintenance as im
from factory.control.issue_maintenance import MaintenanceTasks
from factory.control.store import Conflict, Store

ACTOR = {'id': 7, 'username': 'operator'}
BASE = 'a' * 40


def _seed_project(store, pid='p1'):
    """A real row in the store's own ``projects`` table for ``pid``.

    The fake ``_Repository`` below never touches the real store, so a task's
    ``project_id`` here is otherwise a label nothing backs. Project-memory
    reads and writes (``scenario_memory``, reached directly off ``store``) go
    through the real ``projects`` table regardless of which repository port a
    test wired in, so they need this to exist -- exactly as it always does in
    production, where ``WebuddyRepository.resolve`` already required the
    project to exist before a task could ever reach ``export``.
    """
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO projects VALUES (?,?)',
                   (pid, json.dumps({'id': pid, 'revision': 1})))


def _issue(**over):
    return {'source': 'manual', 'external_id': '1024', 'version': '3',
            'title': '导出报表偶发缺行', 'body': '选择跨月区间时最后一行丢失。', **over}


def _request(**over):
    return {'issue': _issue(**over.pop('issue', {})), 'project_id': 'p1',
            'repository': 'acme/legacy', 'base_sha': BASE,
            'base_branch_label': 'release/2.1',
            'expected_behaviour': '跨月区间导出行数与查询结果一致',
            'delivery_goal': '提供补丁与回执',
            'agreement': {'revision': 'v4', 'skill_version': 'issue-maintenance@2'},
            'idempotency_key': 'import-1024-a', **over}


class _Identity:
    def __init__(self):
        self.refusals = set()

    def require(self, actor, project_id):
        if not isinstance(actor, dict) or not actor.get('id'):
            raise Conflict('需要受控身份')
        if (actor['id'], project_id) in self.refusals:
            raise Conflict('无权访问该项目')


class _Repository:
    def __init__(self, *, base=BASE, repository='acme/legacy'):
        self.known, self.repository = {base}, repository
        self.calls = []

    def resolve(self, project_id, repository, base_sha):
        self.calls.append((project_id, repository, base_sha))
        if repository != self.repository:
            raise ValueError('仓库不匹配')
        if base_sha not in self.known:
            raise ValueError('基线不存在')
        return {'project_id': project_id, 'repository': repository, 'base_sha': base_sha}


class _Execution:
    """Counts what actually happened: dispatches, workspaces, paid calls."""

    def __init__(self):
        self.dispatches, self.workspaces, self.paid_calls = [], [], []
        self.runs = {}

    def submit(self, record, *, actor):
        eid = f'exec-{len(self.runs) + 1}'
        self.dispatches.append(record['id'])
        self.workspaces.append(eid)
        self.paid_calls.append(eid)
        self.runs[eid] = {'state': 'received', 'events': [], 'cost': 0.0,
                          'delivery': None, 'base_sha': record['base_sha'],
                          'repository': record['repository'],
                          'interventions': [], 'resumes': [], 'cancels': []}
        return eid

    # -- test-side drivers -------------------------------------------------
    def advance(self, eid, state, *, event=None, payload=None):
        run = self.runs[eid]
        run['state'] = state
        if event:
            run['events'].append({'sequence': len(run['events']) + 1, 'kind': event,
                                  'payload': payload or {}})
        return run

    def deliver(self, eid, **over):
        self.runs[eid]['state'] = 'ready_for_review'
        self.runs[eid]['delivery'] = {
            'commit': 'c' * 40, 'checks': [{'name': 'local', 'passed': True, 'exit_code': 0}],
            'unverified': ['真实服务器未验证'],
            'working_copy_base_sha': self.runs[eid]['base_sha'],
            'repository': self.runs[eid]['repository'],
            'capability_source': 'platform_executor', 'synthetic': True, **over}
        return self.runs[eid]['delivery']

    # -- port --------------------------------------------------------------
    def status(self, eid):
        return self.runs[eid]['state']

    def events(self, eid, after=0):
        return [e for e in self.runs[eid]['events'] if e['sequence'] > after]

    def cost_usd(self, eid):
        return self.runs[eid]['cost']

    def delivery(self, eid):
        return self.runs[eid]['delivery']

    def export_artifacts(self, eid):
        return {'diff_hash': 'd' * 64,
                'artifacts': [{'name': f'{eid}.patch', 'bytes': b'--- patch ---\n'}]}

    def intervene(self, eid, text, *, actor):
        self.runs[eid]['interventions'].append(text)

    def resume(self, eid, *, actor):
        self.runs[eid]['resumes'].append(actor['username'])

    def cancel(self, eid, *, actor):
        self.runs[eid]['cancels'].append(actor['username'])
        self.runs[eid]['state'] = 'cancelled'


@pytest.fixture
def tasks(tmp_path):
    store = Store(tmp_path / 'control.db')
    _seed_project(store)
    execution = _Execution()
    port = MaintenanceTasks(store, execution=execution,
                            repository=_Repository(), identity=_Identity())
    port.fake = execution
    return port


def test_the_same_key_and_content_returns_the_original_task_without_paying_again(tasks):
    first = tasks.create(_request(), actor=ACTOR)
    again = tasks.create(_request(), actor=ACTOR)
    assert again['task_id'] == first['task_id']
    assert again['execution_id'] == first['execution_id']
    assert tasks.fake.dispatches == [first['task_id']], '重复导入不应再派发'
    assert tasks.fake.workspaces == [first['execution_id']], '重复导入不应开第二个工作区'
    assert tasks.fake.paid_calls == [first['execution_id']]


def test_the_same_key_with_changed_content_is_a_conflict(tasks):
    tasks.create(_request(), actor=ACTOR)
    with pytest.raises(Conflict, match='内容发生变化'):
        tasks.create(_request(expected_behaviour='顺带把导出改成 CSV'), actor=ACTOR)
    assert len(tasks.fake.dispatches) == 1


def test_a_changed_issue_body_under_the_same_key_is_also_a_conflict(tasks):
    """The issue text is part of the submission, not a label on it."""
    tasks.create(_request(), actor=ACTOR)
    with pytest.raises(Conflict, match='内容发生变化'):
        tasks.create(_request(issue={'body': '其实是权限问题，请改鉴权。'}), actor=ACTOR)


def test_an_unresolvable_baseline_is_refused_before_any_dispatch(tasks):
    with pytest.raises(ValueError, match='基线不存在'):
        tasks.create(_request(base_sha='b' * 40), actor=ACTOR)
    assert tasks.fake.dispatches == []
    assert tasks.fake.workspaces == []


def test_a_short_sha_is_refused_even_when_it_names_a_real_commit(tasks):
    """A branch name or an abbreviation cannot stand in for the pin."""
    with pytest.raises(ValueError, match='完整的 40 位基线 commit SHA'):
        tasks.create(_request(base_sha=BASE[:12]), actor=ACTOR)
    with pytest.raises(ValueError, match='完整的 40 位基线 commit SHA'):
        tasks.create(_request(base_sha='release/2.1'), actor=ACTOR)
    assert tasks.fake.dispatches == []


def test_a_refused_baseline_can_be_resubmitted_under_the_same_key(tasks):
    """The key is not burned by a rejection: a corrected retry is the same import."""
    with pytest.raises(ValueError):
        tasks.create(_request(base_sha='b' * 40), actor=ACTOR)
    created = tasks.create(_request(), actor=ACTOR)
    assert created['status'] == 'received'
    assert tasks.fake.dispatches == [created['task_id']]


def _deliver_and_export(tasks, view):
    tasks.fake.deliver(view['execution_id'])
    return tasks.export(view['task_id'], actor=ACTOR)


def test_a_reopened_issue_forms_a_successor_and_keeps_the_old_receipt(tasks):
    first = tasks.create(_request(), actor=ACTOR)
    original = _deliver_and_export(tasks, first)['receipt']

    second = tasks.revise(first['task_id'], _request(
        idempotency_key='import-1024-b', issue={'version': '4',
        'body': '跨月区间仍然丢行，另外季度区间也丢。'}), actor=ACTOR)
    assert second['task_id'] != first['task_id']
    assert second['revision'] == 2
    assert second['predecessor_id'] == first['task_id']
    assert tasks.get(first['task_id'], actor=ACTOR)['successor_id'] == second['task_id']

    # The predecessor's evidence is untouched, and the successor has its own.
    kept = tasks.get(first['task_id'], actor=ACTOR)['receipts']
    assert [r['issue']['version'] for r in kept] == ['3']
    assert kept[0]['delivery'] == original['delivery']
    assert tasks.get(second['task_id'], actor=ACTOR)['receipts'] == []
    successor = _deliver_and_export(tasks, second)['receipt']
    assert successor['issue']['version'] == '4'
    assert tasks.get(first['task_id'], actor=ACTOR)['receipts'] == kept, \
        '后继出回执不应改动前一修订的证据'


def test_a_receipt_is_append_only_at_the_database_level(tasks):
    """Not "the code does not overwrite it" -- the table refuses."""
    view = tasks.create(_request(), actor=ACTOR)
    _deliver_and_export(tasks, view)
    with tasks.records.store.connect() as db:
        with pytest.raises(Exception, match='append-only'):
            db.execute("UPDATE maintenance_receipts SET data='{}' WHERE task_id=?",
                       (view['task_id'],))
        with pytest.raises(Exception, match='append-only'):
            db.execute('DELETE FROM maintenance_receipts WHERE task_id=?',
                       (view['task_id'],))


def test_exporting_twice_returns_the_first_receipt(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    first = _deliver_and_export(tasks, view)['receipt']
    tasks.fake.deliver(view['execution_id'], commit='e' * 40)
    second = tasks.export(view['task_id'], actor=ACTOR)['receipt']
    assert second['delivery']['commit'] == first['delivery']['commit']


def test_a_revision_with_identical_content_is_refused(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    with pytest.raises(Conflict, match='内容完全相同'):
        tasks.revise(view['task_id'], _request(idempotency_key='import-1024-c'),
                     actor=ACTOR)


def test_the_receipt_refuses_a_baseline_the_execution_did_not_actually_use(tasks):
    """The registry's pin and the working copy's baseline are two claims, compared."""
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.deliver(view['execution_id'], working_copy_base_sha='f' * 40)
    with pytest.raises(Conflict, match='基线不一致'):
        tasks.export(view['task_id'], actor=ACTOR)
    assert tasks.get(view['task_id'], actor=ACTOR)['receipts'] == []


def test_the_receipt_refuses_a_working_copy_with_no_confirmable_baseline(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.deliver(view['execution_id'], working_copy_base_sha=None)
    with pytest.raises(Conflict, match='基线不一致'):
        tasks.export(view['task_id'], actor=ACTOR)


def test_the_receipt_refuses_a_repository_the_execution_did_not_use(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.deliver(view['execution_id'], repository='acme/other')
    with pytest.raises(Conflict, match='仓库与登记的仓库不一致'):
        tasks.export(view['task_id'], actor=ACTOR)


@pytest.mark.parametrize('execution_state,expected', [
    ('received', 'received'), ('queued', 'running'), ('planning', 'running'),
    ('running', 'running'), ('verifying', 'running'),
    ('needs_human', 'waiting'), ('awaiting_approval', 'waiting'),
    ('ready_for_review', 'delivered'), ('published', 'delivered'),
    ('failed', 'failed'), ('cancelled', 'cancelled'), ('discarded', 'cancelled'),
])
def test_every_execution_state_maps_to_one_named_task_state(tasks, execution_state, expected):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], execution_state)
    assert tasks.get(view['task_id'], actor=ACTOR)['status'] == expected


def test_an_unknown_execution_state_is_refused_rather_than_guessed(tasks):
    """A maintenance surface must not invent a state for an upstream value."""
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], 'reticulating_splines')
    with pytest.raises(Conflict, match='无法映射到维护任务状态'):
        tasks.get(view['task_id'], actor=ACTOR)


def test_a_waiting_task_reports_why_with_the_event_that_says_so(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], 'running', event='run.started')
    tasks.fake.advance(view['execution_id'], 'needs_human',
                       event='clarification.requested',
                       payload={'question': '跨月区间的定义以财务口径为准吗？'})
    blocking = tasks.get(view['task_id'], actor=ACTOR)['blocking_reason']
    assert blocking['kind'] == 'clarification.requested'
    assert '财务口径' in blocking['message']
    assert blocking['event'] == {'execution_id': view['execution_id'], 'sequence': 2}


def test_a_waiting_task_with_no_reason_event_says_so_instead_of_showing_blank(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], 'needs_human', event='task.completed')
    blocking = tasks.get(view['task_id'], actor=ACTOR)['blocking_reason']
    assert blocking['kind'] == 'unknown'
    assert blocking['event'] is None
    assert blocking['message']


def test_a_running_task_has_no_blocking_reason(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], 'running', event='run.started')
    assert tasks.get(view['task_id'], actor=ACTOR)['blocking_reason'] is None


def test_cancel_records_the_intent_even_if_the_execution_call_fails(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], 'running')

    def refuse(eid, *, actor):
        raise Conflict('成果正在完成原子归档，本次取消未生效')
    tasks.fake.cancel = refuse
    with pytest.raises(Conflict):
        tasks.cancel(view['task_id'], actor=ACTOR)
    # The intent survives, so a reopened process still knows a human asked.
    assert tasks.records.get(view['task_id'])['cancel_requested'] is True
    assert tasks.get(view['task_id'], actor=ACTOR)['status'] == 'cancelling'


def test_supplementing_a_running_task_is_refused_by_the_execution_port(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], 'needs_human')
    tasks.intervene(view['task_id'], '以财务口径为准', actor=ACTOR)
    assert tasks.fake.runs[view['execution_id']]['interventions'] == ['以财务口径为准']
    tasks.resume(view['task_id'], actor=ACTOR)
    assert tasks.fake.runs[view['execution_id']]['resumes'] == ['operator']
    assert len(tasks.fake.dispatches) == 1, '补充与恢复都不应产生新的派发'


def test_reopening_the_store_finds_the_same_task_with_its_evidence_intact(tmp_path):
    """A new MaintenanceTasks over the same database is the reopened-process case."""
    db = tmp_path / 'control.db'
    store = Store(db)
    _seed_project(store)
    execution = _Execution()
    first = MaintenanceTasks(store, execution=execution,
                             repository=_Repository(), identity=_Identity())
    view = first.create(_request(), actor=ACTOR)
    execution.runs[view['execution_id']]['cost'] = 1.25
    execution.deliver(view['execution_id'])
    first.export(view['task_id'], actor=ACTOR)
    first.cancel(view['task_id'], actor=ACTOR)

    reopened = MaintenanceTasks(Store(db), execution=execution,
                                repository=_Repository(), identity=_Identity())
    found = reopened.get(view['task_id'], actor=ACTOR)
    assert found['task_id'] == view['task_id']
    assert found['baseline']['base_sha'] == BASE
    assert found['cost_usd'] == 1.25
    assert found['receipts'][0]['delivery']['commit'] == 'c' * 40
    assert reopened.records.get(view['task_id'])['cancel_requested'] is True
    assert execution.dispatches == [view['task_id']], '重开进程不应产生额外派发'
    # Same key, new process: still the original task, still one dispatch.
    again = reopened.create(_request(), actor=ACTOR)
    assert again['task_id'] == view['task_id']
    assert execution.dispatches == [view['task_id']]


def test_a_task_cannot_be_read_without_project_authorization(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.identity.refusals.add((9, 'p1'))
    with pytest.raises(Conflict, match='无权访问该项目'):
        tasks.get(view['task_id'], actor={'id': 9, 'username': 'other'})


def test_an_unresolved_actor_is_not_an_actor(tasks):
    for actor in ({'username': 'admin'}, 'admin', None, {}):
        with pytest.raises(Conflict, match='需要受控身份'):
            tasks.create(_request(), actor=actor)
    assert tasks.fake.dispatches == []


def test_the_submitted_content_of_a_task_cannot_be_edited(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    with pytest.raises(ValueError, match='只允许补记'):
        tasks.records.link(view['task_id'], {'base_sha': 'f' * 40})


def test_an_execution_cannot_be_rebound_to_another_run(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    with pytest.raises(Conflict, match='不能改绑'):
        tasks.records.link(view['task_id'], {'execution_id': 'exec-999'})
    # Re-recording the same execution id is not a rebind.
    assert tasks.records.link(view['task_id'],
                              {'execution_id': view['execution_id']})['execution_id'] == view['execution_id']


def _state(view, step):
    return next(s['state'] for s in view['steps'] if s['step'] == step)


def test_the_step_list_is_the_sop_and_advances_only_on_reported_evidence(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    assert [s['step'] for s in view['steps']] == list(MaintenanceTasks.STEPS)
    assert _state(view, 'intake') == 'current'
    assert _state(view, 'deliver') == 'pending'

    tasks.fake.advance(view['execution_id'], 'needs_human')
    waiting = tasks.get(view['task_id'], actor=ACTOR)
    assert _state(waiting, 'triage') == 'blocked', '等人回答不能画成正在进行'
    assert _state(waiting, 'modify') == 'pending', '停在澄清的任务没有在修改'

    tasks.fake.deliver(view['execution_id'])
    delivered = tasks.get(view['task_id'], actor=ACTOR)
    assert _state(delivered, 'deliver') == 'done'
    assert _state(delivered, 'receipt') == 'current'


def test_a_failed_task_stops_at_the_step_it_stopped_on(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], 'failed', event='run.failed',
                       payload={'message': '本地检查未通过'})
    failed = tasks.get(view['task_id'], actor=ACTOR)
    assert _state(failed, 'modify') == 'stopped'
    assert _state(failed, 'verify') == 'pending'
    assert failed['blocking_reason']['message'] == '本地检查未通过'


def test_the_human_readable_receipt_carries_every_required_field(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.deliver(view['execution_id'])
    exported = tasks.export(view['task_id'], actor=ACTOR)
    text = exported['text']
    # Every one of these is a field the customer-facing receipt must state.
    assert 'manual#1024 v3' in text
    assert BASE in text and 'release/2.1' in text
    assert 'c' * 40 in text and 'd' * 64 in text
    assert 'v4' in text and 'issue-maintenance@2' in text
    assert 'local：通过（退出码 0）' in text
    assert '真实服务器未验证' in text
    assert '交包待发布，本次未部署到任何环境' in text
    assert 'platform_executor' in text
    assert '合成示例仓库' in text, '合成来源必须显式标注，不得暗示客户成功'
    assert exported['artifacts'][0]['name'].endswith('.patch')


def test_the_receipt_states_a_deployed_tier_only_when_that_tier_was_requested(tasks):
    view = tasks.create(_request(idempotency_key='import-1024-t',
                                delivery_tier='authorized_test_project'), actor=ACTOR)
    tasks.fake.deliver(view['execution_id'], synthetic=False)
    text = tasks.export(view['task_id'], actor=ACTOR)['text']
    assert '已部署到明确授权的公司测试项目' in text
    assert '合成示例仓库' not in text


def test_an_unlisted_delivery_tier_is_refused(tasks):
    with pytest.raises(ValueError, match='交付层级'):
        tasks.create(_request(delivery_tier='customer_production'), actor=ACTOR)


def test_exporting_before_delivery_is_refused(tasks):
    view = tasks.create(_request(), actor=ACTOR)
    tasks.fake.advance(view['execution_id'], 'running')
    with pytest.raises(Conflict, match='还没有可交付的结果'):
        tasks.export(view['task_id'], actor=ACTOR)


def test_an_import_declared_synthetic_keeps_the_label_even_if_the_run_forgets_it(tasks):
    """The disclaimer must not depend on the execution side remembering to report it.

    Either side saying "synthetic" carries it; losing the label needs both to be
    silent. The failure direction is an unnecessary disclaimer, never a demo that
    reads as a customer success.
    """
    view = tasks.create(_request(idempotency_key='import-1024-s', synthetic=True),
                        actor=ACTOR)
    tasks.fake.deliver(view['execution_id'], synthetic=False)
    exported = tasks.export(view['task_id'], actor=ACTOR)
    assert exported['receipt']['synthetic'] is True
    assert '合成示例仓库' in exported['text']


def test_declaring_a_task_synthetic_is_a_different_submission(tasks):
    """The label is part of what was submitted, so it cannot be flipped in place."""
    real = tasks.create(_request(idempotency_key='import-1024-r'), actor=ACTOR)
    assert real['synthetic'] is False
    with pytest.raises(Conflict, match='内容发生变化'):
        tasks.create(_request(idempotency_key='import-1024-r', synthetic=True),
                     actor=ACTOR)


def test_a_reused_check_result_says_so_and_names_the_evidence_it_reuses(tasks):
    """证据适用性: a carried-forward pass must not read as freshly earned.

    M2 already decides reuse per check and records the identity a saved result is
    evidence for. The receipt's job is to not hide that: a reader who cannot tell
    "ran this round" from "reused from an earlier one" cannot audit the delivery.
    """
    view = tasks.create(_request(idempotency_key='import-1024-v'), actor=ACTOR)
    tasks.fake.deliver(view['execution_id'], checks=[
        {'name': 'unit', 'passed': True, 'exit_code': 0, 'reused': False},
        {'name': 'lint', 'passed': True, 'exit_code': 0, 'reused': True,
         'identity_fingerprint': 'f' * 64},
        {'name': 'e2e', 'passed': True, 'exit_code': 0, 'reused': True},
    ])
    exported = tasks.export(view['task_id'], actor=ACTOR)
    by_name = {check['name']: check for check in exported['receipt']['checks']}
    assert by_name['unit']['reused'] is False
    assert by_name['lint']['identity_fingerprint'] == 'f' * 64
    text = exported['text']
    assert 'unit：通过（退出码 0），本轮实际运行' in text
    assert 'lint：通过（退出码 0），复用既有结果，证据身份 ffffffffffff' in text
    # A reuse with no recorded identity is the one that must not read as covered.
    assert 'e2e：通过（退出码 0），复用既有结果，但没有记录证据身份，适用范围无法核对' in text
