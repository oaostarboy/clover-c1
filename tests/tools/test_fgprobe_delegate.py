"""Reviewer probes (Fable, C1.4 P1) for delegate_task under the limit. Not part of the PR."""
from __future__ import annotations

import json
import time

from agent import foreground_time_limit as fl
from tests.tools.test_delegate_foreground_limit import (  # noqa: F401
    _BlockingChild, _clean_registry, _limit_config, _no_async_surface, _root,
    _run_inline, _unbind_session_context, clock, fast_grace, harness,
)
import tools.delegate_tool as dt


def test_probe_config_floor_does_not_beat_the_remainder(clock, monkeypatch, fast_grace):
    """FIX ROUND 2 (parent decision, owner agreed): no limit on a no-async
    surface, so no episode bound exists; an inline child is governed by the
    configured child timeout exactly as in the base code. (Was: a 30 s config
    floor must not beat the episode remainder.)"""
    _no_async_surface()
    root = _root(platform='api_server')
    assert fl.remaining_seconds(root) is None
    monkeypatch.setattr(dt, '_get_child_timeout', lambda: 0.3)
    clock.advance(10_000)
    child = _BlockingChild(hang_seconds=20.0)
    started = time.monotonic()
    result = _run_inline(root, child)
    elapsed = time.monotonic() - started
    assert result['status'] == 'timeout', result
    assert elapsed < 6, f'waited {elapsed:.1f}s'
    assert child.interrupts >= 1


def test_probe_remainder_exhausted_after_setup_is_refused_at_second_check(clock, monkeypatch, fast_grace):
    """FIX ROUND 2 (parent decision, owner agreed): a spent clock never refuses
    an inline child any more -- there is no episode on a no-async surface. The
    child starts and runs as in the base code. (Was: refused at the re-check.)"""
    _no_async_surface()
    root = _root(platform='api_server')
    clock.advance(299.5)
    child = _BlockingChild(hang_seconds=0.0)
    clock.advance(5)
    result = _run_inline(root, child)
    assert child.started.is_set(), 'the inline child must start (no limit applies)'
    assert result['status'] != 'refused'
    assert 'time limit' not in json.dumps(result).lower()


def test_probe_subagent_depth_inline_child_is_unbounded(clock, monkeypatch, fast_grace):
    """An orchestrator subagent (depth>0) waiting on its worker is not limited."""
    _no_async_surface()
    root = _root(platform='api_server')
    root._delegate_depth = 1
    fl.begin_turn(root)
    assert fl.remaining_seconds(root) is None
    clock.advance(10_000)
    import concurrent.futures as cf

    seen = {}
    real_result = cf.Future.result

    def spy(self, timeout=None):
        seen.setdefault('timeout', timeout)
        return real_result(self, timeout=timeout)

    monkeypatch.setattr(cf.Future, 'result', spy)
    child = _BlockingChild(hang_seconds=0.0)
    result = _run_inline(root, child)
    assert seen.get('timeout') is None, seen
    assert result['status'] != 'timeout'
