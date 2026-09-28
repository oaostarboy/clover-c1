"""Windows encoding regressions from the Sonnet 5 platform audit."""

import sys

import pytest


def test_config_yaml_with_bom_still_loads_and_accepts_writes(tmp_path, monkeypatch):
    """Notepad saves config.yaml with a UTF-8 BOM. Before, every reader raised
    and config writes (clover setup, /skin, any setting) were refused."""
    home = tmp_path / ".clover"
    home.mkdir()
    (home / "config.yaml").write_bytes("\ufeffmodel:\n  default: gpt-6-sol\n".encode("utf-8"))
    monkeypatch.setenv("CLOVER_HOME", str(home))
    from clover_cli import config as cfg

    raw = cfg.read_raw_config()
    assert raw["model"]["default"] == "gpt-6-sol"
    loaded = cfg._load_user_config_for_mutation(home / "config.yaml")
    assert loaded["model"]["default"] == "gpt-6-sol"


def test_probe_decodes_with_the_windows_console_code_page(monkeypatch):
    from clover_cli import _subprocess_compat as sc

    monkeypatch.setattr(sc, "IS_WINDOWS", True)
    monkeypatch.setattr("locale.getpreferredencoding", lambda do_setlocale=True: "cp1252")
    assert sc.probe_output_encoding() == "cp1252"
    monkeypatch.setattr(sc, "IS_WINDOWS", False)
    assert sc.probe_output_encoding() == "utf-8"


def test_probe_run_passes_the_chosen_codec_to_the_child(monkeypatch):
    from clover_cli import _subprocess_compat as sc

    seen = {}
    real = sc.subprocess.Popen

    def spy(*a, **kw):
        seen["encoding"] = kw.get("encoding")
        return real(*a, **kw)

    monkeypatch.setattr(sc.subprocess, "Popen", spy)
    monkeypatch.setattr(sc, "probe_output_encoding", lambda: "cp1252")
    res = sc.bounded_probe_run(
        [sys.executable, "-c", "print('ok')"], timeout=30, encoding=sc.probe_output_encoding()
    )
    assert res is not None and res.stdout.strip() == "ok"
    assert seen["encoding"] == "cp1252"
    # Default stays UTF-8: git and Python children always emit UTF-8.
    sc.bounded_probe_run([sys.executable, "-c", "print('ok')"], timeout=30)
    assert seen["encoding"] == "utf-8"
