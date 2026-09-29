"""Pick a substitute when a pinned model doesn't exist on its provider.

Order (stop at the first hit; see #93412 follow-up):
  1. the closest real model on the SAME provider -- a candidate that shares
     the requested model's family+version token (``gemini-3.8-pro`` ~
     ``gemini-3.8-flash-high``, both ``gemini-3.8``) always wins over one
     that only matches on tier/difflib ratio; difflib (cutoff ~0.6, high
     enough that a plausible-but-wrong guess is never silently run) is
     only the tiebreak within that group, or the whole ranking when no
     candidate shares family+version;
  2. the user's own configured default model (``model.default`` /
     ``model.provider``), when it differs from the one that was requested;
  3. nothing -- caller keeps the existing stop-with-error behaviour.

Deliberately never touches ``fallback_providers`` / ``_fallback_chain``:
that chain is for outage recovery, and walking it for a typo'd model name
is exactly the silent-drain-a-different-paid-plan bug this module exists
to prevent.
"""

from __future__ import annotations

import logging
from difflib import get_close_matches
from typing import Any, Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)

_SAME_PROVIDER_CUTOFF = 0.6
_DYNAMIC_MODELS_TIMEOUT_S = 5.0

# Per-process cache of dynamically-fetched model lists, keyed by normalized
# base_url. Populated by :func:`known_models_for_provider` for providers
# with no static ``_PROVIDER_MODELS`` entry (custom/user-defined endpoints).
# A failed fetch is cached too (as ``None``) so a dead/unreachable endpoint
# is only probed once per process, not once per model-not-found retry.
_DYNAMIC_MODELS_CACHE: Dict[str, Optional[List[str]]] = {}


def _family_version(name: str) -> tuple:
    """Split a model id into ``(family, version)`` on ``-``.

    ``"gemini-3.8-pro"`` -> ``("gemini", "3.8")``. Missing pieces are ``""``
    so two short/malformed names never spuriously "match".
    """
    parts = (name or "").split("-")
    family = parts[0] if parts else ""
    version = parts[1] if len(parts) > 1 else ""
    return family, version


def _closest_same_provider_match(requested_model: str, known_models: list) -> Optional[str]:
    """Rank ``known_models`` against ``requested_model``: version-sharing
    candidates first (tiebroken by difflib), else a plain difflib match.
    """
    req_family, req_version = _family_version(requested_model)
    version_matches = []
    if req_family and req_version:
        version_matches = [
            m for m in known_models if _family_version(m) == (req_family, req_version)
        ]
    if version_matches:
        match = get_close_matches(requested_model, version_matches, n=1, cutoff=0.0)
        if match:
            return match[0]

    match = get_close_matches(requested_model, known_models, n=1, cutoff=_SAME_PROVIDER_CUTOFF)
    return match[0] if match else None


class ModelSubstitute(NamedTuple):
    model: str
    provider: str
    source: str  # "same_provider" | "configured_default"


def resolve_model_substitute(
    requested_model: str,
    provider: str,
    *,
    known_models: Optional[list] = None,
    default_model: Optional[str] = None,
    default_provider: Optional[str] = None,
) -> Optional[ModelSubstitute]:
    """Return the substitute to use, or ``None`` when nothing qualifies."""
    # The requested model itself is never its own substitute (a widened
    # catalog can list a slug the provider then rejects).
    known_models = [m for m in (known_models or []) if m != requested_model]
    if known_models:
        match = _closest_same_provider_match(requested_model, list(known_models))
        if match:
            return ModelSubstitute(model=match, provider=provider, source="same_provider")

    default_model = (default_model or "").strip()
    if default_model and default_model != requested_model:
        return ModelSubstitute(
            model=default_model,
            provider=(default_provider or "").strip() or provider,
            source="configured_default",
        )

    return None


