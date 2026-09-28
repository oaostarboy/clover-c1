"""Oneshot (-z) mode: send a prompt, get the final content block, exit.

Bypasses cli.py entirely.  No banner, no spinner, no session_id line,
no stderr chatter.  Just the agent's final text to stdout.

Toolsets = explicit --toolsets when provided, otherwise whatever the user has
configured for "cli" in `clover tools`.
Rules / memory / AGENTS.md / preloaded skills = same as a normal chat turn.
Approvals = auto-bypassed (CLOVER_YOLO_MODE=1 is set for the call).
Working directory = the user's CWD (AGENTS.md etc. resolve from there as usual).

Model / provider selection mirrors `clover chat`:
    - Both optional. If omitted, use the user's configured default.
    - If both given, pair them exactly as given.
    - If only --model given, auto-detect the provider that serves it.
    - If only --provider given, error out (ambiguous — caller must pick a model).

Env var fallbacks (used when the corresponding arg is not passed):
    - CLOVER_INFERENCE_MODEL
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional

from gateway.session_context import declare_stateless_channel
from clover_cli.fallback_config import get_fallback_chain


class ModelNotAvailableError(ValueError):
    """Raised by the -m preflight check: the requested model is not in the
    resolved provider's known catalog and no fallback was attempted."""


def _normalize_toolsets(toolsets: object = None) -> list[str] | None:
    if not toolsets:
        return None

    raw_items = [toolsets] if isinstance(toolsets, str) else toolsets
    if not isinstance(raw_items, (list, tuple)):
        raw_items = [raw_items]

    normalized: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            normalized.extend(part.strip() for part in item.split(","))
        else:
            normalized.append(str(item).strip())

    return [item for item in normalized if item] or None


def _normalize_skills(skills: object = None) -> list[str]:
    """Normalize repeated/comma-separated skill flags and preserve order."""
    normalized = _normalize_toolsets(skills) or []
    return list(dict.fromkeys(normalized))


def _build_preloaded_skills_prompt(skills: object = None) -> str | None:
    """Load requested skills using the same partial-success contract as CLI chat."""
    parsed_skills = _normalize_skills(skills)
    if not parsed_skills:
        return None

    from agent.skill_commands import build_preloaded_skills_prompt

    skills_prompt, loaded_skills, missing_skills = build_preloaded_skills_prompt(
        parsed_skills
    )
    if missing_skills:
        missing_display = ", ".join(missing_skills)
        if loaded_skills:
            logging.warning(
                "Unknown skill(s) requested, skipping: %s. Continuing with: %s. "
                "List available skills with `clover skills list`.",
                missing_display,
                ", ".join(loaded_skills),
            )
        else:
            raise ValueError(f"Unknown skill(s): {missing_display}")

    return skills_prompt or None


def _validate_explicit_toolsets(toolsets: object = None) -> tuple[list[str] | None, str | None]:
    normalized = _normalize_toolsets(toolsets)
    if normalized is None:
        return None, None

    try:
        from toolsets import validate_toolset
    except Exception as exc:
        return None, f"clover -z: failed to validate --toolsets: {exc}\n"

    built_in = [name for name in normalized if validate_toolset(name)]
    unresolved = [name for name in normalized if name not in built_in]

    if unresolved:
        try:
            from clover_cli.plugins import discover_plugins

            discover_plugins()
            plugin_valid = [name for name in unresolved if validate_toolset(name)]
        except Exception:
            plugin_valid = []

        if plugin_valid:
            built_in.extend(plugin_valid)
            unresolved = [name for name in unresolved if name not in plugin_valid]

    if any(name in {"all", "*"} for name in built_in):
        ignored = [name for name in normalized if name not in {"all", "*"}]
        if ignored:
            sys.stderr.write(
                "clover -z: --toolsets all enables every toolset; "
                f"ignoring additional entries: {', '.join(ignored)}\n"
            )
        return None, None

    mcp_names: set[str] = set()
    mcp_disabled: set[str] = set()
    if unresolved:
        try:
            from clover_cli.config import read_raw_config
            from clover_cli.tools_config import _parse_enabled_flag

            cfg = read_raw_config()
            mcp_servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
            for name, server_cfg in mcp_servers.items():
                if not isinstance(server_cfg, dict):
                    continue
                if _parse_enabled_flag(server_cfg.get("enabled", True), default=True):
                    mcp_names.add(str(name))
                else:
                    mcp_disabled.add(str(name))
        except Exception:
            mcp_names = set()
            mcp_disabled = set()

    mcp_valid = [name for name in unresolved if name in mcp_names]
    disabled = [name for name in unresolved if name in mcp_disabled]
    unknown = [name for name in unresolved if name not in mcp_names and name not in mcp_disabled]
    valid = built_in + mcp_valid

    if unknown:
        sys.stderr.write(f"clover -z: ignoring unknown --toolsets entries: {', '.join(unknown)}\n")
    if disabled:
        sys.stderr.write(
            "clover -z: ignoring disabled MCP servers (set enabled: true in config.yaml to use): "
            f"{', '.join(disabled)}\n"
        )

    if not valid:
        return None, "clover -z: --toolsets did not contain any valid toolsets.\n"

    return valid, None


