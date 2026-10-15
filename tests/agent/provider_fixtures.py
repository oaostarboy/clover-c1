"""Opt-in provider discovery isolation for scripted owner tests.

Import this fixture only in tests that script their actual model responses and
are not testing metadata catalogs or auxiliary credential/provider resolution.
The real context sizing, compressor, turn loop, SQLite and work owners stay in
place; test-local patches installed later can still supply a specific catalog
or auxiliary client. No global cache resets or network allowlists.
"""
import pytest


@pytest.fixture(autouse=True)
def scripted_provider_edges(monkeypatch):
    import agent.model_metadata as metadata
    import agent.auxiliary_client as auxiliary

    # No remote catalog is enrolled in these scripted tests. Keep the real
    # model-name/config fallback logic, rather than fabricating a context size.
    monkeypatch.setattr(metadata, "fetch_model_metadata", lambda *a, **k: {})
    monkeypatch.setattr(
        metadata, "_fetch_codex_oauth_context_lengths_with_source",
        lambda *a, **k: ({}, False),
    )

    def no_automatic_auxiliary_provider(*args, **kwargs):
        # The suite scripts its main client and any aux response it needs.
        # An unconfigured optional aux lookup is unavailable, not a live auth
        # probe into gh/Codex credentials and not a fake successful completion.
        raise RuntimeError("No scripted automatic auxiliary provider configured")

    monkeypatch.setattr(auxiliary, "_resolve_auto_route", no_automatic_auxiliary_provider)
