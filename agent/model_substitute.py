"""Pick a substitute when a pinned model doesn't exist on its provider.

Order (stop at the first hit; see #93412 follow-up):
  1. the closest real model on the SAME provider (difflib, cutoff ~0.6,
     high enough that a plausible-but-wrong guess is never silently run);
  2. the user's own configured default model (``model.default`` /
     ``model.provider``), when it differs from the one that was requested;
  3. nothing -- caller keeps the existing stop-with-error behaviour.

Deliberately never touches ``fallback_providers`` / ``_fallback_chain``:
that chain is for outage recovery, and walking it for a typo'd model name
is exactly the silent-drain-a-different-paid-plan bug this module exists
to prevent.
"""

from __future__ import annotations

from difflib import get_close_matches
from typing import Any, NamedTuple, Optional

_SAME_PROVIDER_CUTOFF = 0.6


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
        match = get_close_matches(
            requested_model, known_models, n=1, cutoff=_SAME_PROVIDER_CUTOFF,
        )
        if match:
            return ModelSubstitute(model=match[0], provider=provider, source="same_provider")

    default_model = (default_model or "").strip()
    if default_model and default_model != requested_model:
        return ModelSubstitute(
            model=default_model,
            provider=(default_provider or "").strip() or provider,
            source="configured_default",
        )

    return None


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
