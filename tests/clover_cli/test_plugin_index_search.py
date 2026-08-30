"""Tests for the community plugin index (#64181).

Covers: index parsing, fuzzy search, cache TTL + fallback chain
(remote → cache → seed), `clover plugins search --json`, and install-time
name resolution (unique / ambiguous / passthrough of owner/repo).
No live network — every remote fetch is mocked.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from clover_cli import plugin_index
from clover_cli.plugin_index import (
    PluginIndexEntry,
    _parse_entries,
    load_index,
    resolve_name,
    search_index,
)


def _entry(name, repo="owner/repo", **kw):
    return PluginIndexEntry(name=name, repo=repo, **kw)


def _index_doc(entries):
    return {"schema_version": 1, "plugins": entries}


SAMPLE = _index_doc(
    [
        {
            "name": "clover-media-studio",
            "description": "Generative media workspace plugin.",
            "author": "cloverc1",
            "tags": ["media", "image-gen"],
            "repo": "cloverc1/clover-media-studio",
            "ref": "e" * 40,
        },
        {
            "name": "clover-telegram-business",
            "description": "Telegram secretary bot with owner approval.",
            "author": "cloverc1",
            "tags": ["telegram", "gateway"],
            "repo": "cloverc1/clover-telegram-business",
            "ref": "f" * 40,
            "capabilities": ["platform"],
        },
        {
            "name": "plugin-llm-example",
            "description": "Reference plugin for structured LLM access.",
            "author": "cloverc1",
            "tags": ["example", "llm"],
            "repo": "cloverc1/clover-example-plugins",
            "subdir": "plugin-llm-example",
            "ref": "a" * 40,
            "capabilities": ["commands", "llm"],
        },
    ]
)


@pytest.fixture()
def clover_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    monkeypatch.setattr(plugin_index, "get_clover_home", lambda: tmp_path)
    return tmp_path


def _write_cache(home: Path, doc, *, age_seconds: float = 0) -> Path:
    cache = home / "cache" / "plugin_index.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(doc), encoding="utf-8")
    if age_seconds:
        stamp = time.time() - age_seconds
        import os

        os.utime(cache, (stamp, stamp))
    return cache


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParsing:
    def test_parses_object_form(self):
        entries = _parse_entries(SAMPLE)
        assert [e.name for e in entries] == [
            "clover-media-studio",
            "clover-telegram-business",
            "plugin-llm-example",
        ]
        assert entries[2].subdir == "plugin-llm-example"
        assert entries[2].install_identifier == (
            "cloverc1/clover-example-plugins/plugin-llm-example"
        )
        assert entries[0].install_identifier == "cloverc1/clover-media-studio"

    def test_parses_bare_list_form(self):
        entries = _parse_entries(SAMPLE["plugins"])
        assert len(entries) == 3

    def test_skips_malformed_entries(self):
        doc = _index_doc(
            [
                {"name": "good", "repo": "o/r"},
                {"name": "", "repo": "o/r"},           # empty name
                {"name": "norepo"},                     # missing repo
                {"name": "badrepo", "repo": "not-a-repo"},  # no slash
                {"name": "deep", "repo": "a/b/c"},      # too many slashes
                "not-a-dict",
            ]
        )
        entries = _parse_entries(doc)
        assert [e.name for e in entries] == ["good"]

    def test_rejects_non_container(self):
        with pytest.raises(ValueError):
            _parse_entries("nope")

    def test_bundled_seed_parses(self):
        raw = json.loads(plugin_index.SEED_INDEX_PATH.read_text(encoding="utf-8"))
        entries = _parse_entries(raw)
        assert len(entries) >= 3
        for e in entries:
            assert e.repo.count("/") == 1
            assert e.ref, f"seed entry {e.name} must pin a ref"
            assert len(e.ref) == 40, f"seed entry {e.name} must pin a commit SHA"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    entries = _parse_entries(SAMPLE)

    def test_exact_name_ranks_first(self):
        results = search_index(self.entries, "clover-media-studio")
        assert results[0].name == "clover-media-studio"

    def test_matches_tags(self):
        results = search_index(self.entries, "telegram")
        assert results and results[0].name == "clover-telegram-business"

    def test_matches_description(self):
        results = search_index(self.entries, "secretary")
        assert [e.name for e in results] == ["clover-telegram-business"]

    def test_fuzzy_typo_tolerance(self):
        results = search_index(self.entries, "clover-media-studo")
        assert results and results[0].name == "clover-media-studio"

    def test_no_match(self):
        assert search_index(self.entries, "zzzzqqqq") == []

    def test_empty_term_browses_all_sorted(self):
        results = search_index(self.entries, "")
        assert [e.name for e in results] == sorted(e.name for e in self.entries)

    def test_capability_filter(self):
        results = search_index(self.entries, "", capability="platform")
        assert [e.name for e in results] == ["clover-telegram-business"]

    def test_capability_filter_with_term(self):
        results = search_index(self.entries, "llm", capability="commands")
        assert [e.name for e in results] == ["plugin-llm-example"]


# ---------------------------------------------------------------------------
# Fallback chain: remote → cache → seed
# ---------------------------------------------------------------------------


class TestLoadIndex:
    def test_fresh_cache_wins_without_network(self, clover_home, monkeypatch):
        _write_cache(clover_home, SAMPLE)

        def boom():  # pragma: no cover - must not be called
            raise AssertionError("network hit despite fresh cache")

        monkeypatch.setattr(plugin_index, "_fetch_remote", boom)
        entries, source = load_index()
        assert source == "cache"
        assert len(entries) == 3

    def test_expired_cache_triggers_remote(self, clover_home, monkeypatch):
        _write_cache(clover_home, SAMPLE, age_seconds=plugin_index.INDEX_CACHE_TTL + 60)
        remote_doc = _index_doc([{"name": "fresh-plugin", "repo": "o/r", "ref": "b" * 40}])
        monkeypatch.setattr(
            plugin_index, "_fetch_remote", lambda: _parse_entries(remote_doc)
        )
        entries, source = load_index()
        assert source == "remote"
        assert [e.name for e in entries] == ["fresh-plugin"]

    def test_remote_failure_falls_back_to_stale_cache(self, clover_home, monkeypatch):
        _write_cache(clover_home, SAMPLE, age_seconds=plugin_index.INDEX_CACHE_TTL + 60)
        monkeypatch.setattr(plugin_index, "_fetch_remote", lambda: None)
        entries, source = load_index()
        assert source == "cache"
        assert len(entries) == 3

    def test_no_cache_no_remote_falls_back_to_seed(self, clover_home, monkeypatch):
        monkeypatch.setattr(plugin_index, "_fetch_remote", lambda: None)
        entries, source = load_index()
        assert source == "seed"
        assert len(entries) >= 3

    def test_refresh_bypasses_fresh_cache(self, clover_home, monkeypatch):
        _write_cache(clover_home, SAMPLE)
        remote_doc = _index_doc([{"name": "newer", "repo": "o/r", "ref": "c" * 40}])
        monkeypatch.setattr(
            plugin_index, "_fetch_remote", lambda: _parse_entries(remote_doc)
        )
        entries, source = load_index(refresh=True)
        assert source == "remote"
        assert [e.name for e in entries] == ["newer"]

    def test_offline_skips_network(self, clover_home, monkeypatch):
        def boom():  # pragma: no cover
            raise AssertionError("network hit in offline mode")

        monkeypatch.setattr(plugin_index, "_fetch_remote", boom)
        entries, source = load_index(offline=True)
        assert source == "seed"

    def test_corrupt_cache_ignored(self, clover_home, monkeypatch):
        cache = clover_home / "cache" / "plugin_index.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(plugin_index, "_fetch_remote", lambda: None)
        entries, source = load_index()
        assert source == "seed"

    def test_remote_fetch_writes_cache(self, clover_home, monkeypatch):
        payload = json.dumps(SAMPLE)

        class FakeResponse:
            text = payload

            def raise_for_status(self):
                return None

        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())
        entries, source = load_index()
        assert source == "remote"
        cache = clover_home / "cache" / "plugin_index.json"
        assert cache.is_file()
        assert json.loads(cache.read_text(encoding="utf-8")) == SAMPLE

    def test_index_url_config_override(self, monkeypatch):
        monkeypatch.setattr(
            plugin_index,
            "get_index_url",
            plugin_index.get_index_url,  # keep real fn, patch config below
        )
        from clover_cli import config as config_mod

        monkeypatch.setattr(
            config_mod,
            "load_config_readonly",
            lambda: {"plugins": {"index_url": "https://example.com/custom.json"}},
        )
        assert plugin_index.get_index_url() == "https://example.com/custom.json"


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


class TestResolveName:
    entries = _parse_entries(SAMPLE)

    def test_exact_unique(self):
        entry, candidates = resolve_name(self.entries, "clover-media-studio")
        assert entry is not None and entry.repo == "cloverc1/clover-media-studio"

    def test_case_insensitive(self):
        entry, _ = resolve_name(self.entries, "Clover-Media-Studio")
        assert entry is not None

    def test_unique_partial(self):
        entry, _ = resolve_name(self.entries, "telegram")
        assert entry is not None and entry.name == "clover-telegram-business"

    def test_ambiguous_partial(self):
        entry, candidates = resolve_name(self.entries, "clover")
        assert entry is None
        assert len(candidates) == 2

    def test_unknown(self):
        entry, candidates = resolve_name(self.entries, "nonexistent-thing")
        assert entry is None and candidates == []


# ---------------------------------------------------------------------------
# Install wiring
# ---------------------------------------------------------------------------


class TestInstallResolution:
    def test_bare_name_detection(self):
        from clover_cli.plugins_cmd import _looks_like_bare_index_name

        assert _looks_like_bare_index_name("clover-media-studio")
        assert not _looks_like_bare_index_name("owner/repo")
        assert not _looks_like_bare_index_name("https://github.com/o/r.git")
        assert not _looks_like_bare_index_name("git@github.com:o/r.git")
        assert not _looks_like_bare_index_name("ssh://git@github.com/o/r.git")
        assert not _looks_like_bare_index_name("file:///tmp/x")

    def test_install_resolves_name_and_pins_ref(self, clover_home, monkeypatch):
        from clover_cli import plugins_cmd

        monkeypatch.setattr(
            plugin_index, "load_index", lambda **kw: (_parse_entries(SAMPLE), "seed")
        )
        captured = {}

        def fake_core(identifier, *, force, ref=None, scan_decision_cb=None):
            captured["identifier"] = identifier
            captured["ref"] = ref
            raise plugins_cmd.PluginOperationError("stop here")

        monkeypatch.setattr(plugins_cmd, "_install_plugin_core", fake_core)
        with pytest.raises(SystemExit):
            plugins_cmd.cmd_install("clover-media-studio", enable=False)
        assert captured["identifier"] == "cloverc1/clover-media-studio"
        assert captured["ref"] == "e" * 40

    def test_install_explicit_ref_beats_index_pin(self, clover_home, monkeypatch):
        from clover_cli import plugins_cmd

        monkeypatch.setattr(
            plugin_index, "load_index", lambda **kw: (_parse_entries(SAMPLE), "seed")
        )
        captured = {}

        def fake_core(identifier, *, force, ref=None, scan_decision_cb=None):
            captured["ref"] = ref
            raise plugins_cmd.PluginOperationError("stop here")

        monkeypatch.setattr(plugins_cmd, "_install_plugin_core", fake_core)
        with pytest.raises(SystemExit):
            plugins_cmd.cmd_install("clover-media-studio", enable=False, ref="d" * 40)
        assert captured["ref"] == "d" * 40

    def test_install_ambiguous_name_lists_candidates_and_exits(
        self, clover_home, monkeypatch, capsys
    ):
        from clover_cli import plugins_cmd

        monkeypatch.setattr(
            plugin_index, "load_index", lambda **kw: (_parse_entries(SAMPLE), "seed")
        )
        called = []
        monkeypatch.setattr(
            plugins_cmd,
            "_install_plugin_core",
            lambda *a, **k: called.append(1),
        )
        with pytest.raises(SystemExit) as exc:
            plugins_cmd.cmd_install("clover", enable=False)
        assert exc.value.code == 1
        assert not called
        out = capsys.readouterr().out
        assert "ambiguous" in out
        assert "clover-media-studio" in out
        assert "clover-telegram-business" in out

    def test_install_unknown_name_exits(self, clover_home, monkeypatch, capsys):
        from clover_cli import plugins_cmd

        monkeypatch.setattr(
            plugin_index, "load_index", lambda **kw: (_parse_entries(SAMPLE), "seed")
        )
        with pytest.raises(SystemExit) as exc:
            plugins_cmd.cmd_install("totally-unknown", enable=False)
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().out

    def test_owner_repo_passthrough_skips_index(self, clover_home, monkeypatch):
        """Explicit owner/repo installs never consult the index."""
        from clover_cli import plugins_cmd

        def boom(**kw):  # pragma: no cover
            raise AssertionError("index consulted for owner/repo identifier")

        monkeypatch.setattr(plugin_index, "load_index", boom)
        captured = {}

        def fake_core(identifier, *, force, ref=None, scan_decision_cb=None):
            captured["identifier"] = identifier
            captured["ref"] = ref
            raise plugins_cmd.PluginOperationError("stop here")

        monkeypatch.setattr(plugins_cmd, "_install_plugin_core", fake_core)
        with pytest.raises(SystemExit):
            plugins_cmd.cmd_install("someowner/somerepo", enable=False)
        assert captured["identifier"] == "someowner/somerepo"
        assert captured["ref"] is None


# ---------------------------------------------------------------------------
# CLI search command
# ---------------------------------------------------------------------------


class TestCmdSearch:
    def test_json_output(self, clover_home, monkeypatch, capsys):
        from clover_cli import plugins_cmd

        monkeypatch.setattr(
            plugin_index, "load_index", lambda **kw: (_parse_entries(SAMPLE), "seed")
        )
        plugins_cmd.cmd_search("telegram", json_output=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"] == "seed"
        assert payload["query"] == "telegram"
        assert payload["results"][0]["name"] == "clover-telegram-business"
        assert payload["results"][0]["repo"] == "cloverc1/clover-telegram-business"
        assert payload["results"][0]["ref"] == "f" * 40
        assert "audited" in payload["note"]

    def test_table_output_includes_security_footer(
        self, clover_home, monkeypatch, capsys
    ):
        from clover_cli import plugins_cmd

        monkeypatch.setattr(
            plugin_index, "load_index", lambda **kw: (_parse_entries(SAMPLE), "seed")
        )
        plugins_cmd.cmd_search("media")
        out = capsys.readouterr().out
        assert "clover-media-studio" in out
        assert "audited" in out

    def test_no_results_message(self, clover_home, monkeypatch, capsys):
        from clover_cli import plugins_cmd

        monkeypatch.setattr(
            plugin_index, "load_index", lambda **kw: (_parse_entries(SAMPLE), "seed")
        )
        plugins_cmd.cmd_search("zzzznope")
        assert "No plugins matched" in capsys.readouterr().out

    def test_parser_accepts_search(self):
        import argparse

        from clover_cli.subcommands.plugins import build_plugins_parser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        build_plugins_parser(sub, cmd_plugins=lambda args: None)
        args = parser.parse_args(
            ["plugins", "search", "media", "--json", "--capability", "tools", "--refresh"]
        )
        assert args.plugins_action == "search"
        assert args.term == "media"
        assert args.json is True
        assert args.capability == "tools"
        assert args.refresh is True

    def test_dispatch_routes_search(self, clover_home, monkeypatch):
        from clover_cli import plugins_cmd

        captured = {}

        def fake_search(term, *, json_output, capability, refresh):
            captured.update(
                term=term, json_output=json_output, capability=capability, refresh=refresh
            )

        monkeypatch.setattr(plugins_cmd, "cmd_search", fake_search)
        import argparse

        args = argparse.Namespace(
            plugins_action="search", term="llm", json=True, capability=None, refresh=False
        )
        plugins_cmd.plugins_command(args)
        assert captured == {
            "term": "llm",
            "json_output": True,
            "capability": None,
            "refresh": False,
        }
