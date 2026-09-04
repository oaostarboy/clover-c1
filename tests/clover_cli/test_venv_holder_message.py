"""Regression tests for the gateway-blocked update message.

Behaviour contract, not a snapshot: the assertions are about what the message
must NOT do to a user whose only blocker is the gateway (send them after a
desktop app that isn't running, or hand them a flag that takes the assistant
offline with nothing to restart it).
"""

import sys

import pytest

sys.path.insert(0, "/home/starboy/Projects/clover-c1-build/clover-c1")


GATEWAY_ARGV = (
    r"C:\Users\Francis\AppData\Local\clover\clover-c1\venv\Scripts\python.exe "
    r"-m clover_cli.main gateway run"
)
SERVE_ARGV = (
    r"C:\Users\Francis\AppData\Local\clover\clover-c1\venv\Scripts\python.exe "
    r"-m clover_cli.main serve"
)


def _message(matches):
    from clover_cli.update_cmd import _format_venv_python_holders_message

    return _format_venv_python_holders_message(matches)


class TestGatewayOnlyHolders:
    """When the gateway is the only thing blocking, advice must be actionable."""

    matches = [(27740, "python.exe", GATEWAY_ARGV)]

    def test_does_not_send_the_user_after_a_desktop_app(self):
        """There is no app to close, so that instruction is unfollowable.

        Reported 2026-09-04: a manual-process Windows install refused every
        update with advice about closing a desktop app that was not running.
        """
        msg = _message(self.matches)
        assert "desktop app" not in msg.lower()

    def test_does_not_recommend_force_venv(self):
        """--force-venv stops the gateway; without a supervisor nothing returns it.

        That is the 2026-08-30 incident chain (four outages, one ~9h
        overnight). Offering it as the natural next step points the affected
        user at the exact failure.
        """
        msg = _message(self.matches)
        recommending = [
            line for line in msg.splitlines()
            if "--force-venv" in line and "avoid" not in line.lower()
        ]
        assert not recommending, f"still recommends --force-venv: {recommending}"

    def test_warns_that_forcing_can_strand_the_install(self):
        msg = _message(self.matches).lower()
        assert "--force-venv" in msg
        assert "avoid" in msg
        assert "supervisor" in msg

    def test_names_the_gateway_as_the_blocker(self):
        msg = _message(self.matches).lower()
        assert "gateway itself" in msg

    def test_gives_a_path_that_can_actually_succeed(self):
        """Running outside the gateway is the one route that works."""
        msg = _message(self.matches).lower()
        assert "outside the gateway" in msg

    def test_reassures_that_nothing_changed(self):
        msg = _message(self.matches).lower()
        assert "nothing has changed" in msg

    def test_says_re_running_here_will_refuse_again(self):
        """The 'can never succeed' loop must be stated, not discovered."""
        msg = _message(self.matches).lower()
        assert "refuse again" in msg


class TestNonGatewayHoldersKeepOldAdvice:
    """Desktop/dashboard holders: the original guidance is correct for them."""

    def test_desktop_backend_still_told_to_close_the_app(self):
        msg = _message([(1234, "python.exe", SERVE_ARGV)])
        assert "desktop app" in msg.lower()
        assert "--force-venv" in msg

    def test_mixed_holders_keep_the_generic_advice(self):
        """A gateway PLUS a desktop backend is not the gateway-only case."""
        msg = _message([
            (27740, "python.exe", GATEWAY_ARGV),
            (1234, "python.exe", SERVE_ARGV),
        ])
        assert "desktop app" in msg.lower()
        assert "gateway itself" not in msg.lower()

    def test_unknown_argv_keeps_the_generic_advice(self):
        """Never guess a label; unknown holders get the conservative text."""
        msg = _message([(999, "python.exe", "python.exe some-unrelated-thing")])
        assert "gateway itself" not in msg.lower()


class TestAlwaysPresent:
    def test_every_holder_is_listed_with_its_pid(self):
        msg = _message([(27740, "python.exe", GATEWAY_ARGV)])
        assert "27740" in msg

    def test_explains_why_pyd_locks_matter(self):
        msg = _message([(27740, "python.exe", GATEWAY_ARGV)]).lower()
        assert ".pyd" in msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
