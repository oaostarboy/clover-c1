from auth.throttle import LoginThrottle


def test_blocked_after_limit_failures():
    t = LoginThrottle(limit=3)
    for _ in range(3):
        t.record("ann", success=False)
    assert t.blocked("ann")


def test_success_resets_the_failure_count():
    t = LoginThrottle(limit=3)
    t.record("ann", success=False)
    t.record("ann", success=False)
    t.record("ann", success=True)
    t.record("ann", success=False)
    t.record("ann", success=False)
    assert not t.blocked("ann")


def test_users_are_counted_separately():
    t = LoginThrottle(limit=2)
    t.record("ann", success=False)
    t.record("ann", success=False)
    assert t.blocked("ann") and not t.blocked("bob")
