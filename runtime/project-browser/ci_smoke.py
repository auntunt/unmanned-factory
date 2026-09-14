"""Real Linux/browser smoke: unavailable environments and skipped tests are failures."""
from pathlib import Path
import pytest
from factory.control.claude_terminal import available

class RequiredSmoke:
    def pytest_sessionfinish(self, session, exitstatus):
        reporter = session.config.pluginmanager.get_plugin('terminalreporter')
        if session.testscollected != 2 or len(reporter.stats.get('passed', [])) != 2 or reporter.stats.get('skipped'):
            session.exitstatus = 1

if __name__ == '__main__':
    assert available(), 'sandbox unavailable; refusing skipped smoke'
    assert Path('/opt/webuddy-browser/bridge.mjs').is_file(), 'browser runtime missing'
    raise SystemExit(pytest.main(['-q', 'tests/test_active_verification.py',
        '-k', 'real_verification_browser or real_sandbox_probe', '-rs'], plugins=[RequiredSmoke()]))
