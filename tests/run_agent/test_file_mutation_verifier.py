"""Tests for the per-turn file-mutation verifier footer.

Covers the three moving pieces:

1. ``_extract_file_mutation_targets`` — pulls file paths from write_file /
   patch (replace + V4A) tool-call argument dicts.
2. ``AIAgent._record_file_mutation_result`` — builds the per-turn state
   dict, removing entries when a later success supersedes an earlier
   failure for the same path.
3. ``AIAgent._format_file_mutation_failure_footer`` — renders the dict
   as a user-visible advisory.

Regression target: the "Ben Eng llm-wiki" session where grok-4.1-fast
batched parallel patches, half failed, and the model summarised the
turn claiming every file was edited.  This verifier makes over-claiming
structurally impossible past the model: the user always sees the real
list of files that did NOT change.

Second regression target: an agent's ``patch`` call to
``~/.clover/config.yaml`` was refused by the write guardrail, so the
model fell back to the terminal tool (``clover config set ...``), which
DID change the file. The verifier still reported the path as "NOT
modified" because it only tracked the file-tool call, not the disk.
``_snapshot_file_mutation_target`` / ``_file_mutation_target_changed`` /
``AIAgent._reconcile_file_mutation_failures_with_disk`` close that gap:
a failed path is re-stat/re-hashed against its failure-time snapshot
right before the footer renders, and any path the disk proves wrong is
dropped from the failure list.
"""

from __future__ import annotations

import json

import pytest

from run_agent import (
    AIAgent,
    _FILE_MUTATING_TOOLS,
    _extract_error_preview,
    _extract_file_mutation_targets,
    _extract_landed_file_mutation_paths,
    _file_mutation_target_changed,
    _snapshot_file_mutation_target,
)


# ---------------------------------------------------------------------------
# _extract_file_mutation_targets
# ---------------------------------------------------------------------------


class TestExtractFileMutationTargets:
    def test_non_mutating_tool_returns_empty(self):
        assert _extract_file_mutation_targets("read_file", {"path": "/x"}) == []
        assert _extract_file_mutation_targets("terminal", {"command": "ls"}) == []



    def test_patch_replace_mode_returns_path(self):
        args = {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"}
        assert _extract_file_mutation_targets("patch", args) == ["/tmp/a.md"]



    def test_patch_v4a_multi_file(self):
        body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/a.md\n"
            "@@ @@\n-a\n+b\n"
            "*** Add File: /tmp/new.md\n"
            "+fresh\n"
            "*** Delete File: /tmp/old.md\n"
            "*** End Patch\n"
        )
        args = {"mode": "patch", "patch": body}
        paths = _extract_file_mutation_targets("patch", args)
        assert paths == ["/tmp/a.md", "/tmp/new.md", "/tmp/old.md"]


    def test_patch_v4a_accepts_no_space_after_asterisks(self):
        """Match patch_parser / file_tools: ``***Update File:`` (no space)."""
        body = "***Update File: nospace.py\n"
        assert _extract_file_mutation_targets(
            "patch", {"mode": "patch", "patch": body}
        ) == ["nospace.py"]


# ---------------------------------------------------------------------------
# _extract_error_preview
# ---------------------------------------------------------------------------


class TestExtractErrorPreview:
    def test_json_error_field_preferred(self):
        raw = json.dumps({"success": False, "error": "Could not find old_string in /tmp/x"})
        assert _extract_error_preview(raw) == "Could not find old_string in /tmp/x"

    def test_plain_string_falls_through(self):
        assert _extract_error_preview("Error executing tool: boom") == "Error executing tool: boom"

    def test_long_preview_truncated(self):
        long = "x" * 500
        out = _extract_error_preview(long, max_len=50)
        assert len(out) <= 50
        assert out.endswith("…")



# ---------------------------------------------------------------------------
# _record_file_mutation_result — state transitions
# ---------------------------------------------------------------------------


def _bare_agent() -> AIAgent:
    """Skip __init__ and only attach the per-turn state dict.

    AIAgent.__init__ takes ~60 parameters and touches network, auth, and
    the filesystem.  For these tests we only need the two methods —
    ``_record_file_mutation_result`` and ``_format_file_mutation_failure_footer``.
    Using ``object.__new__`` mirrors the gateway-test pattern documented in
    the agent pitfalls list.
    """
    agent = object.__new__(AIAgent)
    agent._turn_failed_file_mutations = {}
    agent._turn_file_mutation_paths = set()
    return agent