def _fetch_dynamic_models(base_url: Optional[str], api_key: Optional[str]) -> List[str]:
    """Live ``GET {base_url}/models`` probe for a provider with no static
    catalog entry, cached per (normalized) ``base_url`` for the process.
    """
    base_url = (base_url or "").strip()
    if not base_url:
        return []
    cache_key = base_url.rstrip("/").lower()
    if cache_key in _DYNAMIC_MODELS_CACHE:
        return list(_DYNAMIC_MODELS_CACHE[cache_key] or [])

    models: Optional[List[str]] = None
    try:
        from providers.base import ProviderProfile

        profile = ProviderProfile(name="custom", base_url=base_url)
        models = profile.fetch_models(
            api_key=api_key, base_url=base_url, timeout=_DYNAMIC_MODELS_TIMEOUT_S,
        )
    except Exception as exc:
        logger.debug("Dynamic model list fetch failed for %s: %s", base_url, exc)
        models = None

    _DYNAMIC_MODELS_CACHE[cache_key] = models
    return list(models or [])


def _fetch_live_models_for_builtin_provider(provider: str) -> Optional[List[str]]:
    """Live catalog fetch for a BUILT-IN provider that exposes one.

    Returns ``None`` when the provider has no live fetch wired in here, or
    the fetch didn't unambiguously succeed (no credentials / endpoint
    unreachable) -- the caller falls back to the static ``_PROVIDER_MODELS``
    entry in that case. An empty list would be indistinguishable from "the
    fetch failed", so ``None`` is the only "not live" signal.

    Currently wired: ``openai-codex``, whose own ``/codex/models`` catalog
    is what actually caught the regression this module exists to prevent --
    a real, working Codex model (released after Clover's last static-catalog
    sync) was silently downgraded because it wasn't in ``_PROVIDER_MODELS``
    yet (#93412 follow-up). Reuses the same live fetch the ``/model`` picker
    already calls (``clover_cli.models.provider_model_ids``); a static
    catalog is NEVER proof a model doesn't exist, only a list fetched right
    now from the provider itself is.
    """
    if provider != "openai-codex":
        return None
    try:
        from clover_cli.auth import resolve_codex_runtime_credentials
        from clover_cli.codex_models import _fetch_models_from_api

        creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        access_token = creds.get("api_key") if isinstance(creds, dict) else None
    except Exception as exc:
        logger.debug("Codex credential resolution failed for live model list: %s", exc)
        return None
    if not access_token:
        return None
    try:
        models = _fetch_models_from_api(access_token)
    except Exception as exc:
        logger.debug("Codex live model list fetch failed: %s", exc)
        return None
    if not models:
        return None
    return _with_codex_context_variants(models)


def _with_codex_context_variants(models: List[str]) -> List[str]:
    """Add a ``-900k`` sibling for every live-listed Codex base model.

    ``-900k`` is a Clover-side context-window-modifier suffix, stripped
    before the model id ever reaches the wire (``agent/model_metadata.py``);
    it is NOT its own catalog entry on the live endpoint (confirmed: the
    live ``/codex/models`` response never lists a ``<base>-900k`` slug for
    ANY base, "-900k" or not). The picker's own hardcoded eligibility
    allowlist (``_CODEX_900K_ELIGIBLE_BASES``) is exactly the kind of
    static catalog this module exists to stop trusting as proof of absence
    -- it lags a freshly-released family the same way ``_PROVIDER_MODELS``
    does (#93412 follow-up: ``gpt-6-astra-900k`` genuinely works, but its
    base ``gpt-6-astra`` predates that allowlist). Deliberately NOT gated
    on that allowlist here: a preflight-only, best-effort "is this
    plausible" widening, never used for the interactive picker or the wire
    path, and always backstopped by the runtime model_not_found path if a
    given base turns out not to actually support the variant.
    """
    _suffix = "-900k"
    enriched = list(models)
    seen = set(enriched)
    for base in models:
        if base.endswith(_suffix):
            continue
        variant = base + _suffix
        if variant not in seen:
            enriched.append(variant)
            seen.add(variant)
    return enriched


