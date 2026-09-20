"""One bounded reformat of a structurally invalid verdict, and nothing more."""
import json
import threading
import time

import pytest

from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult
from factory.control.verification import (_repaired_matches_original, _repair_verdict_format,
                                          _stated_structure)
from tests.review_helpers import passing_review
from tests.test_control_app import app_env, login, project


def _review(app_env, provider='claude'):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '格式损坏的验收报告')
    run['agent_snapshot'] = {}
    service.cancels[run['id']] = threading.Event()
    cfg = service.runtime_settings.get()
    cfg['agent_verification_profile'] = {'provider': provider, 'model': 'review'}
    return client, store, service, repo, p, run, cfg


def _repairs(store, rid):
    return [e for e in store.events(rid) if e['type'] == 'verification.format_repaired']


def _rows(request, status='fail', evidence='greeting 未更新'):
    """Per-criterion rows the reviewer really wrote, for the criteria it was given."""
    criteria = json.loads(request.prompt.split('CRITERIA JSON:\n', 1)[1].split('\nEND CRITERIA', 1)[0])
    return [{'id': item['id'], 'status': status, 'evidence': evidence} for item in criteria]


def _damaged(verdict, rows, reason):
    """A report whose content is complete but whose JSON envelope is broken."""
    body = json.dumps({'verdict': verdict, 'reason': reason, 'criteria': rows}, ensure_ascii=False)
    assert body.endswith('}')
    return body[:-1] + ',,'  # Unparseable, and nothing was removed.


# Same shape, for the guards that must stop a repair that would otherwise happen.
_REPAIRABLE = _damaged('fail', [{'id': 'request:1', 'status': 'fail', 'evidence': 'greeting 未更新'}],
                       'greeting 未更新')


def test_one_reformat_recovers_a_clear_verdict_without_reviewing_again(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env)
    calls, rows = [], []

    def respond(request, emit, cancel=None):
        calls.append(request)
        if len(calls) == 1:
            rows.extend(_rows(request))
            return ProviderResult(_damaged('fail', rows, 'greeting 未更新'), cost_usd=0.03)
        assert 'reformatter' in request.prompt and 'NOT a reviewer' in request.prompt
        return ProviderResult(json.dumps({'verdict': 'fail', 'reason': 'greeting 未更新',
                                          'criteria': rows}, ensure_ascii=False), cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo), 'checks': [{'name': 'check', 'exit': 0}]}
    with pytest.raises(ExecutionError, match='独立验证未通过'):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    # The conclusion stays fail, and it is a real verdict, not a parse failure.
    assert artifacts['verification']['verdict'] == 'fail'
    assert artifacts['verification'].get('error_type') != 'invalid_response'
    repair = artifacts['verification_format_repair']
    assert repair['accepted'] and repair['tools_executed'] == 0
    assert repair['stated_verdict'] == 'fail'
    # Same snapshot, same contract version: not a second review.
    assert repair['commit'] == artifacts['verification_commit']
    assert repair['template_version'] == artifacts['verification_template_version'] == 1
    assert len(calls) == 2
    reformat = calls[1]
    assert reformat.tools_disabled and reformat.read_only
    assert reformat.session_id is None and reformat.reference_mount is None
    assert reformat.verification is False and reformat.timeout_s <= 120
    assert len(_repairs(store, run['id'])) == 1
    # The repair is billed like any other call.
    assert service._usage(run['id'], profile='verification_format_repair') == {
        'known_cost_usd': 0.01, 'unknown_cost_calls': 0, 'calls': 1}


def test_a_reformat_that_changes_the_conclusion_is_refused(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env)
    calls, rows = [], []

    def respond(request, emit, cancel=None):
        calls.append(request)
        if len(calls) == 1:
            rows.extend(_rows(request))
            return ProviderResult(_damaged('fail', rows, '两项缺证据'), cost_usd=0.03)
        return ProviderResult(json.dumps({'verdict': 'pass', 'reason': '看起来没问题',
                                          'criteria': [{**row, 'status': 'pass'} for row in rows]},
                                         ensure_ascii=False), cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError, match='未返回有效结构化结果'):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert artifacts['verification'] == {'verdict': 'fail', 'reason': '独立验证模型未返回有效 verdict',
                                         'error_type': 'invalid_response'}
    assert artifacts['verification_format_repair']['accepted'] is False
    assert artifacts['verification_format_repair']['refused'] == '重排改变了原结论'
    assert _repairs(store, run['id']) == []


