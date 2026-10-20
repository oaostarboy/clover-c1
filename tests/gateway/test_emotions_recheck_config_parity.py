"""Compare status projection with real current effective configuration, temp only."""
import pytest
import yaml


@pytest.mark.parametrize('user,managed', [
    ({}, {}),
    ({'model':{'name':'synthetic','provider':'openai'}}, {}),
    ({'model':{'default':'synthetic'},'provider':'openai'}, {}),
    ({'model':{'default':{'provider':'openai','model':'synthetic'}}}, {}),
    ({'model':{'default':{'provider':'openai','name':'synthetic'}}}, {}),
    ({'model':{'provider':'custom','default':'synthetic','api_base':'https://inner.invalid/v1'},'api_base':'https://root.invalid/v1'}, {}),
    ({'model':{'provider':'openai','default':'synthetic'}}, {'model':{'name':'managed','provider':'anthropic'}}),
    ({'model':{'provider':'openai','default':'synthetic'}}, {'model':{'default':{'name':'managed','provider':'anthropic'}}}),
])
def test_status_route_agrees_with_effective_config(tmp_path, monkeypatch, user, managed):
    from clover_constants import set_clover_home_override, reset_clover_home_override
    from clover_cli.config import load_config
    from agent.emotions_config import snapshot
    from agent.bounded_completion import select_route
    home=tmp_path/'profile';home.mkdir()
    policy=tmp_path/'managed';policy.mkdir()
    (home/'config.yaml').write_text(yaml.safe_dump(user))
    (policy/'config.yaml').write_text(yaml.safe_dump(managed))
    monkeypatch.setenv('CLOVER_MANAGED_DIR',str(policy))
    token=set_clover_home_override(home)
    try:
        # The ordinary loader is permitted to initialize this isolated baseline
        # home. The status snapshot itself is not granted those side effects.
        effective=load_config(migrate=False)
        projected=snapshot()
        def route(config):
            try:
                value=select_route(config)
                return (value.provider,value.model,value.base_url,value.mode)
            except Exception as exc:
                return ('held',getattr(exc,'reason',type(exc).__name__))
        actual, expected=route(projected),route(effective)
        print('CONFIG_ROUTE_PARITY',{'user':user,'managed':managed,'status':actual,'effective':expected})
        assert actual==expected
    finally:
        reset_clover_home_override(token)
