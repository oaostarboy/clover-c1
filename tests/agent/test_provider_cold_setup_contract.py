"""Cold SDK preparation must not masquerade as usage-parser coverage.

Fresh child processes keep real SDK import/compilation cold. Only a private
loader delay and synthetic HTTP are injected. No auth/network retry occurs.
"""
import json
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import asyncio
from contextlib import contextmanager
import importlib.machinery
import json
import sys
import time
import pytest
from tests.agent import test_provider_security_corrected as target
from tests.gateway.test_trusted_human_origin_ingress import network_guard

scenario = sys.argv[1]
requests = []
delays = []
with pytest.MonkeyPatch.context() as mp:
    with contextmanager(network_guard.__wrapped__)(mp):
        assert 'anthropic' not in sys.modules, 'cold SDK control was accidentally prewarmed'
        loader = importlib.machinery.SourceFileLoader
        original_code = loader.get_code
        def delayed_code(self, fullname):
            if fullname == 'anthropic':
                start = time.monotonic()
                time.sleep(1.05)
                delays.append(time.monotonic()-start)
            return original_code(self, fullname)
        mp.setattr(loader, 'get_code', delayed_code)
        original_fixture = target.route_fixture
        def counted_fixture(monkeypatch, provider, respond):
            def counted(request):
                requests.append(time.monotonic())
                return respond(request)
            return original_fixture(monkeypatch, provider, counted)
        mp.setattr(target, 'route_fixture', counted_fixture)
        if scenario == 'prepared_usage':
            # The original one-second positive assertion, not a replacement
            # serializer or raised timeout. SDK preparation must precede it.
            target.test_malformed_numeric_usage_never_becomes_known(mp, 'anthropic', -1)
            assert len(requests) == 1
        else:
            def response(request):
                import httpx
                return httpx.Response(200, json={'content':[{'type':'text','text':'synthetic'}],
                                               'usage':{'output_tokens':-1}})
            config = target.route_fixture(mp, 'anthropic', response)
            async def cold_then_ready():
                with pytest.raises(TimeoutError):
                    await target.complete(config=config, messages=[{'role':'user','content':'synthetic'}],
                                          max_output_tokens=64, timeout=1)
                assert not requests, 'cold overrun entered physical HTTP'
                await asyncio.sleep(.03)
                assert not requests, 'cold overrun dispatched late'
                result = await target.complete(config=config, messages=[{'role':'user','content':'synthetic'}],
                                               max_output_tokens=64, timeout=1)
                assert result.usage == 'unknown' and result.output_tokens is None
                assert len(requests) == 1
            asyncio.run(cold_then_ready())
        assert len(delays) == 1 and delays[0] >= 1.05
print('COLD_PROBE ' + json.dumps(dict(scenario=scenario, cold_delays=delays, physical_requests=len(requests))))
'''


@pytest.mark.parametrize('scenario', ['prepared_usage', 'cold_deadline'])
def test_cold_sdk_setup_and_one_second_completion_have_separate_contracts(scenario, record_property):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-c', PROBE, scenario], cwd=root,
                            text=True, capture_output=True, timeout=30)
    record_property('cold_sdk_probe_stdout', result.stdout)
    record_property('cold_sdk_probe_stderr', result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(next(line[len('COLD_PROBE '):] for line in result.stdout.splitlines()
                              if line.startswith('COLD_PROBE ')))
    assert receipt['scenario'] == scenario and receipt['physical_requests'] == 1
    assert len(receipt['cold_delays']) == 1 and receipt['cold_delays'][0] >= 1.05