def test_a_reformat_that_rewrites_the_evidence_is_refused(app_env):
    """Same conclusion, invented evidence: still a new judgement, not a reformat."""
    client, store, service, repo, p, run, cfg = _review(app_env)
    calls, rows = [], []

    def respond(request, emit, cancel=None):
        calls.append(request)
        if len(calls) == 1:
            rows.extend(_rows(request))
            return ProviderResult(_damaged('fail', rows, 'greeting 未更新'), cost_usd=0.03)
        rewritten = [{**row, 'evidence': 'INVENTED evidence absent from original'} for row in rows]
        return ProviderResult(json.dumps({'verdict': 'fail', 'reason': 'greeting 未更新',
                                          'criteria': rewritten}, ensure_ascii=False), cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError, match='未返回有效结构化结果'):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert artifacts['verification']['error_type'] == 'invalid_response'
    assert artifacts['verification_format_repair']['accepted'] is False
    assert artifacts['verification_format_repair']['refused'] == '重排改变了原结论'
    assert _repairs(store, run['id']) == []


def test_a_repaired_pass_never_enters_the_tool_equipped_coverage_loop(app_env):
    """A format-only repair must not be followed by a fresh evidence-gathering call."""
    client, store, service, repo, p, run, cfg = _review(app_env)
    run['agent_snapshot'] = {'acceptance': ['第一条验收', '第二条验收']}
    calls, rows = [], []

    def respond(request, emit, cancel=None):
        calls.append(request)
        if len(calls) == 1:
            # One real row out of two criteria: a pass here is under-covered.
            rows.append(_rows(request, status='pass', evidence='看过了')[0])
            return ProviderResult(_damaged('pass', rows, '看过了'), cost_usd=0.03)
        return ProviderResult(json.dumps({'verdict': 'pass', 'reason': '看过了',
                                          'criteria': rows}, ensure_ascii=False), cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo), 'checks': [{'name': 'check', 'exit': 0}]}
    with pytest.raises(ExecutionError, match='独立验证未通过'):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert artifacts['verification']['verdict'] == 'fail'
    assert artifacts['verification']['error_type'] == 'incomplete_coverage'
    # The review call, the reformat, and nothing else: no coverage top-up round.
    assert len(calls) == 2
    assert [e for e in store.events(run['id']) if e['type'] == 'verification.coverage_retry'] == []


def test_only_a_structurally_complete_report_is_repairable():
    rows = [{'id': 'request:1', 'status': 'fail', 'evidence': 'greeting 未更新'}]
    stated = _stated_structure(_damaged('fail', rows, 'greeting 未更新'))
    assert stated == {'verdict': 'fail', 'rows': {'request:1': ('fail', 'greeting 未更新')}}
    # Prose is not structure: no verdict field, whatever words it contains.
    assert _stated_structure('I cannot pass this change. Required evidence is unavailable.') is None
    assert _stated_structure('Overall: pass. No per-criterion evidence has been collected.') is None
    # A conclusion with no per-criterion evidence cannot be completed for it.
    assert _stated_structure(_damaged('pass', [], 'all good')) is None
    assert _stated_structure(_damaged('pass', [{'id': 'request:1', 'status': 'pass',
                                                'evidence': '   '}], 'all good')) is None
    # Two objects, or two verdict fields, are conflicting rather than damaged.
    a = json.dumps({'verdict': 'pass', 'reason': 'first', 'criteria': rows})
    b = json.dumps({'verdict': 'pass', 'reason': 'second', 'criteria': rows})
    assert _stated_structure(f'```json\n{a}\n```\n```json\n{b}\n```') is None
    # A duplicated criterion id is self-conflicting; an unknown status is not a row.
    assert _stated_structure(_damaged('fail', rows + rows, 'twice')) is None
    assert _stated_structure(_damaged('fail', [{'id': 'request:1', 'status': 'ok',
                                               'evidence': 'x'}], 'bad status')) is None


