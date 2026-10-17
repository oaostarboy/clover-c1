"""PTB Application processing with only the Telegram HTTP endpoint replaced."""
import json

import pytest

from clover_cli import config
from tests.gateway.test_proactive_command import home  # noqa: F401
from tests.test_proactive_command_access_wire import wire, dispatch, command, callback


def token(menu, label):
    return next(b['callback_data'] for row in menu['reply_markup']['inline_keyboard']
                for b in row if b['text'] == label)


@pytest.mark.asyncio
async def test_real_ptb_customize_serialization_callback_readback_and_text_parity(home, monkeypatch):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    before = (home / 'config.yaml').read_bytes()
    evidence = []
    async with app:
        await dispatch(app, a, command(app, '/proactive'))
        current = [p for m, p in endpoint.calls if m == 'sendMessage'][-1]
        evidence.append({'page': 'main', **current})
        await app.process_update(callback(app, token(current, '⚙️ Customize')))
        current = [p for m, p in endpoint.calls if m == 'editMessageText'][-1]
        evidence.append({'page': 'customize', **current})
        assert len(current['text']) < 180
        assert (home / 'config.yaml').read_bytes() == before
        for label, page in [('✨ Use recommended', 'recommended'), ('‹ Back', 'customize'),
                            ('🕓 Timing · After reply', 'timing'), ('Never suggest', 'never')]:
            await app.process_update(callback(app, token(current, label)))
            current = [p for m, p in endpoint.calls if m == 'editMessageText'][-1]
            evidence.append({'page': page, **current})
        assert (home / 'config.yaml').read_bytes() == before
        await app.process_update(callback(app, token(current, '💾 Save')))
        saved = config.require_readable_config_before_write()['proactive']
        assert saved['suggestions']['preferred_timing'] == 'never'
        assert saved['suggestions']['enabled'] is True
        assert saved['enabled'] is False
        await dispatch(app, a, command(app, '/proactive timing after_reply'))
        assert config.require_readable_config_before_write()['proactive']['suggestions']['preferred_timing'] == 'after_reply'
        await dispatch(app, a, command(app, '/proactive off'))
        await dispatch(app, a, command(app, '/proactive on'))
        assert config.require_readable_config_before_write()['proactive']['suggestions']['preferred_timing'] == 'after_reply'
    print('CUSTOMIZE_PTB_FIXTURE=' + json.dumps(evidence, ensure_ascii=False))
    errors.assert_not_called()
    r._handle_message_with_agent.assert_not_called()
