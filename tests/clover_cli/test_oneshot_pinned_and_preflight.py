"""``clover -z`` must never silently run a pinned model on another model, and
must fail fast when an unknown model is given with no matching provider.

Bug (#93412), reproduced live: ``clover -z -m gemini-3.8-pro`` ran the whole
job on claude-opus-5-5 with no warning and exit 0. Two guards close this:

  1. A preflight in ``_run_agent`` -- when ``-m`` is given without
     ``--provider`` and ``detect_provider_for_model`` finds no match, and
     the resolved provider's cached/static model list doesn't contain the
     requested model, fail fast with a clear message and close suggestions
     (no API call spent discovering it).
  2. A mid-turn pinned model_not_found result (``pinned_model_unavailable``
     in the run_conversation result) is routed to stderr with a non-zero
     exit, not printed to stdout as if it were a normal answer.

Both are exercised via subprocess (like test_oneshot_surrogate.py) because
``run_oneshot`` redirects the real stdout/stderr for its whole call tree.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run(program: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_pinned_model_unavailable_mid_turn_writes_stderr_and_exits_nonzero():
    program = textwrap.dedent(
        """
        import clover_cli.oneshot as oneshot

        msg = (
            "Model 'gemini-3.8-pro' isn't available on provider "
            "'openai-codex'. Nothing was run on another model."
        )
        oneshot._run_agent = lambda *a, **kw: (
            msg,
            {"final_response": msg, "failed": True, "error": msg,
             "pinned_model_unavailable": True},
        )
        raise SystemExit(oneshot.run_oneshot("hello", model="gemini-3.8-pro"))
        """
    )
    result = _run(program)
    assert result.returncode == 2, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b""
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert "gemini-3.8-pro" in stderr
    assert "openai-codex" in stderr
    assert "Nothing was run on another model" in stderr


def test_unpinned_success_is_unaffected_by_the_new_result_key():
    """Sanity check the new branch is additive: a normal successful run
    (no pinned_model_unavailable key at all) still prints to stdout and
    exits 0, exactly as before."""
    program = textwrap.dedent(
        """
        import clover_cli.oneshot as oneshot

        oneshot._run_agent = lambda *a, **kw: (
            "the answer",
            {"final_response": "the answer", "failed": False, "completed": True},
        )
        raise SystemExit(oneshot.run_oneshot("hello"))
        """
    )
    result = _run(program)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b"the answer\n"


def test_unknown_model_without_provider_fails_fast_with_suggestions():
    """The -m preflight: detect_provider_for_model finds nothing, the
    resolved (default) provider has a static catalog, and the requested
    model isn't in it -- fail before any API call, with close-match
    suggestions from that same catalog."""
    program = textwrap.dedent(
        """
        from unittest.mock import patch
        import clover_cli.oneshot as oneshot

        with (
            patch("clover_cli.models.detect_provider_for_model", return_value=None),
            patch(
                "clover_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "k", "base_url": "https://chatgpt.com/backend-api/codex",
                    "provider": "openai-codex", "requested_provider": "openai-codex",
                    "api_mode": "codex_responses", "credential_pool": None,
                },
            ),
        ):
            raise SystemExit(
                oneshot.run_oneshot("hello", model="gemini-3.8-pro")
            )
        """
    )
    result = _run(program)
    assert result.returncode == 2, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b""
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert "gemini-3.8-pro" in stderr
    assert "openai-codex" in stderr
    assert "Nothing was run on another model" in stderr
    assert "clover -z: agent failed:" not in stderr
