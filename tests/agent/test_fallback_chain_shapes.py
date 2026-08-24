"""B1 regression: a configured fallback chain must actually be usable.

WHY THIS FILE EXISTS
On 2026-08-23/24 Clover went silent for hours. Root cause was NOT the error
classifier. It was that `fallback_providers` entries written as the ordinary
"provider/model" strings were dropped by an isinstance(dict) filter, in TWO
independent places, with no error and no warning. The configured chain of four
models parsed to a chain of ZERO. When the primary started returning
`overloaded_error` the loop went looking for somewhere to go and found nothing.

The first attempted fix (commit 1c5b09e) set `should_fallback=True` in the
classifier and shipped a test that asserted... the classifier's verdict. Nothing
read that flag except a debug log, so the "fix" changed no behaviour and the
test passed anyway. That is the failure mode this file is written against:
assert the THING THAT MATTERS, not the thing that is easy to reach.
"""
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from clover_cli.fallback_config import coerce_fallback_entry, get_fallback_chain


def test_plain_string_entry_is_usable():
    """The shape every human writes must survive."""
    got = coerce_fallback_entry("anthropic/claude-opus-5")
    assert got is not None, "a 'provider/model' string must not be dropped"
    assert got["provider"] == "anthropic"
    assert got["model"] == "claude-opus-5"


def test_model_id_containing_slashes_survives():
    """Split on the FIRST slash: real model ids contain more."""
    got = coerce_fallback_entry("openrouter/deepseek/deepseek-chat")
    assert got is not None
    assert got["provider"] == "openrouter"
    assert got["model"] == "deepseek/deepseek-chat"


def test_dict_entry_still_works():
    got = coerce_fallback_entry({"provider": "anthropic", "model": "claude-haiku-4-5"})
    assert got is not None
    assert got["model"] == "claude-haiku-4-5"


def test_unusable_entries_return_none_not_silence():
    assert coerce_fallback_entry("no-slash-here") is None
    assert coerce_fallback_entry({"provider": "anthropic"}) is None
    assert coerce_fallback_entry(12345) is None
    assert coerce_fallback_entry("") is None


def test_the_exact_config_that_silenced_clover():
    """THE REGRESSION. This config produced a chain of length 0."""
    config = {
        "fallback_providers": [
            "anthropic/claude-opus-4-8",
            "anthropic/claude-sonnet-4-6",
            "anthropic/claude-fable-5",
            "anthropic/claude-haiku-4-5",
        ]
    }
    chain = get_fallback_chain(config)
    assert len(chain) == 4, (
        f"configured 4 fallbacks, chain has {len(chain)}. "
        "An empty chain is how the agent goes silent."
    )
    assert [c["model"] for c in chain] == [
        "claude-opus-4-8", "claude-sonnet-4-6",
        "claude-fable-5", "claude-haiku-4-5",
    ], "order must be preserved: it is the operator's priority"


def test_mixed_string_and_dict_config():
    chain = get_fallback_chain({
        "fallback_providers": [
            "anthropic/claude-opus-4-8",
            {"provider": "openai", "model": "gpt-5"},
        ]
    })
    assert len(chain) == 2


def test_one_bad_entry_does_not_kill_the_good_ones():
    """Partial failure must degrade, not collapse."""
    chain = get_fallback_chain({
        "fallback_providers": [
            "garbage-no-slash",
            "anthropic/claude-opus-4-8",
        ]
    })
    assert len(chain) == 1
    assert chain[0]["model"] == "claude-opus-4-8"


def test_loop_consumes_classifier_verdict():
    """The loop must READ should_fallback, not just log it.

    The 1c5b09e lesson: a flag nothing reads is not a fix. Assert the decision
    expression in conversation_loop actually references the classifier.
    """
    loop_src = (Path(ROOT) / "agent" / "conversation_loop.py").read_text()
    marker = "classified.should_fallback and retry_count"
    assert marker in loop_src, (
        "conversation_loop no longer consumes classified.should_fallback — "
        "the classifier's verdict is being computed and ignored again."
    )


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"[PASS] {name}")
        except AssertionError as e:
            fails += 1
            print(f"[FAIL] {name}\n       {e}")
    print("\n=== verdict ===")
    print("[PASS] chain builds from the shapes humans write" if not fails
          else f"[FAIL] {fails} failing")
    sys.exit(1 if fails else 0)
