"""Memory-trim coverage for the long-lived messaging gateway housekeeper."""

import gateway.run as gateway_run


class _OneTickStopEvent:
    """Run one housekeeping tick without a sleep or background thread."""

    def __init__(self):
        self.waited = False

    def is_set(self):
        return self.waited

    def wait(self, timeout=None):
        self.waited = True
        return True


def test_gateway_housekeeping_calls_idle_kernel_reaper(monkeypatch):
    import tools.code_kernel as code_kernel

    calls = []
    monkeypatch.setattr(code_kernel, "reap_idle_kernels", lambda: calls.append(1) or 0)

    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)

    assert calls == [1]


def test_gateway_housekeeping_calls_periodic_memory_trim(monkeypatch):
    import clover_cli.mem_trim as mem_trim

    calls = []
    monkeypatch.setattr(
        mem_trim,
        "trim_memory",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)

    assert calls == [{"reason": "messaging gateway housekeeping"}]