def test_an_incomplete_entry_makes_the_report_unrepairable_not_shorter():
    """A row that is itself damaged must be seen, not passed over.

    Scraping well-formed fragments would find only the healthy rows, so a
    reformatter could drop the damaged one and still reproduce everything we
    thought the report stated. The whole recovered structure is the boundary.
    """
    good = {'id': 'request:1', 'status': 'pass', 'evidence': 'observed evidence'}
    # Same id, no evidence, opposite status: the report contradicts itself.
    assert _stated_structure(_damaged('pass', [good, {'id': 'request:1', 'status': 'fail'}],
                                      'ok')) is None
    # A different id with no evidence is still a row we cannot preserve.
    assert _stated_structure(_damaged('pass', [good, {'id': 'agent:1', 'status': 'fail'}],
                                      'ok')) is None
    # Neither is an entry with no status, or one that is not an object at all.
    assert _stated_structure(_damaged('pass', [good, {'id': 'agent:1', 'evidence': 'x'}],
                                      'ok')) is None
    assert _stated_structure(_damaged('pass', [good, 'agent:1 failed'], 'ok')) is None
    # The bounded syntax recovery only undoes stray commas and unclosed brackets:
    # a truncated string, or damage inside a value, stays out of scope.
    body = json.dumps({'verdict': 'pass', 'reason': 'ok', 'criteria': [good]})
    assert _stated_structure(body[:-1]) == {'verdict': 'pass',
                                            'rows': {'request:1': ('pass', 'observed evidence')}}
    assert _stated_structure(body[:body.index('observed') + 4]) is None


def test_a_repaired_pass_never_enters_the_browser_receipt_correction(app_env):
    """The other follow-up path a repaired verdict must not reopen.

    A reformatter cannot be asked to re-cite browser observations either: it has
    no tools, never saw the page, and is forbidden to judge. A wrong citation on
    a repaired report is a fail, not a second bounded review.
    """
    client, store, service, repo, p, run, cfg = _review(app_env)
    store.append(run['id'], 'browser.observed', {
        'ok': True, 'action': 'open', 'url': 'http://127.0.0.1:8080/', 'errors': []}, 'coding')
    calls, rows = [], []

    def respond(request, emit, cancel=None):
        calls.append(request)
        if len(calls) == 1:
            verdict = json.loads(passing_review(request, '看过了'))
            # The live defect shape: a pass whose browser_review cites nothing.
            verdict.pop('browser_review', None)
            rows.extend(verdict['criteria'])
            body = json.dumps(verdict, ensure_ascii=False)
            return ProviderResult(body[:-1] + ',,', cost_usd=0.03)
        return ProviderResult(json.dumps({'verdict': 'pass', 'reason': '看过了',
                                          'criteria': rows}, ensure_ascii=False), cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo), 'checks': [{'name': 'check', 'exit': 0}]}
    with pytest.raises(ExecutionError):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert artifacts['verification_format_repair']['accepted'] is True
    assert artifacts['verification']['verdict'] != 'pass', artifacts['verification']
    # The review call, the reformat, and nothing else.
    assert len(calls) == 2
    assert [e for e in store.events(run['id'])
            if e['type'] == 'verification.browser_review_retry'] == []


def test_a_repair_must_reproduce_every_row_byte_for_byte():
    stated = {'verdict': 'fail', 'rows': {'request:1': ('fail', 'greeting 未更新')}}
    assert _repaired_matches_original(stated, {
        'verdict': 'fail', 'reason': 'x',
        'criteria': [{'id': 'request:1', 'status': 'fail', 'evidence': 'greeting 未更新'}]})
    # Upgraded status, rewritten evidence, an invented row, or a dropped row.
    for rows in ([{'id': 'request:1', 'status': 'pass', 'evidence': 'greeting 未更新'}],
                 [{'id': 'request:1', 'status': 'fail', 'evidence': 'INVENTED evidence'}],
                 [{'id': 'request:1', 'status': 'fail', 'evidence': 'greeting 未更新'},
                  {'id': 'agent:1', 'status': 'pass', 'evidence': 'INVENTED'}],
                 []):
        assert not _repaired_matches_original(stated, {'verdict': 'fail', 'reason': 'x',
                                                      'criteria': rows})
    # A different conclusion, and a missing criteria list.
    assert not _repaired_matches_original(stated, {
        'verdict': 'pass', 'reason': 'x',
        'criteria': [{'id': 'request:1', 'status': 'fail', 'evidence': 'greeting 未更新'}]})
    assert not _repaired_matches_original(stated, {'verdict': 'fail', 'reason': 'x'})