def _write_usage_file(path: Optional[str], result: dict, failure: Optional[str] = None) -> None:
    """Best-effort JSON usage report for pipelines (``-z --usage-file``).

    Written even on failure so callers can always account for spend. Never
    raises — a broken usage write must not mask the run's own outcome.
    """
    if not path:
        return
    try:
        import json

        report = {
            "estimated_cost_usd": result.get("estimated_cost_usd"),
            "cost_status": result.get("cost_status"),
            "cost_source": result.get("cost_source"),
            "input_tokens": result.get("input_tokens"),
            "output_tokens": result.get("output_tokens"),
            "cache_read_tokens": result.get("cache_read_tokens"),
            "cache_write_tokens": result.get("cache_write_tokens"),
            "reasoning_tokens": result.get("reasoning_tokens"),
            "total_tokens": result.get("total_tokens"),
            "api_calls": result.get("api_calls"),
            "model": result.get("model"),
            "provider": result.get("provider"),
            # requested_model/actual_model contract (#93412 follow-up): when
            # a pinned model that doesn't exist got silently substituted for
            # a real one, pipelines auditing spend need both names, not just
            # the one that ended up running.
            "model_substituted": bool(result.get("model_substituted")),
            "requested_model": result.get("requested_model"),
            "actual_model": result.get("actual_model"),
            "session_id": result.get("session_id"),
            "completed": result.get("completed"),
            "failed": bool(result.get("failed")) or failure is not None,
            # Billing-audit field: the service tier this run REQUESTED via
            # request_overrides.extra_body (e.g. OpenAI "flex"). None when
            # unset. Lets batch pipelines verify the tier they think they're
            # paying for actually went out on the wire (July 2026 incident:
            # a config-matching bug silently dropped flex -> 2.3x billing).
            "service_tier": result.get("service_tier"),
        }
        if failure is not None:
            report["failure"] = failure
        out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def run_oneshot(
    prompt: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: object = None,
    skills: object = None,
    usage_file: Optional[str] = None,
    activity_events: bool = False,
) -> int:
    """Execute a single prompt and print only the final content block.

    Args:
        prompt: The user message to send.
        model: Optional model override. Falls back to CLOVER_INFERENCE_MODEL
            env var, then config.yaml's model.default / model.model.
        provider: Optional provider override. Falls back to config.yaml's
            model.provider, then "auto".
        toolsets: Optional comma-separated string or iterable of toolsets.
        skills: Optional repeated/comma-separated skill identifiers to preload.
        usage_file: Optional path; when set, a JSON usage report (estimated
            cost, token counts, model, api_calls) is written there after the
            run — even when the run fails — so pipelines can account for
            spend per invocation.

    Returns the exit code.  The caller owns process termination.
    """
    # Silence every stdlib logger for the duration.  AIAgent, tools, and
    # provider adapters all log to stderr through the root logger; file
    # handlers added by setup_logging() keep working (they're attached to
    # the root logger's handler list, not affected by level), but no
    # bytes reach the terminal.
    logging.disable(logging.CRITICAL)

    # --provider without --model is ambiguous: carrying the user's configured
    # model across to a different provider is usually wrong (that provider may
    # not host it), and silently picking the provider's catalog default hides
    # the mismatch.  Require the caller to be explicit.  Validate BEFORE the
    # stderr redirect so the message actually reaches the terminal.
    env_model_early = os.getenv("CLOVER_INFERENCE_MODEL", "").strip()
    if provider and not ((model or "").strip() or env_model_early):
        sys.stderr.write(
            "clover -z: --provider requires --model (or CLOVER_INFERENCE_MODEL). "
            "Pass both explicitly, or neither to use your configured defaults.\n"
        )
        return 2

    explicit_toolsets, toolsets_error = _validate_explicit_toolsets(toolsets)
    if toolsets_error:
        sys.stderr.write(toolsets_error)
        return 2
    use_config_toolsets = _normalize_toolsets(toolsets) is None

    # Auto-approve any shell / tool approvals.  Non-interactive by
    # definition — a prompt would hang forever.
    os.environ["CLOVER_YOLO_MODE"] = "1"
    os.environ["CLOVER_ACCEPT_HOOKS"] = "1"

    # One-shot prints a single final response and exits: there is no later turn
    # for a detached subagent's completion to re-enter, and nothing here drains
    # process_registry.completion_queue (only cli.py's interactive process_loop
    # and the gateway watchers do). Left unbound, async_delivery_supported()
    # defaults True, delegate_task is forced background, and every subagent
    # result is discarded. Declaring the channel stateless routes delegate_task
    # to its inline/synchronous path. See declare_stateless_channel().
    declare_stateless_channel()

    # Redirect stderr AND stdout to devnull for the entire call tree.
    # We'll print the final response to the real stdout at the end.
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    devnull = open(os.devnull, "w", encoding="utf-8")
    # --activity-events: structured worker activity on the REAL stderr (the
    # call tree's stderr goes to devnull). See clover_cli/activity_events.py.
    activity_writer = None
    if activity_events:
        from clover_cli.activity_events import ActivityEventWriter

        activity_writer = ActivityEventWriter(real_stderr)

    response: Optional[str] = None
    result: dict = {}
    failure: BaseException | None = None
    try:
        with redirect_stdout(devnull), redirect_stderr(devnull):
            try:
                response, result = _run_agent(
                    prompt,
                    model=model,
                    provider=provider,
                    toolsets=explicit_toolsets,
                    use_config_toolsets=use_config_toolsets,
                    skills=skills,
                    activity_writer=activity_writer,
                )
            except BaseException as exc:  # noqa: BLE001
                # Capture anything that escapes the agent (including OSError
                # from prompt_toolkit/Vt100 when stdout is a non-TTY pipe,
                # KeyboardInterrupt, SystemExit, etc.) so we can surface it on
                # the real stderr instead of crashing past the redirect with a
                # traceback that the caller never sees. A silent exit in a
                # cron / SSH / subprocess context is the worst failure mode.
                # See #30623.
                failure = exc
    finally:
        try:
            devnull.close()
        except Exception:
            pass

    if activity_writer is not None:
        _ok = failure is None and bool((response or "").strip()) and not result.get("failed")
        _status = "completed" if _ok else "failed"
        if _ok:
            # Still stopped early after every allowed resume: say so, so the
            # parent's card shows "unfinished" instead of a false "done".
            from agent.step_continuation import needs_continuation

            if needs_continuation(result) is not None:
                _status = "incomplete"
        activity_writer.result(response if _ok else "", _status)

    if failure is not None:
        # Re-raise control-flow exceptions so the parent handles them as usual
        # (Ctrl-C / explicit sys.exit() inside the agent).
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            _write_usage_file(usage_file, result, failure=repr(failure))
            raise failure
        _write_usage_file(usage_file, result, failure=str(failure))
        if isinstance(failure, ModelNotAvailableError):
            # The -m preflight check — a plain, direct message with no
            # "agent failed:" framing (nothing ran; there's no agent failure
            # to report).
            real_stderr.write(f"clover -z: {failure}\n")
            real_stderr.flush()
            return 2
        real_stderr.write(f"clover -z: agent failed: {failure}\n")
        real_stderr.flush()
        return 1

    # A pinned model (-m) hit model_not_found mid-turn: the conversation loop
    # aborted instead of silently walking the fallback chain (#93412). Route
    # the message to stderr with a non-zero exit — same contract as the
    # --provider-without-model preflight above — instead of printing it to
    # stdout as if it were a normal (if failed) answer.
    if result.get("pinned_model_unavailable"):
        _pinned_msg = (response or result.get("error") or "").strip() or (
            "Model is pinned and unavailable on this provider; nothing was "
            "run on another model."
        )
        real_stderr.write(f"clover -z: {_pinned_msg}\n")
        real_stderr.flush()
        return 2

    if result.get("model_substituted"):
        # A pinned model that doesn't exist ran on a substitute instead of
        # stopping (#93412 follow-up). Exit 0 -- the job DID run -- but the
        # stderr line still names both models so the caller isn't left
        # thinking their requested model actually answered.
        real_stderr.write(
            f"clover -z: ⚠ '{result.get('requested_model')}' doesn't exist "
            f"on {result.get('requested_provider')}, so I used "
            f"{result.get('actual_model')} instead.\n"
        )
        real_stderr.flush()

    _write_usage_file(usage_file, result)

    # Model text can contain lone UTF-16 surrogates (invalid in UTF-8). Writing
    # those to a real stdout TextIO raises UnicodeEncodeError and aborts with
    # exit 1 after the turn already completed — scrub to U+FFFD first.
    # See #80366.
    if response:
        from agent.message_sanitization import _sanitize_surrogates

        response = _sanitize_surrogates(response)

    if response:
        real_stdout.write(response)
        if not response.endswith("\n"):
            real_stdout.write("\n")
        real_stdout.flush()

    if (result.get("failed") or result.get("partial")) and not (response or "").strip():
        return 2

    if not (response or "").strip():
        real_stderr.write("clover -z: no final response was produced; treating the run as failed.\n")
        real_stderr.flush()
        return 1

    return 0


