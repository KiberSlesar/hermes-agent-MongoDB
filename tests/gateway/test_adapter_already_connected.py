"""Regression: messaging acquire must not treat is_connected property as a method."""

from types import SimpleNamespace

from gateway.run import _adapter_already_connected


def test_adapter_already_connected_reads_bool_property():
    class Adapter:
        @property
        def is_connected(self):
            return True

    assert _adapter_already_connected(Adapter()) is True


def test_adapter_already_connected_false_when_idle():
    class Adapter:
        @property
        def is_connected(self):
            return False

    assert _adapter_already_connected(Adapter()) is False


def test_adapter_already_connected_detects_running_updater():
    adapter = SimpleNamespace(
        is_connected=False,
        _app=SimpleNamespace(updater=SimpleNamespace(running=True)),
    )
    assert _adapter_already_connected(adapter) is True


def test_callable_is_connected_still_supported():
    adapter = SimpleNamespace(is_connected=lambda: True)
    assert _adapter_already_connected(adapter) is True
