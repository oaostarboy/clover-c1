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

    summary = sanitize_debug_text(raw)

    assert summary.splitlines() == ["npm_error_code=1", "npm_error_package=electron"]
    for leaked in ("/home", "clover-c1", "token", "pw", "github.com", "install.js"):
        assert leaked not in summary


def test_npm_summary_keeps_scoped_package_name() -> None:
    raw = "1 error code 1\n1 error path /w/node_modules/@parcel/watcher\n"

    assert "npm_error_package=@parcel/watcher" in sanitize_debug_text(raw).splitlines()


def test_npm_summary_keeps_scrubbed_node_gyp_reason() -> None:
    raw = """1 error code 1
1 error path /home/runner/work/x/node_modules/node-pty
5 gyp ERR! stack Error: not found: make
5 gyp ERR! stack     at /home/runner/.clover/node/lib/node_modules/npm/node_modules/node-gyp/lib/find.js:1:1
6 gyp ERR! stack Error: connect ECONNRESET https://nodejs.org/download/release/v26/node-headers.tar.gz?t=secret
"""

    summary = sanitize_debug_text(raw)

    assert "node_gyp_error=Error: not found: make" in summary.splitlines()
    for leaked in ("/home", "runner", "secret", "nodejs.org", "https://"):
        assert leaked not in summary


def test_npm_summary_scrubs_urls_and_paths_inside_node_gyp_line() -> None:
    raw = "5 gyp ERR! stack Error: request to https://u:p@nodejs.org/x?k=v failed in /opt/secret/dir\n"

    summary = sanitize_debug_text(raw)

    assert summary == "node_gyp_error=Error: request to <url> failed in <path>"
