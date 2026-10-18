"""Settings-only /emotions projection, with no startup/config runtime imports.

The ordinary config module has import-time provider/plugin discovery and may
back up malformed configuration. Do not import it even with migrate=False.
Startup still owns migrations. Only relevant route aliases are projected here
in memory; interpolation holds rather than opening secrets or guessing values.
"""
from copy import deepcopy


def _merge(base, overlay):
    result = deepcopy(base)
    for key, value in overlay.items():
        result[key] = (_merge(result[key], value) if isinstance(value, dict)
                       and isinstance(result.get(key), dict) else deepcopy(value))
    return result


def _model(config):
    from clover_cli.model_config import normalize_root_model_keys
    model = normalize_root_model_keys(config).get('model', {})
    if isinstance(model, str):
        model = {'default': model}
    if not isinstance(model, dict):
        raise ValueError('invalid model settings')
    return {k: deepcopy(v) for k, v in model.items() if k in
            {'provider', 'default', 'base_url', 'api_mode', 'openai_runtime'}}


def _project(config):
    result = {}
    for name, fields in (('presence', {'enabled', 'budget_mode'}),
            ('proactive', {'enabled', 'mode', 'suggestions', 'aux'})):
        if name in config:
            section = config[name]
            if not isinstance(section, dict):
                raise ValueError('invalid status settings')
            result[name] = {k: deepcopy(v) for k, v in section.items() if k in fields}
    proactive = result.get('proactive', {})
    for name, fields in (('suggestions', {'enabled'}),
            ('aux', {'max_calls_per_event', 'max_calls_per_day', 'max_output_tokens'})):
        section = proactive.get(name)
        if isinstance(section, dict):
            proactive[name] = {k: v for k, v in section.items() if k in fields}
    result['model'] = _model(config)
    aux = config.get('auxiliary', {})
    if not isinstance(aux, dict):
        raise ValueError('invalid auxiliary settings')
    task = aux.get('presence', {})
    result['auxiliary'] = {'presence': ({k: deepcopy(v) for k, v in task.items() if k in
        {'provider', 'model', 'base_url', 'api_mode'}} if isinstance(task, dict) else task)}
    return result


def snapshot():
    from clover_constants import get_clover_home
    from clover_cli.managed_scope import load_managed_config
    import yaml

    path = get_clover_home() / 'config.yaml'
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError('unreadable configuration')
        raw = {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('configuration must be a mapping')
    config = _merge(_project(raw), _project(load_managed_config(strict=True)))
    def literal(value):
        if isinstance(value, dict):
            return all(literal(v) for v in value.values())
        if isinstance(value, list):
            return all(literal(v) for v in value)
        return not isinstance(value, str) or '${' not in value
    if not literal(config):
        raise ValueError('status requires literal settings; interpolation not resolved')
    return config
