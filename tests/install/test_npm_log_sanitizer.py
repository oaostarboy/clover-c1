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
