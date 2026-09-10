import os
import subprocess
import sys
import threading
import time

import pytest
from factory.control import resources
from factory.control.resources import command_slot


@pytest.fixture(autouse=True)
def isolated_locks(monkeypatch, tmp_path):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.setattr(resources.tempfile, 'tempdir', str(tmp_path))


def test_command_slot_wait_is_bounded_and_cancellable(monkeypatch):
    monkeypatch.setenv('WEBUDDY_COMMAND_SLOTS', '1')
    with command_slot(1):
        with pytest.raises(TimeoutError):
            with command_slot(.05):
                pytest.fail('shared command slot was not exclusive')
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(InterruptedError):
            with command_slot(1, cancel):
                pytest.fail('cancel ignored')
    with command_slot(.2) as waited:
        assert waited < .2


def test_default_two_slots_allow_overlap_but_bound_third(monkeypatch):
    monkeypatch.delenv('WEBUDDY_COMMAND_SLOTS', raising=False)
    with command_slot(1):
        with command_slot(1):
            with pytest.raises(TimeoutError):
                with command_slot(.05):
                    pytest.fail('third command exceeded default capacity')


@pytest.mark.parametrize('value', ['0', '9', '-1', 'abc', '2.5'])
def test_invalid_capacity_fails_closed(monkeypatch, value):
    monkeypatch.setenv('WEBUDDY_COMMAND_SLOTS', value)
    with pytest.raises(ValueError, match='WEBUDDY_COMMAND_SLOTS'):
        with command_slot(1):
            pytest.fail('invalid capacity dispatched')


def test_two_real_processes_overlap_and_release_capacity(monkeypatch, tmp_path):
    monkeypatch.setenv('WEBUDDY_COMMAND_SLOTS', '2')
    release = tmp_path / 'release'
    script = """
import sys, time
from pathlib import Path
from factory.control.resources import command_slot
with command_slot(5):
    print('acquired', flush=True)
    until = time.monotonic() + 5
    while not Path(sys.argv[1]).exists() and time.monotonic() < until:
        time.sleep(.01)
"""
    children = [subprocess.Popen([sys.executable, '-c', script, str(release)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=dict(os.environ)) for _ in range(2)]
    try:
        import selectors
        with selectors.DefaultSelector() as ready:
            for child in children:
                ready.register(child.stdout, selectors.EVENT_READ)
            seen = set()
            deadline = time.monotonic() + 4
            while len(seen) < 2 and time.monotonic() < deadline:
                for key, _ in ready.select(.1):
                    assert key.fileobj.readline().strip() == 'acquired'
                    seen.add(key.fileobj)
                    ready.unregister(key.fileobj)
        assert len(seen) == 2, 'two processes must simultaneously hold distinct slots'
        with pytest.raises(TimeoutError):
            with command_slot(.1):
                pytest.fail('cross-process capacity exceeded')
        release.touch()
        for child in children:
            assert child.wait(timeout=5) == 0
        with command_slot(.5):
            pass
    finally:
        release.touch()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_waiting_thread_cancellation_wakes_promptly(monkeypatch):
    monkeypatch.setenv('WEBUDDY_COMMAND_SLOTS', '1')
    cancel = threading.Event()
    result = []
    def waiting():
        try:
            with command_slot(10, cancel):
                result.append('unexpected')
        except InterruptedError:
            result.append('cancelled')
    with command_slot(1):
        thread = threading.Thread(target=waiting)
        thread.start()
        cancel.set()
        thread.join(timeout=1)
        assert result == ['cancelled']