def _create_session_db_for_oneshot():
    """Best-effort SessionDB for ``clover -z`` / oneshot mode.

    Oneshot bypasses ``CloverCLI._init_agent()``, so it must wire the SQLite
    session store itself. Without this, the ``session_search``/recall tool is
    advertised but every call returns "Session database not available.".
    """
    try:
        from clover_state import SessionDB

        return SessionDB()
    except Exception as exc:
        logging.debug("SQLite session store not available for oneshot mode: %s", exc)
        return None


def _run_agent(
    prompt: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: object = None,
    use_config_toolsets: bool = True,
    skills: object = None,
    activity_writer: object = None,
) -> tuple[str, dict]:
    """Build an AIAgent exactly like a normal CLI chat turn would, then
    run a single conversation.  Returns ``(final_response, run_result)``."""
    # Imports are local so they don't run when clover is invoked for
    # other commands (keeps top-level CLI startup cheap).
    from clover_cli.config import load_config
    from clover_cli.models import detect_provider_for_model
    from clover_cli.runtime_provider import resolve_runtime_provider
    from clover_cli.tools_config import _get_platform_tools
    from run_agent import AIAgent

    cfg = load_config()

    # Resolve effective model: explicit arg → env var → config.
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        cfg_model = model_cfg
    else:
        _raw = model_cfg.get("default") or model_cfg.get("model") or ""
        if isinstance(_raw, dict):
            from clover_cli.config import split_model_config_default
            cfg_model, _ = split_model_config_default(_raw)
        else:
            cfg_model = str(_raw or "")

    env_model = os.getenv("CLOVER_INFERENCE_MODEL", "").strip()
    effective_model = (model or "").strip() or env_model or cfg_model

    # Resolve effective provider: explicit arg → (auto-detect from model if
    # model was explicit) → env / config (handled inside resolve_runtime_provider).
    #
    # When --model is given without --provider, auto-detect the provider that
    # serves that model — same semantic as `/model <name>` in an interactive
    # session.  Without this, resolve_runtime_provider() would fall back to
    # the user's configured default provider, which may not host the model
    # the caller just asked for.
    effective_provider = (provider or "").strip() or None
    explicit_base_url_from_alias: Optional[str] = None
    if effective_provider is None and (model or env_model):
        # Only auto-detect when the model was explicitly requested via arg or
        # env var (not when it came from config — that's the "use my defaults"
        # path and the configured provider is already correct).
        explicit_model = (model or "").strip() or env_model
        if explicit_model:
            # First check DIRECT_ALIASES populated from config.yaml `model_aliases:`.
            # These map a user-defined alias to (model, provider, base_url) for
            # endpoints not in any catalog (local servers, custom proxies, etc.).
            try:
                from clover_cli import model_switch as _ms
                _ms._ensure_direct_aliases()
                direct = _ms.DIRECT_ALIASES.get(explicit_model.strip().lower())
            except Exception:
                direct = None
            if direct is not None:
                effective_model = direct.model
                effective_provider = direct.provider
                if direct.base_url:
                    explicit_base_url_from_alias = direct.base_url.rstrip("/")
            else:
                cfg_provider = ""
                if isinstance(model_cfg, dict):
                    cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
                current_provider = (
                    cfg_provider
                    or os.getenv("CLOVER_INFERENCE_PROVIDER", "").strip().lower()
                    or "auto"
                )
                detected = detect_provider_for_model(explicit_model, current_provider)
                if detected:
                    effective_provider, effective_model = detected

    runtime = resolve_runtime_provider(
        requested=effective_provider,
        target_model=effective_model or None,
        explicit_base_url=explicit_base_url_from_alias,
    )

    # Preflight: -m/--model given without --provider, and detect_provider_for_model
    # found no confident match (effective_provider is still None), so the run fell
    # through to the caller's default provider. If that provider's model name is
    # simply unknown to Clover's own static catalog (no network probe — a typo'd
    # model would otherwise run the whole job on whatever provider the fallback
    # chain happens to land on with no visible warning, #93412), substitute
    # instead of spending a real API call to discover the failure: the closest
    # real model on the same provider, then the user's own configured default
    # (model.substitute_unknown: false keeps the old fail-fast behavior).
    _model_substitution_info: Optional[dict] = None
    if (model or "").strip() and not (provider or "").strip() and effective_provider is None:
        from clover_cli.models import _PROVIDER_MODELS

        _resolved_provider = str(runtime.get("provider") or "").strip().lower()
        _known_models = _PROVIDER_MODELS.get(_resolved_provider) or []
        if _known_models and effective_model not in _known_models:
            from agent.model_substitute import (
                configured_default_model,
                resolve_model_substitute,
                substitute_unknown_models_enabled,
            )

            _substitute = None
            if substitute_unknown_models_enabled(cfg):
                _default_model, _default_provider = configured_default_model(cfg)
                _substitute = resolve_model_substitute(
                    effective_model, _resolved_provider,
                    known_models=_known_models,
                    default_model=_default_model,
                    default_provider=_default_provider,
                )
            if _substitute is not None:
                logging.getLogger(__name__).warning(
                    "Model substitute activated: %s (%s) → %s (%s); "
                    "reason=model_not_found_substituted",
                    effective_model, _resolved_provider,
                    _substitute.model, _substitute.provider,
                )
                _model_substitution_info = {
                    "requested_model": effective_model,
                    "requested_provider": _resolved_provider,
                    "actual_model": _substitute.model,
                    "actual_provider": _substitute.provider,
                }
                effective_model = _substitute.model
                if _substitute.provider != _resolved_provider:
                    effective_provider = _substitute.provider
                    runtime = resolve_runtime_provider(
                        requested=effective_provider,
                        target_model=effective_model,
                        explicit_base_url=explicit_base_url_from_alias,
                    )
            else:
                from difflib import get_close_matches

                _suggestions = get_close_matches(effective_model, _known_models, n=5, cutoff=0.4)
                _msg = (
                    f"Model '{effective_model}' isn't available on provider "
                    f"'{_resolved_provider}'. Nothing was run on another model."
                )
                if _suggestions:
                    _msg += " Did you mean: " + ", ".join(_suggestions) + "?"
                raise ModelNotAvailableError(_msg)

    # Pull in explicit toolsets when provided; otherwise use whatever the user
    # has enabled for "cli". sorted() gives stable ordering for config-derived
    # sets; explicit values preserve user order.
    toolsets_list = _normalize_toolsets(toolsets)
    if toolsets_list is None and use_config_toolsets:
        toolsets_list = sorted(_get_platform_tools(cfg, "cli"))

    # Ensure MCP tools are discovered before building the agent.  Oneshot
    # bypasses cli.py's _prepare_agent_startup MCP background path and
    # CloverCLI._init_agent's wait — it builds AIAgent directly here, so the
    # tool snapshot at construction time misses any MCP server that hasn't
    # registered yet.  This helper starts discovery if needed (idempotent) and
    # bounded-waits with the larger single-query bound (default 15s) because
    # there is only ONE turn and no between-turns late-binding refresh (#38448).
    from clover_cli.mcp_startup import ensure_mcp_discovery_before_agent_build

    ensure_mcp_discovery_before_agent_build(
        logger=logging.getLogger(__name__),
        single_query=True,
    )

    skills_prompt = _build_preloaded_skills_prompt(skills)

    session_db = _create_session_db_for_oneshot()
    # The try spans agent construction (not just ``chat``) so the SQLite store
    # opened above is always closed — including when ``AIAgent(...)`` itself
    # raises on a provider/config error. The one-shot exit path hard-exits via
    # os._exit and skips finalizers, so an un-closed connection here would leak.
    agent = None
    try:
        # Read the effective fallback chain from profile config so oneshot
        # workers honour the same merge semantics as interactive CLI and
        # gateway sessions.
        _fb = get_fallback_chain(cfg)

        agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_mode=runtime.get("api_mode"),
            model=effective_model,
            # Only "true" when the caller explicitly passed -m/--model — never
            # for a model that came from CLOVER_INFERENCE_MODEL or config.yaml
            # (those are "use my defaults", not "pin this exact model").
            model_pinned=bool((model or "").strip()),
            enabled_toolsets=toolsets_list,
            quiet_mode=True,
            platform="cli",
            session_db=session_db,
            credential_pool=runtime.get("credential_pool"),
            fallback_model=_fb or None,
            ephemeral_system_prompt=skills_prompt,
            # Interactive callbacks are intentionally NOT wired beyond this
            # one.  In oneshot mode there's no user sitting at a terminal:
            #   - clarify  → returns a synthetic "pick a default" instruction
            #                so the agent continues instead of stalling on
            #                the tool's built-in "not available" error
            #   - sudo password prompt → terminal_tool gates on
            #                CLOVER_INTERACTIVE which we never set
            #   - shell-hook approval → auto-approved via CLOVER_ACCEPT_HOOKS=1
            #                (set above); also falls back to deny on non-tty
            #   - dangerous-command approval → bypassed via CLOVER_YOLO_MODE=1
            #   - skill secret capture → returns gracefully when no callback set
            clarify_callback=_oneshot_clarify_callback,
        )

        # Belt-and-braces: make sure AIAgent doesn't invoke any streaming
        # display callbacks that would bypass our stdout capture.
        agent.suppress_status_output = True
        agent.stream_delta_callback = None
        agent.tool_gen_callback = None
        if _model_substitution_info is not None:
            # Durable record for the requested_model/actual_model result
            # contract, and the same-substitution guard conversation_loop.py's
            # pinned branch checks before trying a second substitute.
            agent._model_substitution = _model_substitution_info
        if activity_writer is not None:
            # Structured, redacted worker activity for a parent session.
            agent.tool_progress_callback = activity_writer.tool_progress_callback
            agent.interim_assistant_callback = activity_writer.interim_callback
            agent.model_fallback_callback = activity_writer.model_fallback
            activity_writer.start(effective_model)
            if _model_substitution_info is not None:
                activity_writer.model_fallback(
                    from_model=_model_substitution_info["requested_model"],
                    from_provider=_model_substitution_info["requested_provider"],
                    to_model=_model_substitution_info["actual_model"],
                    to_provider=_model_substitution_info["actual_provider"],
                    reason="model_not_found_substituted",
                )
        if _model_substitution_info is not None:
            agent._emit_status(
                f"⚠ '{_model_substitution_info['requested_model']}' doesn't "
                f"exist on {_model_substitution_info['requested_provider']}, "
                f"so I used {_model_substitution_info['actual_model']} instead."
            )

        result = agent.run_conversation(prompt)
        # Keep a worker going when it stops early: out of steps, or quit
        # with steps left while admitting the job isn't done
        # (delegation.auto_continue, default 2).
        try:
            from agent.step_continuation import (
                auto_continue_limit,
                continue_until_done,
                needs_continuation,
            )

            _limit = auto_continue_limit((cfg.get("delegation") or {}) if isinstance(cfg, dict) else {})
            if _limit and needs_continuation(result, agent):
                def _leg(message, history):
                    return agent.run_conversation(message, conversation_history=history)

                def _note(kind, n, lim):
                    if activity_writer is not None:
                        label = "out of steps" if kind == "budget" else "not finished"
                        activity_writer.interim_callback(f"{label}, continuing ({n}/{lim})")

                result = continue_until_done(agent, result, limit=_limit, run=_leg, on_continue=_note)
        except Exception:
            logging.debug("oneshot auto-continue failed", exc_info=True)
        return (result.get("final_response") or "", result)
    finally:
        # Ordering deliberately mirrors gateway/run.py:_cleanup_agent_resources,
        # NOT cli.py:_run_cleanup — oneshot has no _active_agent_ref and must
        # close the agent explicitly because the hard-exit path skips finalizers.
        if agent is not None:
            # Linger (bounded) for background processes this turn spawned with
            # notify_on_complete=true BEFORE agent.close(): close() calls
            # process_registry.kill_all(task_id) and the dying parent owns the
            # children's stdout pipes, so exiting now destroys in-flight
            # deliveries — including Bot Mode handoff replies dispatched from
            # a short-lived recipient (#90879).
            try:
                from tools.process_registry import process_registry

                process_registry.wait_for_pending_completions(None)
            except Exception:
                logging.debug("oneshot background completion wait failed", exc_info=True)
            try:
                session_messages = getattr(agent, "_session_messages", None)
                if isinstance(session_messages, list):
                    agent.shutdown_memory_provider(session_messages)
                else:
                    agent.shutdown_memory_provider()
            except Exception:
                logging.debug("oneshot memory/context cleanup failed", exc_info=True)
            try:
                agent.close()
            except Exception:
                logging.debug("oneshot agent cleanup failed", exc_info=True)
        # agent.close() calls session_db.end_session() but leaves the connection
        # open; close it here to checkpoint the WAL before os._exit skips
        # finalizers.
        if session_db is not None:
            try:
                session_db.close()
            except Exception:
                logging.debug("oneshot session store cleanup failed", exc_info=True)


def _oneshot_clarify_callback(question: str, choices=None, multi_select=False) -> str:
    """Clarify is disabled in oneshot mode — tell the agent to pick a
    default and proceed instead of stalling or erroring."""
    if choices:
        if multi_select:
            return (
                f"[oneshot mode: no user available. Pick the best subset from "
                f"{choices} using your own judgment and continue.]"
            )
        return (
            f"[oneshot mode: no user available. Pick the best option from "
            f"{choices} using your own judgment and continue.]"
        )
    return (
        "[oneshot mode: no user available. Make the most reasonable "
        "assumption you can and continue.]"
    )
