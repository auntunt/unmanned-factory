import threading
import pytest
from factory.control.resources import command_slot


def test_command_slot_wait_is_bounded_and_cancellable():
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
