import importlib.util
from pathlib import Path
from types import SimpleNamespace

def test_browser_smoke_refuses_skips_and_missing_selection():
    spec = importlib.util.spec_from_file_location('ci_smoke', 'runtime/project-browser/ci_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for selected, skipped, expected in [(2, [], 0), (1, [], 1), (2, ['unavailable'], 1)]:
        reporter = SimpleNamespace(stats={'skipped': skipped, 'passed': [1, 2]})
        session = SimpleNamespace(testscollected=selected, exitstatus=0,
            config=SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter)))
        module.RequiredSmoke().pytest_sessionfinish(session, 0)
        assert session.exitstatus == expected


def test_ci_runs_required_commands_and_separate_strict_smoke():
    """The same commands must still run; they are no longer one chained step.

    ``npm ci && npm run build && npm test`` used to be a single step inside the
    backend job, which is exactly why a backend failure meant the frontend never
    ran at all. The commands are now separate steps in their own job, so this
    checks for each of them rather than for the chain.
    """
    workflow = Path('.github/workflows/ci.yml').read_text()
    for required in ['uv sync --frozen --all-extras', "pytest -m 'not smoke'",
                     'npm ci', 'npm run build', 'npm test', 'browser-smoke:',
                     'docker build -f runtime/project-browser/Dockerfile.smoke',
                     'docker run --rm --privileged']:
        assert required in workflow
    assert 'continue-on-error' not in workflow
    assert 'ci_smoke.py' in Path('runtime/project-browser/Dockerfile.smoke').read_text()


def test_frontend_runs_even_when_the_backend_fails():
    """Independent jobs, or a red backend hides whether the frontend is healthy."""
    import yaml
    jobs = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())['jobs']
    assert {'backend', 'frontend', 'browser-smoke'} <= set(jobs)
    for name in ('frontend', 'browser-smoke'):
        assert 'needs' not in jobs[name], f'{name} 不应因为后端失败而不跑'


def test_backend_proves_isolation_before_running_any_test():
    """Installed is not usable, and the canary has to come first.

    Without a working bubblewrap the capability-pack, chat-tool, MFD and
    meeting-tool suites fail as one cascade with a misleading sixty-eight-way
    symptom. The gate is what turns that into a single clear message, so its
    presence -- and its position ahead of the test step -- is part of the
    contract, not an implementation detail.
    """
    import yaml
    backend = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())['jobs']['backend']
    assert backend['runs-on'] == 'ubuntu-22.04'
    steps = backend['steps']
    def index(fragment):
        for i, step in enumerate(steps):
            if fragment in (step.get('run') or '') or fragment in (step.get('name') or ''):
                return i
        raise AssertionError(f'workflow 里找不到 {fragment!r}')
    install = index('install -y bubblewrap')
    minimal = index('--unshare-user --ro-bind / / true')
    canary = index('isolation_canary.py')
    tests = index("pytest -m 'not smoke'")
    assert install < minimal < tests, '最小隔离命令必须在测试之前'
    assert canary < tests, '金丝雀必须在测试之前'
    assert Path('.github/scripts/isolation_canary.py').is_file()


def test_browser_smoke_uses_runner_supporting_nested_user_namespaces():
    import yaml
    workflow = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())
    assert workflow['jobs']['browser-smoke']['runs-on'] == 'ubuntu-22.04'