class TestRecordFileMutationResult:
    def test_non_mutating_tool_ignored(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "read_file", {"path": "/tmp/x"}, "{}", is_error=True,
        )
        assert agent._turn_failed_file_mutations == {}

    def test_failure_recorded(self):
        agent = _bare_agent()
        result = json.dumps({"success": False, "error": "Could not find old_string"})
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"},
            result, is_error=True,
        )
        state = agent._turn_failed_file_mutations
        assert "/tmp/a.md" in state
        assert state["/tmp/a.md"]["tool"] == "patch"
        assert "Could not find old_string" in state["/tmp/a.md"]["error_preview"]

    def test_success_removes_prior_failure(self):
        agent = _bare_agent()
        # First attempt fails
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"},
            json.dumps({"error": "not found"}), is_error=True,
        )
        assert "/tmp/a.md" in agent._turn_failed_file_mutations
        # Second attempt with corrected old_string succeeds
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "real", "new_string": "fixed"},
            json.dumps({"success": True, "diff": "..."}), is_error=False,
        )
        assert agent._turn_failed_file_mutations == {}
        assert agent._turn_file_mutation_paths == {"/tmp/a.md"}


    def test_landed_paths_prefer_resolved_tool_result(self):
        paths = _extract_landed_file_mutation_paths(
            "patch",
            {"mode": "replace", "path": "src/app.py"},
            json.dumps({
                "success": True,
                "files_modified": ["/tmp/project/src/app.py"],
            }),
        )

        assert paths == ["/tmp/project/src/app.py"]

    def test_write_file_with_lint_error_counts_as_landed(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "write_file",
            {"path": "/tmp/a.py", "content": "bad"},
            json.dumps({"error": "write failed"}),
            is_error=True,
        )
        assert "/tmp/a.py" in agent._turn_failed_file_mutations

        result = json.dumps({
            "bytes_written": 24,
            "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
        })

        agent._record_file_mutation_result(
            "write_file",
            {"path": "/tmp/a.py", "content": "def nope(:\n"},
            result,
            is_error=True,
        )

        assert agent._turn_failed_file_mutations == {}

    def test_patch_with_lsp_diagnostics_counts_as_landed(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "/tmp/a.py", "old_string": "x", "new_string": "y"},
            json.dumps({"error": "Could not find old_string"}),
            is_error=True,
        )
        assert "/tmp/a.py" in agent._turn_failed_file_mutations

        result = json.dumps({
            "success": True,
            "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
            "files_modified": ["/tmp/a.py"],
            "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
        })

        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "/tmp/a.py", "old_string": "x", "new_string": "y"},
            result,
            is_error=True,
        )

        assert agent._turn_failed_file_mutations == {}

    def test_repeated_failure_keeps_first_error(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "v1", "new_string": "y"},
            json.dumps({"error": "first error"}), is_error=True,
        )
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "v2", "new_string": "y"},
            json.dumps({"error": "second error"}), is_error=True,
        )
        # Keep the original error — swapping to the latest would obscure
        # the initial root cause.
        assert "first error" in agent._turn_failed_file_mutations["/tmp/a.md"]["error_preview"]





# ---------------------------------------------------------------------------
# File changed via another route (terminal / CLI / execute_code) — #C1 bug
# ---------------------------------------------------------------------------