def test_conflicting_or_absent_conclusions_are_not_reformatted(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env)
    for text in ('Some of it is a pass and some of it is a fail.', 'I am still thinking about it.'):
        calls = []

        def respond(request, emit, cancel=None):
            calls.append(request)
            return ProviderResult(text, cost_usd=0.03)

        service.runner.run = respond
        artifacts = {'worktree': str(repo)}
        with pytest.raises(ExecutionError, match='未返回有效结构化结果'):
            service._independent_verify(run['id'], run, p, cfg, artifacts)
        assert artifacts['verification']['error_type'] == 'invalid_response'
        # No second call at all: there is nothing unambiguous to reformat.
        assert len(calls) == 1
        assert 'verification_format_repair' not in artifacts


def test_a_provider_without_a_tools_free_channel_is_not_asked_to_reformat(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env, provider='codex')
    calls = []

    def respond(request, emit, cancel=None):
        calls.append(request)
        # Repairable content: only the provider channel stops this one.
        return ProviderResult(_damaged('fail', _rows(request), 'greeting 未更新'), cost_usd=0.03)

    service.runner.run = respond
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError, match='未返回有效结构化结果'):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert len(calls) == 1 and 'verification_format_repair' not in artifacts


def test_repair_happens_at_most_once_per_verification(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env)
    artifacts = {'worktree': str(repo), 'verification_commit': 'snapshot',
                 'verification_template_version': 1,
                 'verification_format_repair': {'attempted': True, 'accepted': False}}

    def respond(request, emit, cancel=None):
        pytest.fail('a second reformat in the same verification must not be dispatched')

    service.runner.run = respond
    deadline = time.monotonic() + 600
    assert _repair_verdict_format(service, run['id'], run, p, artifacts,
                                  cfg['agent_verification_profile'], str(repo),
                                  _REPAIRABLE, deadline) is None
    assert artifacts['verification_format_repair'] == {'attempted': True, 'accepted': False}


def test_no_reformat_call_once_the_money_is_gone(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env)
    store.append(run['id'], 'usage.recorded', {'profile': 'verification',
                                               'cost_usd': p['budget_usd']})

    def respond(request, emit, cancel=None):
        pytest.fail('an exhausted budget must stop the reformat before dispatch')

    service.runner.run = respond
    artifacts = {'worktree': str(repo), 'verification_commit': 'snapshot'}
    assert _repair_verdict_format(service, run['id'], run, p, artifacts,
                                  cfg['agent_verification_profile'], str(repo),
                                  _REPAIRABLE, time.monotonic() + 600) is None
    assert 'verification_format_repair' not in artifacts
    assert service._usage(run['id'], profile='verification_format_repair')['calls'] == 0


def test_a_cancelled_or_expired_review_does_not_reformat(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env)

    def respond(request, emit, cancel=None):
        pytest.fail('no reformat after cancel or deadline')

    service.runner.run = respond
    artifacts = {'worktree': str(repo)}
    # Out of time.
    assert _repair_verdict_format(service, run['id'], run, p, artifacts,
                                  cfg['agent_verification_profile'], str(repo),
                                  _REPAIRABLE, time.monotonic() + 1) is None
    # Cancelled.
    service.cancels[run['id']].set()
    assert _repair_verdict_format(service, run['id'], run, p, artifacts,
                                  cfg['agent_verification_profile'], str(repo),
                                  _REPAIRABLE, time.monotonic() + 600) is None
    assert 'verification_format_repair' not in artifacts


def test_a_valid_envelope_is_never_sent_to_the_reformatter(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env)
    calls = []

    def respond(request, emit, cancel=None):
        calls.append(request)
        return ProviderResult(passing_review(request, 'observed evidence'), cost_usd=0.03)

    service.runner.run = respond
    artifacts = {'worktree': str(repo), 'checks': [{'name': 'check', 'exit': 0}]}
    service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert artifacts['verification']['verdict'] == 'pass'
    assert len(calls) == 1 and 'verification_format_repair' not in artifacts
