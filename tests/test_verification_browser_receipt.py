"""验收回执的浏览器引用：有界纠正，但不放宽真实闸门。

现场（run 88256a6c…，候选 ebf0a64）：应用 25 项测试通过、ledger 33 pass/1 fail、
本轮新浏览器观察干净，却停在 needs_human——模型把 `recent_failures` 里的历史
失败编号（favicon 404，已修）一并写进了 browser_review.event_ids，引用与
`browser_observations.latest` 不相等，直接判失败且没有纠正机会。

两条语义必须分清：
  · 调用前快照 —— 提示词里给模型看的那份，模型只能引用它；
  · 本轮新观察 —— 模型自己在验收中产生的，事件编号在提示词之后才存在。
历史已解决的失败不能无限阻断新的干净观察；真实未解决的错误必须仍然阻断。
"""
import json

import pytest
from factory.control.providers import ProviderResult
from factory.control.verification_evidence import browser_evidence
from tests.test_control_app import app_env, login, project  # noqa: F401
from tests.test_active_verification import setup_review
from tests.review_helpers import passing_review


def _prompt_observations(request):
    """模型只能引用提示词里给它的那份快照——照真实约束取值，不作弊读库。"""
    text = request.prompt
    start = text.index('"latest"') - 1
    depth, end = 0, None
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(text[start:end])


def _stale_failure_then_clean(service, rid):
    """和现场同形：verification 任务先有失败观察，随后被新的干净观察取代。"""
    service.store.append(rid, 'browser.observed', {
        'ok': True, 'action': 'screenshot', 'url': 'http://127.0.0.1:8080/',
        'errors': ['HTTP 404: http://127.0.0.1:8080/favicon.ico']}, 'verification')
    service.store.append(rid, 'browser.observed', {
        'ok': True, 'action': 'open', 'url': 'http://127.0.0.1:8080/', 'errors': []}, 'coding')


def test_receipt_citing_historical_failures_is_corrected_once_then_passes(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    _stale_failure_then_clean(service, run['id'])
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        snapshot = _prompt_observations(request)
        # 本轮自己的新观察：干净，且编号在提示词之后才产生。
        emit('browser.observed', {'ok': True, 'action': 'screenshot',
                                  'url': 'http://127.0.0.1:8080/', 'errors': []})
        verdict = json.loads(passing_review(request, 'fresh browser cycle, no errors'))
        latest_ids = [o['event_id'] for o in snapshot['latest']]
        if len(calls) == 1:
            # 现场的错误引用：把 recent_failures 里的历史失败也写了进去。
            ids = latest_ids + [o['event_id'] for o in snapshot['recent_failures']]
        else:
            ids = latest_ids
        verdict['browser_review'] = {'event_ids': ids, 'disposition': 'clean',
                                     'reason': 'fresh browser cycle produced errors:[]'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)

    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)

    assert len(calls) == 2, '应当有且只有一次有界的验收回执纠正'
    assert artifacts['verification']['verdict'] == 'pass', artifacts['verification']
    kinds = [e['type'] for e in service.store.export_events(run['id'])]
    assert 'verification.browser_review_retry' in kinds, kinds


def test_receipt_still_wrong_after_the_one_correction_does_not_pass(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    _stale_failure_then_clean(service, run['id'])
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        emit('browser.observed', {'ok': True, 'action': 'screenshot', 'errors': []})
        verdict = json.loads(passing_review(request, 'fresh browser cycle'))
        verdict['browser_review'] = {'event_ids': [999999], 'disposition': 'clean',
                                     'reason': 'still citing the wrong observations'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)

    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(Exception):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)

    assert len(calls) == 2, '纠正只给一次'
    verification = artifacts['verification']
    assert verification['verdict'] != 'pass', verification
    assert verification.get('error_type') == 'browser_review_unreconciled', verification


def test_real_unresolved_browser_error_still_blocks_without_correction(app_env, monkeypatch):
    """本轮新观察里有真实未解决错误：不纠正、不通过。"""
    service, run, p, repo = setup_review(app_env)
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        emit('browser.observed', {'ok': False, 'action': 'open',
                                  'error': 'net::ERR_CONNECTION_REFUSED', 'errors': []})
        verdict = json.loads(passing_review(request, 'claiming clean anyway'))
        verdict['browser_review'] = {
            'event_ids': [o['event_id'] for o in _prompt_observations(request)['latest']],
            'disposition': 'clean', 'reason': 'claimed clean'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)

    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(Exception):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)

    assert len(calls) == 1, '真实错误不触发回执纠正'
    assert artifacts['verification']['verdict'] != 'pass', artifacts['verification']


def test_wrong_citation_together_with_a_real_failure_gets_no_correction(app_env, monkeypatch):
    """引用错误 + 真实未解决失败：不能因为「先报引用问题」就换来一次纠正机会。

    browser_review_failure 会先返回引用类缺口，所以必须由「本轮仍有未解决失败」
    优先判定，否则真实故障会被当成回执格式问题放行进重试。
    """
    service, run, p, repo = setup_review(app_env)
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        emit('browser.observed', {'ok': False, 'action': 'open',
                                  'error': 'net::ERR_CONNECTION_REFUSED', 'errors': []})
        verdict = json.loads(passing_review(request, 'wrong citation and a broken page'))
        verdict['browser_review'] = {'event_ids': [424242], 'disposition': 'clean',
                                     'reason': 'citing ids that do not exist'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)

    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(Exception):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)

    assert len(calls) == 1, '存在真实失败时不得触发回执纠正'
    verification = artifacts['verification']
    assert verification['verdict'] != 'pass', verification
    assert verification.get('error_type') != 'browser_review_unreconciled', \
        '真实浏览器故障不能被记成回执未对账：' + repr(verification)


