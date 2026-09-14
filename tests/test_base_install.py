"""Also run in CI before extras are installed, using only base and dev dependencies."""
import subprocess
import sys


def test_deploy_targets_import_and_web_entrypoint():
    subprocess.run(
        [sys.executable, '-I', '-c', 'import factory.control.deploy_targets'],
        check=True, capture_output=True, text=True, timeout=30,
    )
    result = subprocess.run(
        [sys.executable, '-I', '-c', 'from factory.control.app import main; main()', '--help'],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert 'usage:' in result.stdout.lower()
