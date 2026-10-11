"""Login throttling."""


class LoginThrottle:
    """Blocks a user after `limit` failed logins in a row."""

    def __init__(self, limit=3):
        self.limit = limit
        self._failures = {}

    def record(self, user, success):
        if success:
            return
        self._failures[user] = self._failures.get(user, 0) + 1

    def blocked(self, user):
        return self._failures.get(user, 0) >= self.limit
