from cron.presence import ThoughtStore


def test_explicit_two_surface_link_never_guesses_matching_numeric_ids(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from agent.presence_state import ContextKey
    from agent.workstreams import Scope
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    store = ThoughtStore()
    a = ContextKey(Scope(str(tmp_path), '/project', 'private:telegram:7', '7'), 'telegram-root')
    b = ContextKey(Scope(str(tmp_path), '/project', 'private:desktop:7', '7'), 'desktop-root')
    moment = datetime.now(timezone.utc)
    assert store.linked_key(a) == a and store.linked_key(b) == b
    code = store.issue_link(a, 'telegram', moment)
    assert code and store.accept_link(code, b, 'desktop', moment) is True
    assert store.linked_key(a) == store.linked_key(b)
    assert store.linked_key(a) not in (a, b)
    assert store.accept_link(code, b, 'desktop', moment) is False
    assert store.unlink(a) is True
    assert store.linked_key(a) == a and store.linked_key(b) == b


def test_link_rejects_unmatched_project_profile_and_expired_challenge(monkeypatch, tmp_path):
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    from agent.presence_state import ContextKey
    from agent.workstreams import Scope
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    store = ThoughtStore()
    a = ContextKey(Scope(str(tmp_path), '/project', 'private:telegram:7', '7'), 'telegram-root')
    b = ContextKey(Scope(str(tmp_path), '/project', 'private:desktop:7', '7'), 'desktop-root')
    moment = datetime.now(timezone.utc)
    code = store.issue_link(a, 'telegram', moment)
    assert store.accept_link(code, replace(b, scope=replace(b.scope, project='/other')), 'desktop', moment) is False
    assert store.accept_link(code, replace(b, scope=replace(b.scope, profile='/other')), 'desktop', moment) is False
    assert store.accept_link(code, b, 'desktop', moment+timedelta(seconds=301)) is False