def known_models_for_provider(
    provider: Optional[str],
    *,
    requested_provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> tuple:
    """Best-known model catalog for a provider, plus whether it's LIVE.

    Returns ``(models, is_live)``. A static catalog is NEVER proof that a
    model doesn't exist -- it lags real provider releases. Only two things
    count as proof a pinned model is unknown: (a) a list just fetched from
    the provider itself (``is_live=True`` here), or (b) the provider's API
    returning ``model_not_found`` at runtime (the conversation_loop pinned
    branch, handled entirely outside this function). Callers that gate a
    substitute-or-fail decision on the catalog (the ``clover -z`` preflight)
    MUST only act when ``is_live`` is true; a static-only or empty result
    means "skip the preflight, let the real call decide" (#93412 follow-up).

    Live sources, tried in order:
      1. A built-in provider's own live fetch (currently: ``openai-codex``;
         see :func:`_fetch_live_models_for_builtin_provider`).
      2. A live probe of a custom/user-defined provider's own
         OpenAI-compatible ``/models`` (:func:`_fetch_dynamic_models`) --
         only reached when NEITHER ``provider`` nor ``requested_provider``
         has a static entry (a named custom provider like ``"gemini-oauth"``
         has no ``_PROVIDER_MODELS`` entry at all, by construction).

    Falls back to the static ``_PROVIDER_MODELS`` catalog -- matched by
    ``provider`` (the resolved runtime/billing class, e.g. ``"custom"``) AND
    by ``requested_provider`` (the user-facing name, e.g. a named custom
    provider) -- only when no live source is available. Deliberately checks
    for KEY PRESENCE, not truthiness: a provider that genuinely has a static
    entry with an empty list (a real, empty-for-now catalog) must NOT
    trigger the dynamic ``/models`` probe -- only a provider with no entry
    at all (custom/user-defined providers, by construction) does.
    """
    provider_norm = (provider or "").strip().lower()
    live_models = _fetch_live_models_for_builtin_provider(provider_norm)
    if live_models is not None:
        return live_models, True

    from clover_cli.models import _PROVIDER_MODELS

    for key in (provider, requested_provider):
        key_norm = (key or "").strip().lower()
        if key_norm and key_norm in _PROVIDER_MODELS:
            return list(_PROVIDER_MODELS[key_norm]), False

    dynamic_models = _fetch_dynamic_models(base_url, api_key)
    return dynamic_models, bool(dynamic_models)


def substitute_unknown_models_enabled(config: Optional[dict]) -> bool:
    """``model.substitute_unknown`` (default ``True``).

    ``model:`` is either a bare string (no dict, nothing to read) or a dict
    with ``default``/``model``/``provider``/``substitute_unknown`` keys --
    same flexible shape ``configured_default_model`` below handles.
    """
    model_cfg = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_cfg, dict):
        return bool(model_cfg.get("substitute_unknown", True))
    return True


def configured_default_model(config: Optional[dict]) -> tuple:
    """Return ``(default_model, default_provider)`` from config.yaml's
    ``model`` section, or ``("", "")`` when unset.

    Mirrors the same bare-string-vs-dict resolution ``clover -z``'s
    effective-model logic already performs (``clover_cli/oneshot.py``).
    """
    model_cfg = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_cfg, str):
        return model_cfg.strip(), ""
    if not isinstance(model_cfg, dict):
        return "", ""

    raw: Any = model_cfg.get("default") or model_cfg.get("model") or ""
    if isinstance(raw, dict):
        from clover_cli.config import split_model_config_default

        default_model, embedded_provider = split_model_config_default(raw)
    else:
        default_model, embedded_provider = str(raw or "").strip(), ""

    default_provider = embedded_provider or str(model_cfg.get("provider") or "").strip()
    return default_model, default_provider


def pinned_model_unavailable_message(model: str, provider: str, substitution: Optional[dict] = None) -> str:
    """Stop-with-error text for a pinned model the provider rejected.

    When ``substitution`` (``agent._model_substitution``) is set, the failing
    model is itself a substitute: say so, so the caller knows both the
    requested and the substituted model were unavailable.
    """
    if substitution:
        return (
            f"Substitute model '{model}' (for '{substitution.get('requested_model')}') "
            f"isn't available on provider '{provider}' either. "
            "Nothing was run on another model."
        )
    return (
        f"Model '{model}' isn't available on provider "
        f"'{provider}'. Nothing was run on another model."
    )
