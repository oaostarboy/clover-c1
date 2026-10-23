"""The timing fixture must reject quadratic controls, not scheduler stalls."""
import pytest

from tests.perf_guards import test_pattern_b_scaling as guard


def quadratic_dict_field(n, frag):
    acc = {0: {'function': {'arguments': ''}}}
    for _ in range(n):
        acc[0]['function']['arguments'] += frag
    return len(acc[0]['function']['arguments'])


def test_fixed_work_uses_cpu_clock_and_normalizes_calls(monkeypatch):
    cpu = [0.0]
    calls = []

    def work():
        cpu[0] += 0.125
        calls.append(True)

    def forbidden_wall_clock():
        raise AssertionError('wall-clock stalls are not assembly work')

    monkeypatch.setattr(guard.time, 'process_time', lambda: cpu[0])
    monkeypatch.setattr(guard.time, 'perf_counter', forbidden_wall_clock)
    assert guard._fixed_work_time(work, number=16, repeat=3) == 0.125
    assert len(calls) == 48


def test_copy_work_guard_rejects_real_quadratic_control():
    good = guard.TestToolCallFragmentAssemblyLinear._assemble_dict_field
    small, large = guard._fragment_copy_work(good, 256), guard._fragment_copy_work(good, 1024)
    assert large == 4 * small
    small_bad = guard._fragment_copy_work(quadratic_dict_field, 256)
    large_bad = guard._fragment_copy_work(quadratic_dict_field, 1024)
    assert large_bad / small_bad > guard.TestToolCallFragmentAssemblyLinear.MAX_RATIO


def test_timing_guard_rejects_real_quadratic_control(monkeypatch):
    # Same guard and threshold as the good model. A smaller control avoids
    # copying terabytes; the algorithm and fixed-work measurement are identical.
    case = guard.TestToolCallFragmentAssemblyLinear()
    monkeypatch.setattr(case, 'N_SMALL', 4000)
    monkeypatch.setattr(case, 'N_LARGE', 16000)
    monkeypatch.setattr(case, '_assemble_dict_field', quadratic_dict_field)
    with pytest.raises(AssertionError, match='assembly is superlinear'):
        case.test_4x_fragments_cost_about_4x_time()
