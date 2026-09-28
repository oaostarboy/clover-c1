"""Regression tests for image_gen provider persistence (direct-key selection).

Contract: each picker row writes exactly ONE provider string per category —
``image_gen.provider: fal`` for the BYOK FAL row — and any legacy
``use_gateway`` key is popped so the read-time shim (use_gateway: true ⇒
clover) cannot override the fresh pick. The video twin
(``_select_plugin_video_gen_provider``) shares the contract.

The managed "Clover Subscription" routing this file used to guard against a
clobber bug in was removed along with the hosted Clover Portal Tool Gateway
feature; only the surviving BYOK/direct-key contract is tested here.
"""

from clover_cli.tools_config import (
    _select_plugin_image_gen_provider,
    _select_plugin_video_gen_provider,
)


def _quiet(monkeypatch):
    import clover_cli.tools_config as tc

    monkeypatch.setattr(tc, "_print_success", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_print_info", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(tc, "_configure_imagegen_model_for_plugin", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_configure_videogen_model_for_plugin", lambda *a, **k: None)


def test_image_gen_selector_direct_key_pick_writes_vendor(monkeypatch):
    """Picking a provider writes the vendor name and pops the legacy flag."""
    _quiet(monkeypatch)
    config = {"image_gen": {"use_gateway": True}}

    _select_plugin_image_gen_provider("fal", config)
    assert config["image_gen"]["provider"] == "fal"
    assert "use_gateway" not in config["image_gen"]


def test_image_and_video_selectors_share_the_selection_contract(monkeypatch):
    """The two selectors are twins: same vendor-name persistence behavior."""
    _quiet(monkeypatch)

    config = {
        "image_gen": {"use_gateway": True},
        "video_gen": {"use_gateway": True},
    }
    _select_plugin_image_gen_provider("fal", config)
    _select_plugin_video_gen_provider("fal", config)
    assert config["image_gen"]["provider"] == "fal"
    assert config["video_gen"]["provider"] == "fal"
    assert "use_gateway" not in config["image_gen"]
    assert "use_gateway" not in config["video_gen"]


def _quiet_reconfigure(monkeypatch):
    """Silence prints + model pickers for _reconfigure_provider paths."""
    import clover_cli.tools_config as tc

    monkeypatch.setattr(tc, "_print_success", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_print_info", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(tc, "_print_warning", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(tc, "_configure_imagegen_model", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_run_post_setup", lambda *a, **k: None, raising=False)


def test_reconfigure_direct_fal_row_writes_vendor_selection(monkeypatch):
    """Direct-key FAL reconfig writes the vendor name and pops any stale
    legacy use_gateway key so the read-time shim can't resurrect it."""
    _quiet_reconfigure(monkeypatch)
    import clover_cli.tools_config as tc

    direct_row = {
        "name": "FAL.ai",
        "env_vars": [],
        "imagegen_backend": "fal",
    }
    config = {"image_gen": {"use_gateway": True}}

    tc._reconfigure_provider(direct_row, config)

    assert config["image_gen"]["provider"] == "fal"
    assert "use_gateway" not in config["image_gen"]