class TestReconcileFileMutationFailuresWithDisk:
    """Covers `_snapshot_file_mutation_target` / `_file_mutation_target_changed`
    / `AIAgent._reconcile_file_mutation_failures_with_disk` — the turn-end
    re-stat that drops a failure the disk has since contradicted."""

    def test_snapshot_then_out_of_band_change_clears_failure(self, tmp_path):
        """(1) failed patch, then an out-of-band on-disk change → no footer entry."""
        target = tmp_path / "config.yaml"
        target.write_text("model:\n  default: old-model\n")

        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": str(target), "old_string": "x", "new_string": "y"},
            json.dumps({"error": "Refusing to write to Clover config file"}),
            is_error=True,
        )
        assert str(target) in agent._turn_failed_file_mutations

        # Simulate the terminal tool running `clover config set ...` —
        # a completely different code path writes the file directly.
        target.write_text("model:\n  default: claude-opus-5-5\n")

        agent._reconcile_file_mutation_failures_with_disk()
        assert agent._turn_failed_file_mutations == {}

    def test_untouched_file_still_reported(self, tmp_path):
        """(2) failed patch, file genuinely untouched → footer still reported."""
        target = tmp_path / "untouched.md"
        target.write_text("original content\n")

        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": str(target), "old_string": "x", "new_string": "y"},
            json.dumps({"error": "Could not find old_string"}),
            is_error=True,
        )
        assert str(target) in agent._turn_failed_file_mutations

        # Nothing touches the file between the failure and turn-end.
        agent._reconcile_file_mutation_failures_with_disk()
        assert str(target) in agent._turn_failed_file_mutations

    def test_nonexistent_file_later_created_clears_failure(self, tmp_path):
        """(3) failed write to a non-existent file that another route later
        creates → cleared."""
        target = tmp_path / "new_file.txt"
        assert not target.exists()

        agent = _bare_agent()
        agent._record_file_mutation_result(
            "write_file",
            {"path": str(target), "content": "data"},
            json.dumps({"error": "permission denied"}),
            is_error=True,
        )
        assert str(target) in agent._turn_failed_file_mutations
        snap = agent._turn_failed_file_mutations[str(target)]["snapshot"]
        assert snap == {"exists": False}

        # execute_code / terminal creates the file via another route.
        target.write_text("created out of band\n")

        agent._reconcile_file_mutation_failures_with_disk()
        assert agent._turn_failed_file_mutations == {}

    def test_failed_then_successful_file_tool_write_clears(self, tmp_path):
        """(4) failed then successful file-tool write → cleared (pre-existing
        behavior, still holds after reconciliation runs)."""
        target = tmp_path / "a.md"
        target.write_text("x\n")

        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": str(target), "old_string": "nope", "new_string": "y"},
            json.dumps({"error": "not found"}),
            is_error=True,
        )
        assert str(target) in agent._turn_failed_file_mutations

        target.write_text("y\n")
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": str(target), "old_string": "x", "new_string": "y"},
            json.dumps({"success": True, "diff": "..."}),
            is_error=False,
        )
        assert agent._turn_failed_file_mutations == {}

        # Reconciliation on an already-empty dict is a no-op.
        agent._reconcile_file_mutation_failures_with_disk()
        assert agent._turn_failed_file_mutations == {}

    def test_missing_snapshot_keeps_current_behavior(self):
        """A path with no snapshot (e.g. recorded by old in-memory state,
        or the file lives somewhere unstattable) is left exactly as-is —
        reconciliation never invents a change it can't prove."""
        agent = _bare_agent()
        agent._turn_failed_file_mutations["/no/snapshot/here.md"] = {
            "tool": "patch",
            "error_preview": "boom",
        }
        agent._reconcile_file_mutation_failures_with_disk()
        assert "/no/snapshot/here.md" in agent._turn_failed_file_mutations

    def test_snapshot_unavailable_sentinel_is_conservative(self):
        """`_file_mutation_target_changed` returns False (no claim of
        change) when the snapshot itself is the 'unavailable' sentinel."""
        assert _file_mutation_target_changed("/tmp/whatever", {"exists": None}) is False
        assert _file_mutation_target_changed("/tmp/whatever", None) is False

    def test_snapshot_missing_file_round_trip(self, tmp_path):
        missing = tmp_path / "ghost.txt"
        snap = _snapshot_file_mutation_target(str(missing))
        assert snap == {"exists": False}
        assert _file_mutation_target_changed(str(missing), snap) is False
        missing.write_text("now it exists\n")
        assert _file_mutation_target_changed(str(missing), snap) is True

    def test_snapshot_content_hash_detects_same_size_edit(self, tmp_path):
        """A same-size, same-second edit must still be caught via sha256
        (size + mtime alone can't distinguish this case)."""
        target = tmp_path / "same_size.txt"
        target.write_text("aaaa")
        snap = _snapshot_file_mutation_target(str(target))
        assert "sha256" in snap

        # Force identical mtime_ns to simulate a same-tick rewrite, only
        # the content differs.
        import os as _os
        st = _os.stat(target)
        target.write_text("bbbb")
        _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))

        assert _file_mutation_target_changed(str(target), snap) is True


# ---------------------------------------------------------------------------
# _format_file_mutation_failure_footer
# ---------------------------------------------------------------------------


