import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from factory.control.acceptance_ledger import coverage, criteria_for
from factory.control.review_workspace import review_workspace, changed_sources
from factory.control.providers import ProviderResult, ProviderRequest
from factory.control.execution import ExecutionError
from tests.test_workbench_app import app_env
from tests.test_control_app import login, project
from tests.review_helpers import passing_review


def test_coverage_rejects_missing_duplicate_and_empty_evidence():
    criteria = criteria_for({'plan': {'tasks': [{'id': 'a', 'acceptance': ['run', 'export']}]}})
    rows = [{'id': c['id'], 'status': 'pass', 'evidence': 'Observed expected output'} for c in criteria]
    assert coverage(criteria, {'criteria': rows}, 'sha')['complete']
    for bad in [rows[:1], [rows[0], rows[0]], [{**r, 'evidence': ''} for r in rows], [{**rows[0], 'id': 'unknown'}, rows[1]]]:
        assert not coverage(criteria, {'criteria': bad}, 'sha')['complete']
    assert coverage(criteria, {}, 'sha')['counts']['unverified'] == 2


def test_snapshot_is_committed_isolated_and_detects_source_changes(app_env):
    _, _, _, repo = app_env
    original = (repo / 'greeting.txt').read_bytes()
    (repo / 'ignored-private-file').write_text('not part of source')
    with review_workspace(repo) as (root, commit, baseline):
        assert not (root / '.git').exists()
        assert not (root / 'ignored-private-file').exists()
        (root / 'probe.py').write_text('print(42)')
        assert subprocess.check_output([sys.executable, 'probe.py'], cwd=root).strip() == b'42'
        assert changed_sources(root, baseline) == []
        (root / 'greeting.txt').write_text('rewritten')
        assert changed_sources(root, baseline) == ['greeting.txt']
        assert (repo / 'greeting.txt').read_bytes() == original
    assert not root.exists()
    with pytest.raises(ValueError, match='提交'):
        with review_workspace(repo, 'wrong'):
            pass


