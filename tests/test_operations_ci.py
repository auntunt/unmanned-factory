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
    workflow = Path('.github/workflows/ci.yml').read_text()
    for required in ['uv sync --frozen --all-extras', "pytest -m 'not smoke'",
                     'npm ci && npm run build && npm test', 'browser-smoke:',
                     'docker build -f runtime/project-browser/Dockerfile.smoke',
                     'docker run --rm --privileged']:
        assert required in workflow
    assert 'continue-on-error' not in workflow
    assert 'ci_smoke.py' in Path('runtime/project-browser/Dockerfile.smoke').read_text()