class TestFormatFooter:
    def test_empty_returns_empty_string(self):
        assert AIAgent._format_file_mutation_failure_footer({}) == ""

    def test_single_failure(self):
        out = AIAgent._format_file_mutation_failure_footer(
            {"/tmp/a.md": {"tool": "patch", "error_preview": "Could not find old_string"}},
        )
        assert "1 file(s) were NOT modified" in out
        assert "/tmp/a.md" in out
        assert "Could not find old_string" in out
        assert "git status" in out  # user-actionable hint

    def test_truncation_at_10_entries(self):
        failed = {
            f"/tmp/f{i}.md": {"tool": "patch", "error_preview": "err"}
            for i in range(15)
        }
        out = AIAgent._format_file_mutation_failure_footer(failed)
        assert "15 file(s) were NOT modified" in out
        assert "… and 5 more" in out
        # Ten file bullets + header + "and X more" line
        lines = out.split("\n")
        bullet_lines = [ln for ln in lines if ln.lstrip().startswith("•")]
        assert len(bullet_lines) == 11  # 10 shown + 1 summary


    def test_footer_path_not_extracted_by_gateway(self):
        """End-to-end: the gateway's extract_local_files must NOT pull a
        config.yaml path out of the rendered footer (#35584)."""
        import os
        import tempfile
        from gateway.platforms.base import BasePlatformAdapter

        tmp = tempfile.mkdtemp(prefix="clover_footer_")
        try:
            cfg = os.path.join(tmp, "config.yaml")
            with open(cfg, "w") as fh:
                fh.write("openrouter_api_key: sk-LEAK\n")
            footer = AIAgent._format_file_mutation_failure_footer(
                {cfg: {
                    "tool": "patch",
                    "error_preview": (
                        f"Write denied: '{cfg}' is a protected "
                        "system/credential file."
                    ),
                }},
            )
            response = "I updated your config.\n\n" + footer
            paths, _ = BasePlatformAdapter.extract_local_files(response)
            assert paths == [], f"footer leaked deliverable path(s): {paths}"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# _file_mutation_verifier_enabled — env + config precedence
# ---------------------------------------------------------------------------


class TestVerifierEnabled:
    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("CLOVER_FILE_MUTATION_VERIFIER", raising=False)
        agent = _bare_agent()
        # With no env and no config present, safe default is True.
        # load_config may surface a user config.yaml in some envs — stub it.
        import clover_cli.config as _cfg_mod
        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {})
        assert agent._file_mutation_verifier_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
    def test_env_disables(self, monkeypatch, value):
        monkeypatch.setenv("CLOVER_FILE_MUTATION_VERIFIER", value)
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is False

    def test_config_read_once_then_cached(self, monkeypatch):
        """Measured-work pin: the config lookup happens once per agent.

        The footer gate runs at the end of every turn, so a fresh
        ``load_config()`` per call is wasted work (measured ~0.9 ms/call on
        a warm mtime-cache on this host; the sibling per-turn-config kill in
        #74211 removed exactly this class of read).  The config read must be
        cached after the first call; the env-var override must still win on
        every call, cached or not.
        """
        monkeypatch.delenv("CLOVER_FILE_MUTATION_VERIFIER", raising=False)
        agent = _bare_agent()
        calls = {"n": 0}

        import clover_cli.config as _cfg_mod

        def counting_load():
            calls["n"] += 1
            return {"display": {"file_mutation_verifier": True}}

        monkeypatch.setattr(_cfg_mod, "load_config", counting_load)

        # First call reads config and caches the result.
        assert agent._file_mutation_verifier_enabled() is True
        assert calls["n"] == 1
        # Subsequent calls must not re-read config.
        assert agent._file_mutation_verifier_enabled() is True
        assert agent._file_mutation_verifier_enabled() is True
        assert calls["n"] == 1
        # Env override stays authoritative even after the cache is warm.
        monkeypatch.setenv("CLOVER_FILE_MUTATION_VERIFIER", "0")
        assert agent._file_mutation_verifier_enabled() is False
        assert calls["n"] == 1  # env path never touches config

    def test_cache_respects_config_value(self, monkeypatch):
        """A disabled config value is cached as False, not re-read."""
        monkeypatch.delenv("CLOVER_FILE_MUTATION_VERIFIER", raising=False)
        agent = _bare_agent()

        import clover_cli.config as _cfg_mod
        monkeypatch.setattr(
            _cfg_mod, "load_config", lambda: {"display": {"file_mutation_verifier": False}}
        )
        assert agent._file_mutation_verifier_enabled() is False
        # Warm cache: flip the underlying config; the agent still reports the
        # cached value (next-session semantics).
        monkeypatch.setattr(
            _cfg_mod, "load_config", lambda: {"display": {"file_mutation_verifier": True}}
        )
        assert agent._file_mutation_verifier_enabled() is False




# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


def test_file_mutating_tools_set_shape():
    """write_file + patch are the only tools the verifier tracks.

    Guard rail: if someone adds a third file-mutating tool (e.g. a new
    ``append_file``), they should also audit whether the verifier should
    track it.  This test fails loudly on unilateral additions.
    """
    assert _FILE_MUTATING_TOOLS == frozenset({"write_file", "patch"})