def setup_review(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Verify greeting output')
    service.cancels[run['id']] = threading.Event()
    return service, run, p, repo


def test_overall_pass_without_coverage_cannot_deliver(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    monkeypatch.setattr(service.runner, 'run', lambda *args, **kwargs: ProviderResult('{"verdict":"pass","reason":"looks fine"}', cost_usd=.01))
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError, match='逐项验收'):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert artifacts['acceptance_ledger']['counts']['unverified'] == 1


def test_verifier_source_edit_invalidates_its_pass(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    original = (repo / 'greeting.txt').read_bytes()
    def reviewer(request, emit, cancel=None):
        assert request.verification and request.read_only
        assert Path(request.workspace).resolve() != repo.resolve()
        (Path(request.workspace) / 'greeting.txt').write_text('cheating')
        return ProviderResult(passing_review(request, 'fake successful check'))
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError, match='修改了被验收源码'):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert artifacts['verification']['error_type'] == 'verification_source_changed'
    assert (repo / 'greeting.txt').read_bytes() == original


def test_claude_active_review_exposes_terminal_but_not_edit_tools(app_env, monkeypatch):
    sdk = pytest.importorskip('claude_agent_sdk')
    from factory.control import claude_terminal, providers
    monkeypatch.setattr(claude_terminal, 'available', lambda: True)
    class ResultMessage:
        is_error = False
        result = 'done'
        session_id = 'review'
        total_cost_usd = 0
    async def query(*, prompt, options):
        assert options.tools == ['Read', 'Glob', 'Grep']
        assert 'mcp__project__run_command' in options.allowed_tools
        assert 'mcp__project__browser_screenshot' in options.allowed_tools
        assert 'disposable independent verification' in options.system_prompt['append']
        hook = options.hooks['PreToolUse'][0].hooks[0]
        for tool, decision in [('mcp__project__run_command', 'allow'), ('Edit', 'deny'), ('Bash', 'deny')]:
            response = await hook({'tool_name': tool, 'tool_input': {}}, None, {})
            assert response['hookSpecificOutput']['permissionDecision'] == decision
        yield ResultMessage()
    monkeypatch.setattr(sdk, 'query', query)
    monkeypatch.setattr(sdk, 'ResultMessage', ResultMessage)
    _, _, _, repo = app_env
    providers._run_claude(ProviderRequest('claude', 'test', 'review', str(repo), read_only=True, verification=True), lambda *e: None)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux production sandbox')
def test_real_sandbox_probe_cannot_write_developer_workspace(app_env):
    from factory.control.claude_terminal import available, TerminalSession
    if not available():
        pytest.skip('bubblewrap unavailable')
    _, _, _, repo = app_env
    original = (repo / 'greeting.txt').read_bytes()
    with review_workspace(repo) as (root, _, baseline):
        session = TerminalSession(root)
        try:
            import shlex
            result = session.run('python3 -c ' + shlex.quote('from pathlib import Path; print(Path("greeting.txt").read_text()); Path("probe-result.txt").write_text("verified")'), 15)
            assert result['exit_code'] == 0
            assert (root / 'probe-result.txt').read_text() == 'verified'
            attempt = session.run('echo BAD > ' + shlex.quote(str(repo / 'greeting.txt')), 15)
            assert attempt['exit_code'] != 0
            assert changed_sources(root, baseline) == []
        finally:
            session.close()
    assert (repo / 'greeting.txt').read_bytes() == original


def test_missing_coverage_retries_verifier_without_restarting_coding(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    calls = []
    def reviewer(request, emit, cancel=None):
        calls.append(request)
        if len(calls) == 1:
            return ProviderResult('{"verdict":"pass","reason":"missed coverage"}', session_id='same-review', cost_usd=.01)
        assert request.session_id == 'same-review'
        assert request.workspace == calls[0].workspace
        assert request.timeout_s <= calls[0].timeout_s
        return ProviderResult(passing_review(request, 'read greeting.txt and checked output'), cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert len(calls) == 2 and all(c.verification for c in calls)
    assert artifacts['acceptance_ledger']['complete']
    assert service._usage(run['id'], profile='verification')['known_cost_usd'] == .02


def test_active_browser_error_cannot_be_hidden_by_overall_pass(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    def reviewer(request, emit, cancel=None):
        emit('browser.observed', {'action': 'click', 'ok': False, 'error': 'Submit button failed'})
        return ProviderResult(passing_review(request, 'claimed successful submit'))
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError, match='Submit button failed'):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)


def test_screenshot_survives_disposable_workspace_and_rejects_foreign_file(app_env, tmp_path):
    from factory.control.review_workspace import preserve_screenshot
    _, _, _, repo = app_env
    png = b'\x89PNG\r\n\x1a\n' + b'fixture'
    with review_workspace(repo) as (root, _, _):
        (root / 'screen.png').write_bytes(png)
        saved = Path(preserve_screenshot(root, 'screen.png', tmp_path / 'evidence'))
        assert saved.read_bytes() == png
        with pytest.raises(ValueError):
            preserve_screenshot(root, repo / 'greeting.txt', tmp_path / 'evidence')
    assert saved.read_bytes() == png


@pytest.mark.skipif(not sys.platform.startswith('linux') or not Path('/opt/webuddy-browser/bridge.mjs').exists(), reason='Production browser runtime')
def test_real_verification_browser_opens_and_captures_local_result(app_env):
    import shlex
    from factory.control.claude_terminal import TerminalSession, available
    from factory.control.project_browser import BrowserSession
    if not available():
        pytest.skip('bubblewrap unavailable')
    _, _, _, repo = app_env
    with review_workspace(repo) as (root, _, baseline):
        (root / 'index.html').write_text('<h1>Verification fixture</h1><button onclick="this.textContent=\'Passed\'">Check</button>')
        terminal = TerminalSession(root)
        browser = BrowserSession(root, terminal)
        try:
            server = "from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler; s=ThreadingHTTPServer(('127.0.0.1',0),SimpleHTTPRequestHandler); print(s.server_port,flush=True); s.serve_forever()"
            terminal.run('python3 -u -c ' + shlex.quote(server) + ' > /tmp/review-port 2>&1 &', 10)
            result = terminal.run('for i in 1 2 3 4 5; do test -s /tmp/review-port && break; sleep 0.2; done; head -1 /tmp/review-port', 10)
            port = int(result['output'].strip())
            observed = browser.call('open', url=f'http://127.0.0.1:{port}')
            assert observed['ok'], observed
            shot = browser.call('screenshot')
            assert shot['ok'], shot
            image = Path(shot['screenshot_path'])
            if not image.is_absolute(): image = root / image
            assert image.read_bytes().startswith(b'\x89PNG')
            assert changed_sources(root, baseline) == []
        finally:
            browser.close()
            terminal.close()


def test_export_attributes_cannot_remove_tests_from_review(app_env):
    _, _, _, repo = app_env
    (repo / '.gitattributes').write_text('greeting.txt export-ignore\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.gitattributes'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'export attribute fixture'], check=True)
    with review_workspace(repo) as (root, _, _):
        assert (root / 'greeting.txt').read_bytes() == (repo / 'greeting.txt').read_bytes()
