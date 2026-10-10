from scripts.sandbox.sanitize_npm_log import sanitize_debug_text


def test_npm_summary_keeps_only_code_and_hostname() -> None:
    raw = """0 verbose stack FetchError: request to https://user:password@registry.npmjs.org/@scope/pkg?token=secret failed, reason: UNABLE_TO_VERIFY_LEAF_SIGNATURE
0 error code UNABLE_TO_VERIFY_LEAF_SIGNATURE
0 error Authorization: Bearer private-token
0 verbose argv npm install https://registry.npmjs.org/pkg?auth=private
"""

    summary = sanitize_debug_text(raw)

    assert summary.splitlines() == [
        "npm_error_code=UNABLE_TO_VERIFY_LEAF_SIGNATURE",
        "npm_error_host=registry.npmjs.org",
    ]
    for secret in ("user", "password", "secret", "private-token", "@scope", "Authorization", "https://"):
        assert secret not in summary


def test_npm_summary_handles_missing_or_unparseable_details() -> None:
    assert sanitize_debug_text("0 verbose title npm install\n") == "npm_debug_failure_details=unavailable"
    assert sanitize_debug_text("0 error code ECONNRESET\n") == "npm_error_code=ECONNRESET"


def test_npm_summary_names_failing_lifecycle_package_without_local_paths() -> None:
    raw = """12 error code 1
12 error path /home/clover/.clover/clover-c1/node_modules/electron
12 error command failed
12 error command sh -c node install.js
12 error Error: connect to https://user:pw@github.com/x/y.zip?token=abc failed
"""

    summary = sanitize_debug_text(raw, {"electron"})

    assert summary.splitlines() == ["npm_error_code=1", "npm_error_package=electron"]
    for leaked in ("/home", "clover-c1", "token", "pw", "github.com", "install.js"):
        assert leaked not in summary


def test_npm_summary_keeps_scoped_package_name() -> None:
    raw = "1 error code 1\n1 error path /w/node_modules/@parcel/watcher\n"

    assert "npm_error_package=@parcel/watcher" in sanitize_debug_text(raw, {"@parcel/watcher"}).splitlines()


def test_npm_summary_maps_node_gyp_line_to_a_fixed_label() -> None:
    raw = """1 error code 1
1 error path /home/runner/work/x/node_modules/node-pty
5 gyp ERR! stack Error: not found: make
5 gyp ERR! stack     at /home/runner/.clover/node/lib/node_modules/npm/node_modules/node-gyp/lib/find.js:1:1
"""

    summary = sanitize_debug_text(raw, {"node-pty"})

    assert summary.splitlines() == [
        "npm_error_code=1",
        "npm_error_package=node-pty",
        "node_gyp_cause=make_not_found",
    ]


def test_npm_summary_never_copies_free_text_from_gyp_or_error_lines() -> None:
    secret = "SYNTHETIC_ONLY_NOT_A_SECRET"
    hostile = "\n".join(
        [
            f"5 gyp ERR! stack Error: Authorization: Bearer {secret}",
            f"5 gyp ERR! stack Error: ECONNRESET X-Api-Key: {secret}",
            f"6 gyp ERR! stack Error: token={secret} password={secret} at /opt/{secret}/x",
            f"7 error code Bearer {secret}",
            f"8 error code {secret}",
            f"9 error path /srv/{secret}/node_modules/pkg-{secret}",
            f"10 verbose stack FetchError: request to https://user:{secret}@evil.example.com/{secret}?k={secret} failed",
        ]
    )

    summary = sanitize_debug_text(hostile, {"node-pty"})

    assert secret not in summary
    for leaked in ("Bearer", "Authorization", "Api-Key", "password", "token", "/opt", "/srv", "user:"):
        assert leaked not in summary
    # Only allowlisted facts can appear: a recognised cause label and a hostname.
    assert set(line.split("=")[0] for line in summary.splitlines()) <= {
        "npm_error_code",
        "npm_error_package",
        "node_gyp_cause",
        "npm_error_host",
    }
    assert "node_gyp_cause=connection_reset" in summary.splitlines()
    assert not any(line.startswith("npm_error_code=") for line in summary.splitlines())


def test_npm_summary_unrecognised_gyp_line_emits_no_cause() -> None:
    assert sanitize_debug_text("5 gyp ERR! stack Error: something novel with Bearer abc\n") == (
        "npm_debug_failure_details=unavailable"
    )


def test_npm_summary_omits_package_the_lockfile_does_not_pin() -> None:
    raw = "1 error code 1\n1 error path /w/node_modules/not-in-our-lock\n"

    assert sanitize_debug_text(raw, {"node-pty"}) == "npm_error_code=1"
    assert sanitize_debug_text(raw) == "npm_error_code=1"


def test_cli_reads_package_names_from_the_lockfile(tmp_path) -> None:
    import subprocess
    import sys
    from pathlib import Path

    logs = tmp_path / "_logs"
    logs.mkdir()
    (logs / "2026-x-debug-0.log").write_text(
        "1 error code 1\n1 error path /w/node_modules/node-pty\n", encoding="utf-8"
    )
    lock = tmp_path / "package-lock.json"
    lock.write_text('{"packages": {"": {}, "node_modules/node-pty": {}}}', encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "scripts/sandbox/sanitize_npm_log.py"

    out = subprocess.run(
        [sys.executable, str(script), str(logs), "--lockfile", str(lock)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
    ).stdout

    assert out.splitlines() == ["npm_error_code=1", "npm_error_package=node-pty"]
