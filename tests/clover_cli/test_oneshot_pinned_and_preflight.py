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


def test_preflight_substitutes_closest_same_provider_model_and_exits_zero():
    """Case (a): the resolved provider's own LIVE catalog (fetched right
    now from the provider itself -- here, Codex's own live model list) has
    a close enough match (cutoff ~0.6) -- run on it instead of failing,
    exit 0, and name both models on stderr (#93412 follow-up).

    A STATIC-only catalog must NOT trigger this preflight substitution --
    see ``test_preflight_skips_static_only_openai_codex_catalog`` below,
    which reproduces the actual #93412-round-2 regression this guards: a
    live, working model missing from Clover's static list must never be
    silently swapped out."""
    program = textwrap.dedent(
        """
        import sys
        from unittest.mock import MagicMock, patch
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
            # No static entry at all -- the match must come from the LIVE
            # fetch below, not a coincidentally-present static list.
            patch("clover_cli.models._PROVIDER_MODELS", {}),
            patch(
                "clover_cli.auth.resolve_codex_runtime_credentials",
                return_value={"api_key": "test-token"},
            ),
            patch(
                "clover_cli.codex_models._fetch_models_from_api",
                return_value=["gpt-5.3-codex-real"],
            ),
            patch("run_agent.AIAgent") as MockAgent,
        ):
            MockAgent.return_value.run_conversation.return_value = {
                "final_response": "ok", "failed": False, "completed": True,
            }
            code = oneshot.run_oneshot("hello", model="gpt-5.3-codex-typo")
            # Proof the PREFLIGHT (not just result-surfacing) substituted:
            # AIAgent itself must have been constructed with the real model.
            print(
                "ACTUAL_MODEL=" + str(MockAgent.call_args.kwargs.get("model")),
                file=sys.__stderr__,
            )
            raise SystemExit(code)
        """
    )
    result = _run(program)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b"ok\n"
    stderr = result.stderr.decode("utf-8", errors="replace")
    # AIAgent is mocked, so its ``_emit_status`` warning never reaches real
    # stderr -- the only durable, process-external proof the preflight
    # substituted is what model AIAgent was actually constructed with.
    assert "ACTUAL_MODEL=gpt-5.3-codex-real" in stderr


