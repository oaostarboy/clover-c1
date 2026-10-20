"""Independent final-boundary review; synthetic transport, real owner readers."""
import asyncio
import json
import time
from pathlib import Path

import httpx
import openai
import pytest
from agent import auxiliary_client as aux
from agent.bounded_completion import CompletionHeld
from tests.agent.test_provider_security_corrected import route_fixture, response_for, run, entry, genuine_pool_route


@pytest.mark.parametrize('delay,timeout', [(.005, .5), (.06, .03)])
def test_openai_sync_request_construction_cannot_dispatch_late(monkeypatch, delay, timeout):
    requests = []
    timestamps = []
    def respond(req):
        requests.append(req)
        timestamps.append(time.monotonic())
        return response_for('openai')
    config = route_fixture(monkeypatch, 'openai', respond)
    assert run(config, timeout=2).text == 'synthetic'
    requests.clear(); timestamps.clear()
    original = openai.AsyncOpenAI._build_request
    def slow(self, *args, **kwargs):
        time.sleep(delay)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(openai.AsyncOpenAI, '_build_request', slow)
    started = time.monotonic()
    if delay < timeout:
        assert run(config, timeout=timeout).text == 'synthetic'
        assert len(requests) == 1 and timestamps[0] - started < timeout
    else:
        with pytest.raises(TimeoutError):
            run(config, timeout=timeout)
        print('INDEPENDENT_LATE_OPENAI_REQUESTS', len(requests),
              'DISPATCH_SECONDS', [t - started for t in timestamps], 'BUDGET_SECONDS', timeout)
        assert not requests
