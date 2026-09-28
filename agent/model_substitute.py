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


def known_models_for_provider(
    provider: Optional[str],
    *,
    requested_provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[str]:
    """Best-known model catalog for a provider.

    Static ``_PROVIDER_MODELS`` first -- matched by ``provider`` (the
    resolved runtime/billing class, e.g. ``"custom"``) AND by
    ``requested_provider`` (the user-facing name, e.g. a named custom
    provider like ``"gemini-oauth"``), since a custom/user-defined
    provider's catalog -- if Clover ships one -- is keyed by whichever name
    happens to be more specific. When neither has a static list, fall back
    to a live probe of the endpoint's own OpenAI-compatible ``/models``
    (#93412 follow-up: a custom provider has no ``_PROVIDER_MODELS`` entry
    at all, which was silently treated as an empty catalog -- skipping the
    same-provider substitute entirely and jumping straight to a
    cross-provider default).

    Deliberately checks for KEY PRESENCE, not truthiness: a provider that
    genuinely has a static entry with an empty list (a real, empty-for-now
    catalog) must NOT trigger a live probe -- only a provider with no entry
    at all (custom/user-defined providers, by construction) does.
    """
    from clover_cli.models import _PROVIDER_MODELS

    for key in (provider, requested_provider):
        key_norm = (key or "").strip().lower()
        if key_norm and key_norm in _PROVIDER_MODELS:
            return list(_PROVIDER_MODELS[key_norm])

    return _fetch_dynamic_models(base_url, api_key)


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
