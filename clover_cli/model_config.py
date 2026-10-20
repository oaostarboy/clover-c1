"""Pure legacy model-key normalization shared by startup and read-only status.

No defaults, interpolation, I/O, provider discovery or credential access.
"""
from typing import Any, Dict


def normalize_root_model_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    """Move stale root-level provider/base_url/context_length into model section.

    Some users (or older code) placed ``provider:``, ``base_url:``, or
    ``context_length:`` at the config root instead of inside ``model:``.
    These root-level keys are only used as a fallback when the corresponding
    ``model.*`` key is empty — they never override an existing value.
    After migration the root-level keys are removed so they can't cause
    confusion on subsequent loads.

    Also aliases ``api_base`` → ``base_url`` (issue #8919). ``api_base`` is the
    intuitive name OpenAI-SDK / LiteLLM users reach for, and ``clover config set``
    blindly accepts any dotted key — so ``model.api_base`` got written, confirmed,
    and then silently ignored by the runtime resolver (which reads only
    ``model.base_url``), causing requests to fall back to OpenRouter. We migrate
    the alias to the canonical key (fallback-only — never override an explicit
    ``base_url``) and drop the alias so it can't confuse later loads.

    Finally, canonicalizes the model-id key to ``model.default`` (issue #34500).
    The runtime resolver and ~14 other readers select the chat model via
    ``model.default``; ``model.model`` was already aliased inline at some sites
    but ``model.name`` was not, so a custom-provider config like
    ``model: {name: <id>, provider: <custom>}`` resolved to an empty model and
    the API request went out with ``model=`` (HTTP 400 from OpenAI-compatible
    backends) — while display paths (``clover status``/``dump``) read ``name``
    and *showed* the model, making the failure silent. Normalizing here (the
    single load/save chokepoint) means every reader, present and future, sees a
    populated ``default`` and the stale alias is migrated out of config.yaml on
    the next save. Precedence: ``default`` > ``model`` > ``name`` (never
    overrides an explicit ``default``, so existing configs are unaffected).
    """
    # Only act if there's something to migrate: root-level keys, an api_base
    # alias, or a model dict whose id lives under a non-canonical key.
    model_in = config.get("model")
    model_has_alias = isinstance(model_in, dict) and model_in.get("api_base")
    # A dict-valued ``default``/``model`` (``{provider: ..., model: ...}``)
    # must be flattened into ``default`` (string) + ``provider`` here at the
    # single load/save chokepoint, so every reader (doctor, status, fallback
    # picker, prompt-size, context-switch guard, …) sees plain strings instead
    # of a nested dict that crashes ``.strip()``/``.lower()`` or routes the
    # model through the wrong provider.
    _has_nested_default = isinstance(model_in, dict) and (
        isinstance(model_in.get("default"), dict)
        or isinstance(model_in.get("model"), dict)
        or isinstance(model_in.get("name"), dict)
    )
    # A model dict needs canonicalization if its id lives under a non-canonical
    # key (``model``/``name``) — either because ``default`` is empty (we must
    # promote the alias) or because ``default`` is set but a stale alias still
    # lingers (we must drop it so config.yaml ends up canonical).
    model_needs_canon = isinstance(model_in, dict) and (
        model_in.get("model") or model_in.get("name")
    )
    has_root = any(
        config.get(k) for k in ("provider", "base_url", "context_length", "api_base")
    )
    if not has_root and not model_has_alias and not model_needs_canon and not _has_nested_default:
        return config

    config = dict(config)
    model = config.get("model")
    if not isinstance(model, dict):
        model = {"default": model} if model else {}
    else:
        model = dict(model)
    config["model"] = model

    # Flatten a dict-valued ``model.default``/``model.model``:
    # ``{provider: <p>, model: <m>}`` -> ``default: "<m>"`` and, when no
    # explicit ``model.provider`` is set, ``provider: "<p>"``. The nested
    # provider must win over the merged default ``"auto"`` (which runtime
    # resolution treats as authoritative and would otherwise route the model
    # through the wrong active provider), but never over an explicitly
    # configured outer provider.
    for _key in ("default", "model", "name"):
        _val = model.get(_key)
        if isinstance(_val, dict):
            _nested_model = _val.get("model") or _val.get("default")
            _nested_provider = str(_val.get("provider") or "").strip()
            model[_key] = str(_nested_model or "").strip()
            if _nested_provider:
                _outer_provider = str(model.get("provider") or "").strip()
                if not _outer_provider or _outer_provider == "auto":
                    model["provider"] = _nested_provider

    for key in ("provider", "base_url", "context_length"):
        root_val = config.get(key)
        if root_val and not model.get(key):
            model[key] = root_val
        config.pop(key, None)

    # api_base is an alias for base_url, at the root OR inside model.
    for alias_val in (config.get("api_base"), model.get("api_base")):
        if alias_val and not model.get("base_url"):
            model["base_url"] = alias_val
    config.pop("api_base", None)
    model.pop("api_base", None)

    # Canonicalize the model id to ``default``. ``model`` and ``name`` are
    # last-resort aliases (in that order) — only consulted when ``default`` is
    # empty, then dropped so later loads/saves can't reintroduce the ambiguity.
    if not (model.get("default") or ""):
        alias = model.get("model") or model.get("name")
        if alias:
            model["default"] = alias
    if model.get("default"):
        # Drop the now-redundant aliases so config.yaml ends up canonical.
        model.pop("model", None)
        model.pop("name", None)

    return config