def test_coverage_retry_cannot_reset_the_browser_correction_budget(app_env, monkeypatch):
    """整个独立验收过程最多一次浏览器回执纠正，coverage 补齐不得把它重置。

    交错路径：浏览器纠正（1）→ 模型这轮漏了 criteria → coverage 补齐（2）→
    浏览器引用又错（3）。若 coverage 递归没有透传浏览器纠正标志，第 3 次会再得到
    一次纠正机会，等于纠正次数不受限。
    """
    service, run, p, repo = setup_review(app_env)
    _stale_failure_then_clean(service, run['id'])
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        snapshot = _prompt_observations(request)
        emit('browser.observed', {'ok': True, 'action': 'screenshot', 'errors': []})
        verdict = json.loads(passing_review(request, 'review'))
        if len(calls) == 2:
            verdict['criteria'] = verdict['criteria'][:-1]  # 漏一项，触发 coverage 补齐
        verdict['browser_review'] = {
            'event_ids': [o['event_id'] for o in snapshot['latest']]
                         + [o['event_id'] for o in snapshot['recent_failures']],
            'disposition': 'clean', 'reason': 'wrong citation every time'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)

    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(Exception):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)

    retries = [e for e in service.store.export_events(run['id'])
               if e['type'] == 'verification.browser_review_retry']
    assert len(retries) == 1, f'浏览器回执纠正只允许一次，实际 {len(retries)} 次'
    assert artifacts['verification']['verdict'] != 'pass', artifacts['verification']


def test_snapshot_error_not_superseded_at_all_is_not_correctable(app_env, monkeypatch):
    """快照里的错误没有被任何更新观察取代时，不得换来纠正机会。

    用 coding 任务的 errors（ok=True、无 error 串），刻意绕开「本轮仍有未解决失败」
    那条分支，好让覆盖判定成为唯一决定因素——否则这条测试测不到它。
    """
    service, run, p, repo = setup_review(app_env)
    service.store.append(run['id'], 'browser.observed', {
        'ok': True, 'action': 'open', 'url': 'http://127.0.0.1:8080/',
        'errors': ['HTTP 404: http://127.0.0.1:8080/favicon.ico']}, 'coding')
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        snapshot = _prompt_observations(request)
        verdict = json.loads(passing_review(request, 'claiming clean'))
        verdict['browser_review'] = {'event_ids': [o['event_id'] for o in snapshot['latest']],
                                     'disposition': 'clean', 'reason': 'claimed clean'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)

    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(Exception):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)

    retries = [e for e in service.store.export_events(run['id'])
               if e['type'] == 'verification.browser_review_retry']
    assert retries == [], '没有更新观察覆盖旧失败，不得触发回执纠正'
    assert artifacts['verification']['verdict'] != 'pass', artifacts['verification']


def test_newer_but_still_failing_observation_does_not_count_as_superseding(app_env, monkeypatch):
    """更新的观察本身仍然失败：不算「已解决」，同样不得换来纠正机会。"""
    service, run, p, repo = setup_review(app_env)
    service.store.append(run['id'], 'browser.observed', {
        'ok': True, 'action': 'open', 'errors': ['HTTP 404: /favicon.ico']}, 'coding')
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        snapshot = _prompt_observations(request)
        # 本轮之后 coding 又有一次观察，但它仍然带错误。
        service.store.append(run['id'], 'browser.observed', {
            'ok': True, 'action': 'open', 'errors': ['HTTP 500: /api/items']}, 'coding')
        verdict = json.loads(passing_review(request, 'claiming resolved'))
        verdict['browser_review'] = {'event_ids': [o['event_id'] for o in snapshot['latest']],
                                     'disposition': 'clean', 'reason': 'claimed resolved'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)

    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(Exception):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)

    retries = [e for e in service.store.export_events(run['id'])
               if e['type'] == 'verification.browser_review_retry']
    assert retries == [], '更新观察仍失败，不算已解决'
    assert artifacts['verification']['verdict'] != 'pass', artifacts['verification']


def test_superseded_correction_also_counts_against_the_single_attempt(app_env, monkeypatch):
    """走「旧失败已被覆盖」这条路的纠正，同样计入唯一一次配额。"""
    service, run, p, repo = setup_review(app_env)
    _stale_failure_then_clean(service, run['id'])
    calls = []

    def reviewer(request, emit, cancel=None):
        calls.append(request)
        snapshot = _prompt_observations(request)
        emit('browser.observed', {'ok': True, 'action': 'screenshot', 'errors': []})
        verdict = json.loads(passing_review(request, 'review'))
        # 每轮都把历史失败混进引用：纠正后仍错。
        verdict['browser_review'] = {
            'event_ids': [o['event_id'] for o in snapshot['latest']]
                         + [o['event_id'] for o in snapshot['recent_failures']],
            'disposition': 'clean', 'reason': 'still wrong'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)

    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(Exception):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)

    retries = [e for e in service.store.export_events(run['id'])
               if e['type'] == 'verification.browser_review_retry']
    assert len(retries) == 1, f'全过程只允许一次浏览器回执纠正，实际 {len(retries)} 次'
    assert artifacts['verification']['verdict'] != 'pass', artifacts['verification']
