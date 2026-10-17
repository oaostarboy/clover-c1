"""Controls enable real consumers, not merely saved toggle labels."""
from clover_cli.proactive import proactive_command
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.gateway.test_workstream_suggestion_offer import OfferHarness, seed, offer, offers
from tests.gateway.test_workstream_verified_workflow import workflow  # noqa: F401


def test_explicit_on_enables_real_inline_offer_without_extra_provider_calls(monkeypatch, tmp_path, network_guard):
    h = OfferHarness(monkeypatch, tmp_path, proactive=False)
    try:
        # Harness normally pins its startup config. Use the real profile reader
        # for settings propagation while retaining scripted transport/model edges.
        import gateway.run as gr
        from clover_cli import config
        startup = gr._load_gateway_config()
        config.save_config(startup, strip_defaults=False)
        monkeypatch.setattr(gr, '_load_gateway_config', lambda: {
            **startup, 'proactive': config.load_config(migrate=False).get('proactive', {})})
        assert 'Saved' in proactive_command('on', can_change=True)
        seed(h)
        result = offer(h)
        assert 'Optional workstream check' in result['final_response']
        assert len(offers(h)) == 1
        assert h.scripted.calls == 2  # ordinary foreground replies only
        assert not (h.home / 'cron' / 'jobs.json').exists()
        assert 'Saved' in proactive_command('off', can_change=True)
        h.inbound_human('Help plan deployment target verification', message_id=102)
        assert h.results[-1]['final_response'] == 'ack'
        assert h.scripted.calls == 3
    finally:
        h.close()


def test_optional_skill_choice_feeds_existing_review_store_not_installation(workflow):
    from tools import write_approval as wa
    from agent import curator
    h = workflow
    assert 'Saved' in proactive_command('recommended', can_change=True)
    assert 'Saved' in proactive_command('skill-drafts on', can_change=True)
    first = h.run()
    assert first['workstream_execution']['status'] == 'success'
    assert wa.list_pending('skills') == []
    h.run()
    result = h.run(new=True)
    assert len(result['workstream_execution']['drafts']) == 1
    assert len(wa.list_pending('skills')) == 1
    assert curator.run_procedure_intake() == []
    assert len(wa.list_pending('skills')) == 1
    assert not list(h.home.glob('skills/**/SKILL.md'))