def test_preflight_substitute_unknown_false_keeps_old_fail_fast_behavior():
    """``model.substitute_unknown: false`` opts back into the original
    stop-with-error contract even when a close same-provider match exists
    on a LIVE catalog."""
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
            patch("clover_cli.models._PROVIDER_MODELS", {}),
            patch(
                "clover_cli.auth.resolve_codex_runtime_credentials",
                return_value={"api_key": "test-token"},
            ),
            patch(
                "clover_cli.codex_models._fetch_models_from_api",
                return_value=["gpt-5.3-codex-real"],
            ),
            patch(
                "clover_cli.config.load_config",
                return_value={"model": {"substitute_unknown": False}},
            ),
        ):
            raise SystemExit(
                oneshot.run_oneshot("hello", model="gpt-5.3-codex-typo")
            )
        """
    )
    result = _run(program)
    assert result.returncode == 2, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b""
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert "gpt-5.3-codex-typo" in stderr
    assert "Nothing was run on another model" in stderr


def test_preflight_skips_static_only_openai_codex_catalog():
    """The #93412-round-2 regression, reproduced directly: Clover's static
    ``_PROVIDER_MODELS`` catalog lags a provider's real, working model
    list. A static-only catalog (no live Codex credentials/fetch available)
    is NEVER proof a model doesn't exist -- the preflight must skip
    entirely and let the real call decide, instead of "substituting away"
    a model that actually works."""
    program = textwrap.dedent(
        """
        import sys
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
            # Stale static list: missing the real model the user asked for.
            patch(
                "clover_cli.models._PROVIDER_MODELS",
                {"openai-codex": ["gpt-5.6-terra-900k"]},
            ),
            # No usable Codex credentials -- the live fetch can't run, so
            # the only catalog available is the static one above.
            patch(
                "clover_cli.auth.resolve_codex_runtime_credentials",
                return_value={},
            ),
            patch("run_agent.AIAgent") as MockAgent,
        ):
            MockAgent.return_value.run_conversation.return_value = {
                "final_response": "hi", "failed": False, "completed": True,
            }
            code = oneshot.run_oneshot("hello", model="gpt-6-astra-900k")
            print(
                "ACTUAL_MODEL=" + str(MockAgent.call_args.kwargs.get("model")),
                file=sys.__stderr__,
            )
            raise SystemExit(code)
        """
    )
    result = _run(program)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b"hi\n"
    stderr = result.stderr.decode("utf-8", errors="replace")
    # The real model must reach AIAgent UNCHANGED -- the static list never
    # gets a say, and there is no "isn't available" preflight message.
    assert "ACTUAL_MODEL=gpt-6-astra-900k" in stderr
    assert "isn't available on provider" not in stderr


def test_preflight_uses_dynamic_models_for_custom_provider_without_static_catalog():
    """A custom/user-defined provider (billing class ``"custom"``) has no
    ``_PROVIDER_MODELS`` entry -- the preflight must probe the endpoint's
    own ``/models`` instead of treating the missing static list as "no
    catalog" and skipping the same-provider substitute check entirely
    (#93412 follow-up). ``AIAgent`` is mocked, so ``sys.__stderr__`` (which
    survives oneshot's internal stdout/stderr capture) is used to report
    what model it was actually constructed with."""
    program = textwrap.dedent(
        """
        import sys
        from unittest.mock import patch
        import clover_cli.oneshot as oneshot

        with (
            patch("clover_cli.models.detect_provider_for_model", return_value=None),
            patch(
                "clover_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "k", "base_url": "http://127.0.0.1:8317/v1",
                    "provider": "custom", "requested_provider": "gemini-oauth",
                    "api_mode": "chat_completions", "credential_pool": None,
                },
            ),
            patch("clover_cli.models._PROVIDER_MODELS", {}),
            patch(
                "providers.base.ProviderProfile.fetch_models",
                return_value=["gemini-3.8-flash-high", "gemini-3.1-pro-low"],
            ),
            patch("run_agent.AIAgent") as MockAgent,
        ):
            MockAgent.return_value.run_conversation.return_value = {
                "final_response": "ok", "failed": False, "completed": True,
            }
            code = oneshot.run_oneshot("hello", model="gemini-3.8-pro")
            print(
                "ACTUAL_MODEL=" + str(MockAgent.call_args.kwargs.get("model")),
                file=sys.__stderr__,
            )
            raise SystemExit(code)
        """
    )
    result = _run(program)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b"ok\n"
    stderr = result.stderr.decode("utf-8", errors="replace")
    # The dynamically-fetched catalog contains a same-family+version match
    # ("gemini-3.8-flash-high" shares "gemini-3.8" with the typo'd
    # "gemini-3.8-pro") -- the preflight must have substituted onto it
    # instead of skipping straight past an (incorrectly) empty catalog.
    assert "ACTUAL_MODEL=gemini-3.8-flash-high" in stderr, stderr


def test_unknown_model_without_provider_fails_fast_with_suggestions():
    """The -m preflight: detect_provider_for_model finds nothing, the
    resolved (default) provider has a LIVE catalog (fetched right now from
    the provider), and the requested model isn't in it -- fail before any
    API call, with close-match suggestions from that same catalog. A
    static-only catalog must NOT fail fast this way -- see
    ``test_unknown_model_reaches_runtime_when_only_static_catalog_exists``."""
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
            patch("clover_cli.models._PROVIDER_MODELS", {}),
            patch(
                "clover_cli.auth.resolve_codex_runtime_credentials",
                return_value={"api_key": "test-token"},
            ),
            patch(
                "clover_cli.codex_models._fetch_models_from_api",
                return_value=["gpt-5.3-codex-real"],
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


def test_unknown_model_reaches_runtime_when_only_static_catalog_exists():
    """Companion to the fail-fast case above: with ONLY a static catalog
    available (no live Codex credentials, so no live fetch), the preflight
    must skip entirely -- AIAgent is constructed with the model UNCHANGED,
    and the (here, mocked) runtime model_not_found path is what decides,
    not a bare static-list membership check (#93412 follow-up round 2)."""
    program = textwrap.dedent(
        """
        import sys
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
            patch(
                "clover_cli.auth.resolve_codex_runtime_credentials",
                return_value={},
            ),
            patch("run_agent.AIAgent") as MockAgent,
        ):
            msg = (
                "Model 'gemini-3.8-pro' isn't available on provider "
                "'openai-codex'. Nothing was run on another model."
            )
            MockAgent.return_value.run_conversation.return_value = {
                "final_response": msg, "failed": True, "error": msg,
                "pinned_model_unavailable": True,
            }
            code = oneshot.run_oneshot("hello", model="gemini-3.8-pro")
            print(
                "ACTUAL_MODEL=" + str(MockAgent.call_args.kwargs.get("model")),
                file=sys.__stderr__,
            )
            raise SystemExit(code)
        """
    )
    result = _run(program)
    assert result.returncode == 2, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b""
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert "gemini-3.8-pro" in stderr
    assert "openai-codex" in stderr
    assert "Nothing was run on another model" in stderr
    # Preflight never touched the model -- it reached AIAgent unchanged and
    # the (mocked) runtime path produced this exact message.
    assert "ACTUAL_MODEL=gemini-3.8-pro" in stderr
