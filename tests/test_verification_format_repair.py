"""One bounded reformat of a structurally invalid verdict, and nothing more."""
import json
import threading
import time

import pytest

from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult
from factory.control.verification import _repair_verdict_format, _stated_verdicts
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


def test_one_reformat_recovers_a_clear_verdict_without_reviewing_again(app_env):
    client, store, service, repo, p, run, cfg = _review(app_env)
    calls = []

    def respond(request, emit, cancel=None):
        calls.append(request)
        if len(calls) == 1:
            return ProviderResult('I reviewed the change. The greeting is wrong, so this is a '
                                  'fail. (No JSON envelope here.)', cost_usd=0.03)
        assert 'reformatter' in request.prompt and 'NOT a reviewer' in request.prompt
        return ProviderResult(json.dumps({'verdict': 'fail', 'reason': 'greeting 未更新'}),
                              cost_usd=0.01)

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
    calls = []

    def respond(request, emit, cancel=None):
        calls.append(request)
        if len(calls) == 1:
            return ProviderResult('Evidence is missing for two criteria, so: fail.', cost_usd=0.03)
        return ProviderResult(json.dumps({'verdict': 'pass', 'reason': '看起来没问题'}), cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError, match='未返回有效结构化结果'):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert artifacts['verification'] == {'verdict': 'fail', 'reason': '独立验证模型未返回有效 verdict',
                                         'error_type': 'invalid_response'}
    assert artifacts['verification_format_repair']['accepted'] is False
    assert artifacts['verification_format_repair']['refused'] == '重排改变了原结论'
    assert _repairs(store, run['id']) == []


def test_unverified_and_fail_survive_a_reformat_and_are_never_upgraded(app_env):
    for stated in ('unverified', 'fail'):
        assert _stated_verdicts(f'The outcome is {stated} for now.') == {stated}
    # A pass claim that the original never made cannot be introduced.
    assert _stated_verdicts('I could not verify anything: unverified.') == {'unverified'}
    assert _stated_verdicts('passed the tests but the verdict is fail') == {'fail'}
    assert _stated_verdicts('bypassed, unverifiable, failing') == set()


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
        return ProviderResult('Clearly a fail, but no JSON.', cost_usd=0.03)

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
                                  'Clearly a fail.', deadline) is None
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
                                  'Clearly a fail.', time.monotonic() + 600) is None
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
                                  'Clearly a fail.', time.monotonic() + 1) is None
    # Cancelled.
    service.cancels[run['id']].set()
    assert _repair_verdict_format(service, run['id'], run, p, artifacts,
                                  cfg['agent_verification_profile'], str(repo),
                                  'Clearly a fail.', time.monotonic() + 600) is None
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
