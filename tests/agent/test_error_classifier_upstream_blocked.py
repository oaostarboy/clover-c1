"""A 403 that is not a credential refusal must not read as a bad API key (R09).

Two shapes are not key rejections:

* A WAF/CDN/relay in front of the provider answers 403 with a block or challenge
  page (Cloudflare browser challenge, "Your request was blocked."). The credential
  never reached the provider, so the verdict is ``upstream_blocked``: no
  rotation, no retry, fallback allowed.
* A gateway answers 403 with the structured code ``upstream_unavailable`` (an
  upstream outage). That is transient: ``overloaded``, retried with backoff, no
  credential benched.

Generic 403 and every 401 keep the ``auth`` verdict.

Adapted from NousResearch/hermes-agent 6f6ed01355 and 1213109474 (MIT).
"""

import pytest

from agent.error_classifier import FailoverReason, classify_api_error


class _APIError(Exception):
    def __init__(self, message, status_code, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


@pytest.mark.parametrize("body", [
    "Error code: 403 - Your request was blocked.",
    "<!doctype html><html><body>Enable JavaScript and cookies to continue</body></html>",
    "<!doctype html><html><script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page'></script></html>",
])
def test_403_waf_block_is_upstream_blocked_not_auth(body):
    result = classify_api_error(_APIError(body, 403), provider="openai-api")
    assert result.reason == FailoverReason.upstream_blocked
    assert result.retryable is False
    assert result.should_fallback is True
    assert result.should_rotate_credential is False
    assert result.is_auth is False


def test_403_upstream_unavailable_code_is_transient_not_auth():
    body = {"error": {
        "message": "Upstream service temporarily unavailable. Please retry later.",
        "type": "upstream_unavailable",
        "code": "upstream_unavailable",
    }}
    result = classify_api_error(_APIError("Forbidden", 403, body=body), provider="custom")
    assert result.reason == FailoverReason.overloaded
    assert result.retryable is True
    assert result.should_rotate_credential is False
    assert result.is_auth is False


@pytest.mark.parametrize("message, status, reason", [
    ("<html><title>Forbidden</title><body>Access denied</body></html>", 403, FailoverReason.auth),
    ("Invalid API key", 403, FailoverReason.auth),
    ("<html>Enable JavaScript and cookies to continue</html>", 401, FailoverReason.auth),
])
def test_generic_403_and_all_401_keep_auth(message, status, reason):
    assert classify_api_error(_APIError(message, status), provider="openai-api").reason == reason
