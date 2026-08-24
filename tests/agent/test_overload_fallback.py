"""Does an Anthropic `overloaded_error` now route to the fallback chain?

THE BUG (2026-08-23/24, Clover silent for hours):
Anthropic returned {'type':'error','error':{'type':'overloaded_error',...}} —
notably with HTTP status 200, so it lands in the message-only overload branch of
the classifier. That branch returned:

    FailoverReason.overloaded, retryable=True        # should_fallback defaults False

`retryable` only means "try the SAME model again". With should_fallback unset,
conversation_loop burned its 3 retries on one model and returned
"API call failed after 3 retries" while four healthy fallback models sat unused.
The journal shows exactly that: attempt 1/3, 2/3, 3/3, no model switch.

Fixing the config chain alone could never have helped — nothing was reading it.

This asserts the classifier's verdict directly, using the real error strings.

Run:  python3 -m pytest tests/agent/test_overload_fallback.py -q
  or: python3 tests/agent/test_overload_fallback.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.error_classifier import FailoverReason, classify_api_error  # noqa: E402


# The exact shapes seen in Clover's journal, plus the classic 503/529.
CASES = [
    (
        "anthropic HTTP-200 overloaded_error (the one that silenced Clover)",
        "{'type': 'error', 'error': {'details': None, 'type': 'overloaded_error', "
        "'message': 'Overloaded'}, 'request_id': 'req_011CeM6GQqwGC1CzdnH7ZG3p'}",
        200,
    ),
    ("bare Overloaded message", "HTTP 200: Overloaded", 200),
    ("503 service overloaded", "503 Service Unavailable: server overloaded", 503),
    ("529 overloaded", "529: Overloaded", 529),
    ("429 with overload body", "429 Too Many Requests: service is temporarily overloaded", 429),
]


class _FakeAPIError(Exception):
    """Stands in for anthropic.APIStatusError — the classifier reads str() and
    an optional .status_code, which is all these cases need."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def check(label, msg, status):
    c = classify_api_error(
        _FakeAPIError(msg, status), provider="anthropic", model="claude-opus-5"
    )
    ok_reason = c.reason == FailoverReason.overloaded
    ok_fb = bool(getattr(c, "should_fallback", False))
    ok_retry = bool(getattr(c, "retryable", False))
    status_s = "PASS" if (ok_reason and ok_fb) else "FAIL"
    print(f"[{status_s}] {label}")
    print(f"         reason={c.reason} retryable={ok_retry} should_fallback={ok_fb}")
    return ok_reason and ok_fb


def test_overload_falls_back():
    assert all(check(l, m, s) for l, m, s in CASES), (
        "an overloaded error must set should_fallback so the next model is tried"
    )


if __name__ == "__main__":
    results = [check(l, m, s) for l, m, s in CASES]
    print("\n=== verdict ===")
    if all(results):
        print("[PASS] every overload shape routes to the fallback chain")
        sys.exit(0)
    print(f"[FAIL] {results.count(False)}/{len(results)} still dead-end on one model")
    sys.exit(1)
