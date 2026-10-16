"""Exact task routes: named configuration references, not model-supplied auth.

Tiers deliberately allow substitution; these selectors never do. Account
selection is currently unavailable in the shared runtime resolver (in particular
an email on a custom proxy is not credential binding). Refuse it explicitly.
"""
from __future__ import annotations

import re
from typing import Any

_SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")


def route_catalog(cfg: dict) -> dict[str, dict[str, str]]:
    routes = cfg.get('routes')
    if not isinstance(routes, dict):
        return {}
    return {name: {'provider': value['provider'], 'model': value['model']}
            for name, value in routes.items()
            if isinstance(name, str) and _SELECTOR.fullmatch(name)
            and isinstance(value, dict)
            and all(isinstance(value.get(k), str) and _SELECTOR.fullmatch(value[k])
                    for k in ('provider', 'model'))}


def resolve_named_route(task: dict, cfg: dict, parent: Any) -> dict | None:
    selector = task.get('route')
    account = task.get('account')
    if account is not None or (isinstance(cfg.get('routes'), dict)
                               and isinstance(cfg['routes'].get(selector), dict)
                               and cfg['routes'][selector].get('account') is not None):
        raise ValueError('Exact account binding is unsupported by the delegation credential resolver; '
                         'no child was started. Use a separately verified account-scoped provider route; '
                         'an email in task text or route metadata is not account proof.')
    if selector is None:
        return None
    if not isinstance(selector, str) or not _SELECTOR.fullmatch(selector):
        raise ValueError('Invalid named delegation route; no child was started.')
    route = route_catalog(cfg).get(selector)
    if route is None:
        raise ValueError('Requested named delegation route is not configured in delegation.routes; '
                         'no child was started and no default model was substituted.')
    if task.get('tier'):
        raise ValueError('An exact named route cannot also request a fallback tier.')
    from tools.delegate_tool import _resolve_delegation_credentials
    try:
        resolved = _resolve_delegation_credentials(route, parent)
    except Exception as exc:
        # Provider errors can contain private URLs or keys. Do not serialize them.
        raise ValueError('Requested named delegation route is unavailable; no child was started '
                         'and no alternate model, account or paid route was selected.') from exc
    if not resolved.get('provider') or not resolved.get('api_key') or resolved.get('model') != route['model']:
        raise ValueError('Requested named delegation route could not be resolved exactly; no child was started.')
    return {**resolved, 'requested_route': {'route': selector, **route}}
