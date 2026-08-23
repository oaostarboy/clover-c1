"""Tests for the AI_AGENT / CLOVER_C1 harness-attribution env vars.

Port of earendil-works/pi#7493: entry points advertise the agent harness to
child processes via the cross-agent ``AI_AGENT`` standard plus a
Clover-specific marker, without clobbering an outer harness.

The AI_AGENT value must equal Clover' id in the public agent-harness
registry (``clover-c1`` in huggingface.js ``agent-harnesses.ts``) —
standard-var matching there is exact, so any other value is attributed to
"unknown".

The terminal backends additionally export both vars inside every wrapped
shell command (``BaseEnvironment._wrap_command``) so the marker reaches
REMOTE backends (Docker/SSH/Modal/Daytona/Singularity/Vercel) whose exec
environment does not inherit the Clover process env, and survives the
cross-session leak guard that strips ``CLOVER_SESSION_*`` from subprocess
envs in engaged multi-session hosts.
"""

import os
import subprocess

from clover_cli.main import _advertise_agent_env

# Registry id — must stay in sync with huggingface.js agent-harnesses.ts.
HARNESS_ID = "clover-c1"


class TestAdvertiseAgentEnv:
    def test_sets_both_vars_when_unset(self, monkeypatch):
        monkeypatch.delenv("AI_AGENT", raising=False)
        monkeypatch.delenv("CLOVER_C1", raising=False)
        _advertise_agent_env()
        assert os.environ["AI_AGENT"] == HARNESS_ID
        assert os.environ["CLOVER_C1"] == "true"

    def test_does_not_clobber_outer_harness(self, monkeypatch):
        monkeypatch.setenv("AI_AGENT", "pi")
        monkeypatch.delenv("CLOVER_C1", raising=False)
        _advertise_agent_env()
        assert os.environ["AI_AGENT"] == "pi"
        assert os.environ["CLOVER_C1"] == "true"

    def test_idempotent(self, monkeypatch):
        monkeypatch.delenv("AI_AGENT", raising=False)
        monkeypatch.delenv("CLOVER_C1", raising=False)
        _advertise_agent_env()
        _advertise_agent_env()
        assert os.environ["AI_AGENT"] == HARNESS_ID
        assert os.environ["CLOVER_C1"] == "true"


class TestWrapCommandAdvertisesHarness:
    """The shell-level export in BaseEnvironment._wrap_command."""

    def _wrap(self, command: str) -> str:
        from tools.environments.local import LocalEnvironment

        env = LocalEnvironment.__new__(LocalEnvironment)
        env._snapshot_ready = False
        env._session_id = "testsession0"
        env._cwd_marker = "__CLOVER_CWD_testsession0__"
        env._snapshot_path = "/tmp/clover-snap-testsession0.sh"
        env._snapshot_passthrough_names = set()
        return env._wrap_command(command, "/tmp")

    def test_wrap_command_contains_export(self):
        wrapped = self._wrap("true")
        assert 'AI_AGENT="${AI_AGENT:-' + HARNESS_ID + '}"' in wrapped
        assert 'CLOVER_C1="${CLOVER_C1:-true}"' in wrapped

    def test_export_precedes_user_command(self):
        wrapped = self._wrap("echo payload-sentinel")
        assert wrapped.index("AI_AGENT=") < wrapped.index("payload-sentinel")

    def test_shell_sets_default_and_preserves_outer(self):
        """Run the wrapped script through real bash both ways."""
        wrapped = self._wrap('echo "AI=$AI_AGENT CLOVER=$CLOVER_C1"')

        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("AI_AGENT", "CLOVER_C1")}
        out = subprocess.run(
            ["bash", "-c", wrapped], capture_output=True, text=True,
            env=clean_env, timeout=30,
        )
        assert f"AI={HARNESS_ID} CLOVER=true" in out.stdout

        outer_env = dict(clean_env, AI_AGENT="pi", CLOVER_C1="false")
        out = subprocess.run(
            ["bash", "-c", wrapped], capture_output=True, text=True,
            env=outer_env, timeout=30,
        )
        assert "AI=pi CLOVER=false" in out.stdout
